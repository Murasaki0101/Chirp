"""Turn a raw chat message into a structured AnalysisResult via one Jev call.

Jev's fan-out is essentially free (40 questions ≈ 1 question in latency), so we
ask ~20 questions per message and score locally. No LLM needed. Reply
suggestions come from a rule table keyed on the top intents, since Jev itself
does not generate text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..infrastructure.jev_client import (
    ChoiceAnswer,
    JevError,
    JevResponse,
    NoulAnswer,
    ScoreAnswer,
)
from ..infrastructure.engine import DecisionEngine
from .context import UserContext, empty_context

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Question bank
# ---------------------------------------------------------------------------

# Every entry: key -> (noul-instruction, human-readable Chinese label)
# Instructions are kept short and example-free so BOTH Jev and local Laya stay
# calibrated (Laya over-triggers when instructions embed quoted Chinese examples).
INTENT_QUESTIONS: dict[str, tuple[str, str]] = {
    "task_request":    ("The message asks the reader to do a specific task or deliver something.", "任务请求"),
    "has_deadline":    ("The message specifies a deadline, due time, or explicit time constraint.", "含截止时间"),
    "urgent_word":     ("The message contains words expressing urgency or time pressure.", "紧急用词"),
    "info_question":   ("The message asks a question that requires an informational answer.", "询问信息"),
    "decision_needed": ("The message asks the reader to make a decision, choose an option, or approve something.", "需决策/审批"),
    "approval_request":("The message asks for approval, sign-off, review, or confirmation.", "求审批"),
    "bug_or_issue":    ("The message reports a bug, error, incident, or something broken.", "问题反馈"),
    "meeting_related": ("The message is about scheduling, joining, or following up on a meeting.", "会议相关"),
    "work_coord":      ("The message coordinates work: handoff, status update, task assignment, or collaboration.", "工作对接"),
    "sharing_info":    ("The message shares information, files, links, or FYI content with no explicit ask.", "分享资料"),
    "broadcast":       ("The message is a broadcast/announcement to many people and does not require a personal reply.", "广播通知"),
    "greeting":        ("The message is a greeting or opening salutation.", "打招呼"),
    "thanks":          ("The message expresses thanks or appreciation.", "感谢"),
    "casual_chat":     ("The message is casual small talk unrelated to work tasks.", "闲聊"),
    "negative_emotion":("The message carries negative emotion: anger, complaint, frustration, disappointment, or pressure.", "负面情绪"),
    "positive_emotion":("The message carries positive emotion: praise, excitement, gratitude, or friendliness.", "正面情绪"),
    "needs_reply":     ("The message clearly expects a reply from the reader (question, request, or direct address).", "需回复"),
}

SCORE_QUESTIONS: dict[str, tuple[str, list[str], str]] = {
    # key -> (instructions, criteria, chinese label)
    "emotion_valence": (
        "Rate the emotional valence of the message from the sender.",
        ["negative", "neutral", "positive"],
        "情绪极性",
    ),
    "emotion_intensity": (
        "Rate how emotionally intense the message is.",
        ["low", "medium", "high"],
        "情绪强度",
    ),
    "urgency_level": (
        "Rate how urgently the reader must respond to this message.",
        ["low", "medium", "high"],
        "紧急度",
    ),
    "importance_level": (
        "Rate how important this message is for the reader's work or relationships.",
        ["low", "medium", "high"],
        "重要度",
    ),
}

# Context-aware questions — only asked when a UserContext is supplied. These
# turn urgency from a property of the message into a property of
# (message × my current situation).
CONTEXT_QUESTIONS: dict[str, tuple[str, str]] = {
    "deadline_conflict": (
        "The deadline or time expectation in this message conflicts with my "
        "existing calendar events or todo commitments shown above, so I cannot "
        "meet it without rescheduling something.",
        "截止时间冲突",
    ),
    "blocked_now": (
        "My current or imminent schedule shown above (a meeting in progress, or "
        "one starting within the next hour) blocks me from handling this message "
        "right now.",
        "当前被日程占用",
    ),
    "can_handle_now": (
        "Considering the current time and my schedule shown above, I am free to "
        "handle this message immediately.",
        "现在有空处理",
    ),
    "already_planned": (
        "The task or topic requested in this message is already covered by one "
        "of my existing todos or calendar events shown above.",
        "已在计划中",
    ),
}

# ---------------------------------------------------------------------------
# PUA / emotional-manipulation detection
# ---------------------------------------------------------------------------
# Each is a Noul question scored 0..1. We only raise a flag when MULTIPLE
# signals fire together (see pua_risk), to avoid over-triggering on normal
# messages.
#
# IMPORTANT — instructions are kept SHORT and FREE of embedded example phrases.
# Empirically, laya-multilingual gets badly miscalibrated when an instruction
# contains quoted Chinese examples (it treats them as content cues and
# over-triggers PUA on benign messages: e.g. "谢谢" → isolation 0.63). Cloud Jev
# tolerates examples, but short clean descriptions score well on BOTH backends,
# so we use them uniformly. Verified: simplified instructions give
# "谢谢"→pua≈0.00 and "你怎么这么笨"→belittling 0.41 on local Laya.
PUA_QUESTIONS: dict[str, tuple[str, str]] = {
    "pua_belittling": (
        "The sender insults, belittles, or demeans the reader's ability or worth.",
        "贬低打压",
    ),
    "pua_guilt_trip": (
        "The sender uses guilt or a sense of obligation to manipulate the reader.",
        "制造愧疚",
    ),
    "pua_isolation": (
        "The sender tries to isolate the reader from friends or family, or implies only they truly care.",
        "孤立挑拨",
    ),
    "pua_gaslighting": (
        "The sender denies the reader's feelings or memory, or calls them overly sensitive, to make them doubt themselves.",
        "否定感受",
    ),
    "pua_love_bombing": (
        "The sender shows excessive flattery or affection, or rushes intimacy far beyond the normal pace.",
        "过度热情",
    ),
    "pua_control": (
        "The sender shows controlling behavior: monitoring, demanding reports of whereabouts, or restricting the reader.",
        "控制欲",
    ),
    "pua_intermittent": (
        "The sender is hot-and-cold or intermittently affectionate to create anxiety and dependence.",
        "忽冷忽热",
    ),
    "pua_boundary": (
        "The sender crosses boundaries with inappropriate sexual hints, demands for private information, or uncomfortable requests.",
        "越界冒犯",
    ),
    "pua_blackmail": (
        "The sender uses emotional blackmail, implying the reader's love or loyalty is conditional on compliance.",
        "情感绑架",
    ),
    "pua_compliance": (
        "The sender tests the reader's obedience with escalating demands or pressures them to prove loyalty.",
        "服从测试",
    ),
}

# ---------------------------------------------------------------------------
# Auto-reply judgment
# ---------------------------------------------------------------------------
# Scene classification (Choice) + two Noul gates. The product rule from the
# user: if auto-reply confidence > 0.85 → may auto-reply; otherwise → only with
# explicit user authorization, or as a suggestion.
AUTO_REPLY_SCENE_CRITERIA: dict[str, str] = {
    "simple_ack": "消息只需简单确认即可（收到/好的/知道了/OK），无需实质内容",
    "greeting": "打招呼或寒暄（在吗/你好/早安/忙吗），简单回应即可",
    "thanks": "对方表达感谢或客气，简单回应即可",
    "info_question": "对方询问具体信息，需要查证或思考后才能回答",
    "task_request": "对方提出任务或请求，需要承诺、协调或执行",
    "emotional": "对方在表达情绪或情感（开心/难过/生气/暧昧），需要有温度、贴切的回应",
    "sensitive": "敏感或高风险对话（冲突、谈判、金钱、亲密关系、上下级评价）",
    "complex": "内容复杂或多诉求，需要思考、权衡后才能回复",
}

AUTO_REPLY_QUESTIONS: dict[str, tuple[str, str]] = {
    "auto_reply_confidence": (
        "Can this message be safely and appropriately answered with a short, "
        "low-risk canned reply (like '收到', '好的', '在的', '不客气') WITHOUT a "
        "human needing to think, look anything up, make a decision, or handle "
        "emotional nuance? Answer yes ONLY if an automatic reply would almost "
        "certainly be correct, sufficient, and cause no harm.",
        "可自动回复置信度",
    ),
    "auto_reply_safe": (
        "Would sending a simple automatic reply to this message be SAFE — it "
        "cannot cause misunderstanding, offense, financial or work loss, "
        "missing something important, or damage the relationship?",
        "自动回复安全性",
    ),
}


# ---------------------------------------------------------------------------
# Reply suggestion rule table
# ---------------------------------------------------------------------------

# Ordered by priority. First rule whose `all_of` intents are all "yes"
# (score >= threshold) wins. Each template can use {name} / {content}.
@dataclass
class ReplyRule:
    name: str
    all_of: tuple[str, ...]
    template: str
    any_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()   # if ANY of these >= min_score, rule is skipped
    tone: str = "neutral"
    min_score: float = 0.5          # noul score required for all_of / any_of / none_of


REPLY_RULES: list[ReplyRule] = [
    # 1. broadcast — a mass announcement should never trigger a personal reply,
    #    even if it mentions a time.
    ReplyRule(
        name="broadcast",
        all_of=("broadcast",),
        template="收到。",
        tone="neutral",
    ),
    # 2. urgent_task — explicit task + explicit deadline dominates. This must
    #    come before angry_customer so "急 + 3点前" gets a time commitment,
    #    not an apology.
    ReplyRule(
        name="urgent_task",
        all_of=("task_request", "has_deadline"),
        template="收到，我{deadline_hint}处理完给你。",
        tone="commit",
    ),
    # 3. angry_customer — strong negative emotion (>=0.7) + a concrete ask.
    #    Comes before bug_report so an upset customer gets soothed, not asked
    #    for reproduction steps.
    ReplyRule(
        name="angry_customer",
        all_of=("negative_emotion",),
        any_of=("bug_or_issue", "task_request", "info_question"),
        template="抱歉让你着急了，我马上看一下，稍后同步进展。",
        tone="soothe",
        min_score=0.7,
    ),
    # 4. bug_report — technical issue without strong emotion.
    ReplyRule(
        name="bug_report",
        all_of=("bug_or_issue",),
        template="麻烦发下复现步骤和截图/日志，我这边排查。",
        tone="investigate",
    ),
    # --- Social fast-paths: these are SPECIFIC scenes and must win over the
    # --- generic info_question / task matchers below. ---
    # 5. greeting — "在吗/你好" should get "在的", not "我查一下".
    ReplyRule(
        name="greeting",
        all_of=("greeting",),
        none_of=("task_request", "has_deadline"),
        template="在的，怎么了？",
        tone="warm",
    ),
    # 6. thanks
    ReplyRule(
        name="thanks",
        all_of=("thanks",),
        template="不客气～",
        tone="warm",
    ),
    # 7. affectionate — friendly / flirty: positive emotion + casual small talk.
    ReplyRule(
        name="affectionate",
        all_of=("positive_emotion", "casual_chat"),
        template="在呢～怎么啦？",
        tone="warm",
        min_score=0.6,
    ),
    # 8. casual — plain small talk.
    ReplyRule(
        name="casual",
        all_of=("casual_chat",),
        template="哈哈，是的。",
        tone="casual",
    ),
    # --- Work scenarios ---
    ReplyRule(
        name="approval",
        all_of=("approval_request",),
        template="看到了，我现在处理。",
        tone="commit",
    ),
    ReplyRule(
        name="meeting",
        all_of=("meeting_related",),
        none_of=("casual_chat",),   # "今晚回家吃饭吗" trips meeting_related but
                                    # is really casual — don't reply like a meeting.
        template="时间我确认下，稍后回你。",
        tone="neutral",
    ),
    ReplyRule(
        name="decision",
        all_of=("decision_needed",),
        template="我看下，{deadline_hint}回你。",
        tone="commit",
    ),
    ReplyRule(
        name="info_question",
        all_of=("info_question",),
        template="我查一下，稍后回你。",
        tone="neutral",
    ),
    ReplyRule(
        name="task_no_deadline",
        all_of=("task_request",),
        template="收到，我这边安排下。",
        tone="commit",
    ),
    ReplyRule(
        name="sharing",
        all_of=("sharing_info",),
        template="收到，我看下。",
        tone="neutral",
    ),
]

FALLBACK_REPLY = "收到，我看下。"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class IntentHit:
    key: str
    label: str
    score: float

    @property
    def yes(self) -> bool:
        return self.score >= 0.5


@dataclass
class AnalysisResult:
    text: str
    top_intents: list[IntentHit]                # sorted desc, only score >= 0.4
    all_intents: dict[str, IntentHit]
    emotion_valence: str                        # "negative" | "neutral" | "positive"
    emotion_intensity: str                      # "low" | "medium" | "high"
    urgency: str                                # "low" | "medium" | "high"
    importance: str                             # "low" | "medium" | "high"
    urgency_score: float                        # 0..2 (index into criteria)
    importance_score: float
    reply_suggestion: str
    reply_rule: str
    composite_score: float                      # for sorting the radar list
    tokens: tuple[int, int]
    latency_ms: int
    # context-aware signals (all 0.0 / False when no UserContext was supplied)
    deadline_conflict: float = 0.0
    blocked_now: float = 0.0
    can_handle_now: float = 0.0
    already_planned: float = 0.0
    busy_now: bool = False
    context_used: bool = False
    # PUA / emotional-manipulation signals
    pua_signals: dict[str, float] = field(default_factory=dict)
    pua_risk: float = 0.0
    pua_level: str = "none"                       # none | low | medium | high
    pua_top: list[str] = field(default_factory=list)   # chinese labels of top signals
    # auto-reply judgment
    auto_reply_scene: str = ""
    auto_reply_confidence: float = 0.0
    auto_reply_safe: float = 0.0
    can_auto_reply: bool = False                  # confidence > 0.85 AND safe
    raw: JevResponse | None = None
    error: str | None = None

    @property
    def is_urgent(self) -> bool:
        return self.urgency == "high" or (self.urgency == "medium" and self.importance == "high")

    @property
    def needs_coordination(self) -> bool:
        """True when the right reply is to negotiate timing rather than commit."""
        return self.deadline_conflict >= 0.5 or (self.blocked_now >= 0.5 and self.urgency != "low")

    @property
    def pua_alert(self) -> bool:
        return self.pua_level in ("medium", "high")


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class MessageAnalyzer:
    """Batches all questions for one message into a single Jev call."""

    def __init__(self, client: DecisionEngine, noul_threshold: float = 0.4) -> None:
        self.client = client
        self.noul_threshold = noul_threshold

    # ------------------------------------------------------------------

    def build_questions(self, include_context: bool = False) -> dict[str, dict[str, Any]]:
        q: dict[str, dict[str, Any]] = {}
        for key, (instr, _label) in INTENT_QUESTIONS.items():
            q[key] = {"type": "noul", "instructions": instr}
        for key, (instr, criteria, _label) in SCORE_QUESTIONS.items():
            q[key] = {"type": "score", "instructions": instr, "criteria": criteria}
        # PUA / manipulation detection — always asked
        for key, (instr, _label) in PUA_QUESTIONS.items():
            q[key] = {"type": "noul", "instructions": instr}
        # Auto-reply judgment — always asked
        for key, (instr, _label) in AUTO_REPLY_QUESTIONS.items():
            q[key] = {"type": "noul", "instructions": instr}
        q["auto_reply_scene"] = {
            "type": "choice",
            "instructions": "Classify what kind of reply this message needs.",
            "criteria": AUTO_REPLY_SCENE_CRITERIA,
        }
        if include_context:
            for key, (instr, _label) in CONTEXT_QUESTIONS.items():
                q[key] = {"type": "noul", "instructions": instr}
        return q

    def build_state(
        self,
        text: str,
        sender: str = "",
        conversation: str = "",
        is_group: bool = False,
        context: UserContext | None = None,
        plain: bool = False,
    ) -> str:
        if plain:
            # Laya mode: raw message (+ context if any), no "会话/发送人/消息内容:"
            # scaffolding — that scaffolding measurably degrades Laya.
            if context is not None:
                return context.serialize() + "\n\n" + text
            return text
        parts: list[str] = []
        if context is not None:
            parts.append(context.serialize())
            parts.append("")  # visual separator before the message block
        if conversation:
            parts.append(f"会话：{conversation}（{'群聊' if is_group else '单聊'}）")
        if sender:
            parts.append(f"发送人：{sender}")
        parts.append(f"消息内容：{text!r}")
        return "\n".join(parts)

    # ------------------------------------------------------------------

    def analyze(
        self,
        text: str,
        sender: str = "",
        conversation: str = "",
        is_group: bool = False,
        context: UserContext | None = None,
    ) -> AnalysisResult:
        has_ctx = context is not None
        plain = getattr(self.client, "use_plain_state", False)
        state = self.build_state(text, sender, conversation, is_group, context, plain=plain)
        questions = self.build_questions(include_context=has_ctx)

        try:
            resp = self.client.ask(state=state, questions=questions)
        except JevError as e:
            log.warning("Jev analysis failed: %s", e)
            return self._fallback(text, error=str(e))

        return self._score(text, resp, context)

    # ------------------------------------------------------------------

    def _score(self, text: str, resp: JevResponse, context: UserContext | None = None) -> AnalysisResult:
        all_intents: dict[str, IntentHit] = {}
        for key, (_instr, label) in INTENT_QUESTIONS.items():
            ans = resp.answers.get(key)
            score = float(ans.score) if isinstance(ans, NoulAnswer) else 0.0
            all_intents[key] = IntentHit(key=key, label=label, score=score)

        def score_label(key: str, default_idx: int = 1) -> tuple[str, float]:
            ans = resp.answers.get(key)
            if isinstance(ans, ScoreAnswer):
                return ans.label or "neutral", ans.value
            return "neutral", float(default_idx)

        def noul_score(key: str) -> float:
            ans = resp.answers.get(key)
            return float(ans.score) if isinstance(ans, NoulAnswer) else 0.0

        valence, _ = score_label("emotion_valence", 1)
        intensity, _ = score_label("emotion_intensity", 0)
        urgency, urgency_val = score_label("urgency_level", 0)
        importance, importance_val = score_label("importance_level", 0)

        # Context-aware signals (0.0 when no context was supplied)
        deadline_conflict = noul_score("deadline_conflict")
        blocked_now = noul_score("blocked_now")
        can_handle_now = noul_score("can_handle_now")
        already_planned = noul_score("already_planned")
        busy_now = context.is_busy_now() if context is not None else False
        context_used = context is not None

        # PUA / manipulation + auto-reply judgment
        pua_signals, pua_risk, pua_level, pua_top = self._compute_pua(resp)
        ar_scene, ar_conf, ar_safe, can_auto = self._compute_auto_reply(resp)

        # Sort intents by score desc; keep those above threshold
        top = sorted(
            (h for h in all_intents.values() if h.score >= self.noul_threshold),
            key=lambda h: h.score,
            reverse=True,
        )

        # Composite ranking score. Urgency dominates; context + PUA adjust it:
        #   + deadline_conflict → needs active coordination, surface it
        #   + blocked_now       → busy AND urgent → quickly acknowledge & defer
        #   + pua_risk          → manipulative messages deserve attention
        #   - already_planned   → it's handled, demote
        composite = (
            urgency_val * 2.0
            + importance_val * 1.2
            + all_intents["needs_reply"].score * 0.8
            + all_intents["negative_emotion"].score * 0.6
            + all_intents["has_deadline"].score * 0.5
            + deadline_conflict * 1.0
            + blocked_now * 0.4
            + pua_risk * 0.8
        )
        if already_planned >= 0.5:
            composite -= 0.6

        reply, rule = self._pick_reply(all_intents, text, context,
                                       deadline_conflict=deadline_conflict,
                                       blocked_now=blocked_now,
                                       already_planned=already_planned,
                                       pua_level=pua_level,
                                       pua_top=pua_top)

        return AnalysisResult(
            text=text,
            top_intents=top[:6],
            all_intents=all_intents,
            emotion_valence=valence,
            emotion_intensity=intensity,
            urgency=urgency,
            importance=importance,
            urgency_score=urgency_val,
            importance_score=importance_val,
            reply_suggestion=reply,
            reply_rule=rule,
            composite_score=round(composite, 3),
            tokens=(resp.input_tokens, resp.output_tokens),
            latency_ms=resp.latency_ms,
            deadline_conflict=deadline_conflict,
            blocked_now=blocked_now,
            can_handle_now=can_handle_now,
            already_planned=already_planned,
            busy_now=busy_now,
            context_used=context_used,
            pua_signals=pua_signals,
            pua_risk=pua_risk,
            pua_level=pua_level,
            pua_top=pua_top,
            auto_reply_scene=ar_scene,
            auto_reply_confidence=ar_conf,
            auto_reply_safe=ar_safe,
            can_auto_reply=can_auto,
            raw=resp,
        )

    # ------------------------------------------------------------------

    def _compute_pua(self, resp: JevResponse) -> tuple[dict[str, float], float, str, list[str]]:
        """Aggregate the 10 PUA signals into a risk score + level + top labels.

        Uses a noisy-OR over significant signals (>= 0.5) so that multiple
        moderate flags compound, while a single weak flag stays low. This keeps
        false positives down — we only alert when a real pattern shows up.
        """
        signals: dict[str, float] = {}
        for key in PUA_QUESTIONS:
            ans = resp.answers.get(key)
            signals[key] = float(ans.score) if isinstance(ans, NoulAnswer) else 0.0

        significant = [s for s in signals.values() if s >= 0.5]
        if significant:
            prod = 1.0
            for s in significant:
                prod *= (1.0 - s)
            risk = 1.0 - prod
        else:
            risk = max(signals.values()) if signals else 0.0

        if risk >= 0.75:
            level = "high"
        elif risk >= 0.5:
            level = "medium"
        elif risk >= 0.3:
            level = "low"
        else:
            level = "none"

        top = sorted(
            ((PUA_QUESTIONS[k][1], v) for k, v in signals.items() if v >= 0.5),
            key=lambda x: x[1], reverse=True,
        )
        top_labels = [f"{label} {int(v * 100)}" for label, v in top[:4]]
        return signals, round(risk, 3), level, top_labels

    def _compute_auto_reply(self, resp: JevResponse) -> tuple[str, float, float, bool]:
        """Scene (choice) + confidence/safety (noul) → can_auto_reply gate.

        Product rule: confidence > 0.85 AND safe → may auto-reply directly.
        Below that → only with user authorization, or as a suggestion.
        """
        conf_ans = resp.answers.get("auto_reply_confidence")
        conf = float(conf_ans.score) if isinstance(conf_ans, NoulAnswer) else 0.0
        safe_ans = resp.answers.get("auto_reply_safe")
        safe = float(safe_ans.score) if isinstance(safe_ans, NoulAnswer) else 0.0

        scene = ""
        scene_ans = resp.answers.get("auto_reply_scene")
        if isinstance(scene_ans, ChoiceAnswer):
            scene = AUTO_REPLY_SCENE_CRITERIA.get(scene_ans.choice, scene_ans.choice)

        can_auto = conf > 0.85 and safe >= 0.5
        return scene, round(conf, 3), round(safe, 3), can_auto

    # ------------------------------------------------------------------

    def _pick_reply(
        self,
        intents: dict[str, IntentHit],
        text: str,
        context: UserContext | None = None,
        deadline_conflict: float = 0.0,
        blocked_now: float = 0.0,
        already_planned: float = 0.0,
        pua_level: str = "none",
        pua_top: list[str] | None = None,
    ) -> tuple[str, str]:
        pua_top = pua_top or []

        # --- PUA override (highest priority) ---
        # Never answer a manipulative message with a naive work-style reply.
        # Surface a boundary-setting suggestion instead.
        if pua_level in ("medium", "high"):
            return self._pua_reply(pua_level, pua_top), "pua_guard"

        # Deadline hint: extract a clean time phrase from the message.
        deadline_hint = _extract_deadline_hint(text) or "今天"

        has_ask = any(intents[k].score >= 0.5
                      for k in ("task_request", "has_deadline", "needs_reply",
                                "info_question", "bug_or_issue") if k in intents)

        # --- Context-aware overrides (take priority over content-only rules) ---
        # 1. Schedule conflict / blocked: negotiate timing, don't over-promise.
        if deadline_conflict >= 0.5 or (blocked_now >= 0.5 and has_ask):
            return self._coordination_reply(context, deadline_hint), "coordinate"
        # 2. Already on my plan: say so instead of treating it as new work.
        if already_planned >= 0.5:
            return "这个我已经在跟进了，有进展第一时间同步你。", "already_planned"

        # --- Content-only rule table ---
        for rule in REPLY_RULES:
            thr = rule.min_score
            # none_of: skip this rule if any excluded intent is strongly present
            if rule.none_of and any(intents[k].score >= thr for k in rule.none_of):
                continue
            if all(intents[k].score >= thr for k in rule.all_of):
                if rule.any_of and not any(intents[k].score >= thr for k in rule.any_of):
                    continue
                return rule.template.format(deadline_hint=deadline_hint), rule.name
        return FALLBACK_REPLY, "fallback"

    def _pua_reply(self, pua_level: str, pua_top: list[str]) -> str:
        """Boundary-setting suggestion for manipulative messages.

        This is deliberately NOT a ready-to-send auto-reply — PUA messages need
        the human in the loop. We surface the pattern and a calm boundary line.
        """
        signals = "、".join(s.split()[0] if " " in s else s for s in pua_top[:2]) or "操纵话术"
        if pua_level == "high":
            return (f"⚠️ 明显 PUA 倾向（{signals}）。别急着自证或道歉，冷静设边界，"
                    f"例如：「我不太接受这样的说法，我们就事论事好吗？」")
        return (f"⚠️ 这条有点 {signals} 的味道，留意。回复前先想清楚自己的边界，"
                f"不要被带着走。")

    def _coordination_reply(self, context: UserContext | None, deadline_hint: str) -> str:
        """Build a reply that negotiates timing based on my actual schedule.

        Key insight: when I'm blocked, the honest reply states WHEN I can
        actually do it, and — if the stated deadline will be missed — says so
        upfront instead of over-promising.
        """
        has_explicit_deadline = deadline_hint and deadline_hint != "今天"

        if context is not None:
            cur = context.current_event()
            if cur:
                free = datetime.fromtimestamp(cur.end_ms / 1000).strftime("%H:%M")
                base = f"我在开会（{cur.title}），{free} 结束后第一时间处理发你。"
                if has_explicit_deadline:
                    base += f"你那边{deadline_hint}要的话可能有点赶，我尽量提前，抱歉。"
                return base
            nxt = context.next_upcoming_event()
            if nxt:
                base = f"我 {nxt.fmt_range()} 有安排（{nxt.title}），之后第一时间处理发你。"
                if has_explicit_deadline:
                    base += f"{deadline_hint}应该来得及。"
                return base
        return f"我这边手头有点事，稍晚处理发你，{deadline_hint}前可以吗？"

    # ------------------------------------------------------------------

    def _fallback(self, text: str, error: str) -> AnalysisResult:
        return AnalysisResult(
            text=text,
            top_intents=[],
            all_intents={},
            emotion_valence="neutral",
            emotion_intensity="low",
            urgency="low",
            importance="low",
            urgency_score=0.0,
            importance_score=0.0,
            reply_suggestion=FALLBACK_REPLY,
            reply_rule="error_fallback",
            composite_score=0.0,
            tokens=(0, 0),
            latency_ms=0,
            error=error,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEADLINE_PATTERNS: tuple[str, ...] = (
    # 明天下午3点前 / 今天下午5点之前 / 明早9点
    r"[今明后]天(?:[上下]午|早上|晚上|中午)?\s*\d{1,2}\s*[点时:：]\s*\d{0,2}\s*(?:之?前|之?内)?",
    # 本周五下午4点 / 下周一上午10点
    r"[本这下]周[一二三四五六日天末]?(?:[上下]午|早上|晚上|中午)?\s*\d{1,2}\s*[点时:：]?\s*\d{0,2}\s*(?:之?前|之?内)?",
    # 周X下午Y点
    r"周[一二三四五六日天末](?:[上下]午|早上|晚上|中午)?\s*\d{1,2}\s*[点时:：]?\s*\d{0,2}\s*(?:之?前|之?内)?",
    # 下午3点前 / 上午10点
    r"[上下]午\s*\d{1,2}\s*[点时:：]\s*\d{0,2}\s*(?:之?前|之?内)?",
    # 3点前 / 15点前
    r"\d{1,2}\s*[点时]\s*(?:之?前|之?内)",
    # 今晚 / 明早 / 后天
    r"(?:今晚|明早|明晚|后天|大后天)",
    # N小时内 / N分钟内 / N天内
    r"\d+\s*(?:小时|分钟|天|日)(?:之?内|之?内|以内)",
    # ASAP / 尽快 / 马上 / 立刻 / 加急
    r"(?:ASAP|asap|尽快|马上|立刻|立即|加急|抓紧)",
)


def _extract_deadline_hint(text: str) -> str | None:
    """Extract a clean time/deadline phrase from the message.

    Returns None if nothing matches, in which case the reply template falls
    back to "今天".
    """
    import re
    for pat in _DEADLINE_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Batch API — analyze many messages, one Jev call each (Jev itself batches
# questions inside a single call, so this is already efficient).
# ---------------------------------------------------------------------------

def analyze_many(
    analyzer: MessageAnalyzer,
    messages: list[dict[str, Any]],
    context: UserContext | None = None,
) -> list[AnalysisResult]:
    """Analyze a list of message dicts sequentially.

    Each dict should have: `content`, optional `sender`, `conversation`, `is_group`.
    A single shared `context` (my calendar/todos/now) is folded into every call.
    Jev's per-call latency is ~1s and fan-out is free inside a call, so serial
    is fine for the ~10 unread messages a typical user has. Parallelize with
    concurrent.futures if you have more.
    """
    out: list[AnalysisResult] = []
    for m in messages:
        out.append(analyzer.analyze(
            text=m.get("content", ""),
            sender=m.get("sender", ""),
            conversation=m.get("conversation", ""),
            is_group=bool(m.get("is_group", False)),
            context=context,
        ))
    return out
