"""Detailed message interpretation — the "军师 / 恋爱大师" deep-read layer.

Jev gives us fast structured judgments (intent / emotion / PUA / urgency /
auto-reply). This module turns those into a human-readable deep interpretation
of what the other person REALLY means, plus a few reply options.

Two modes
---------
1. LLM mode (preferred): if `RADAR_LLM_BASE_URL` / `RADAR_LLM_API_KEY` /
   `RADAR_LLM_MODEL` are set, a fast generative model writes the interpretation
   and reply options (OpenAI-compatible /chat/completions). The user said they'd
   configure a fast model — this is the slot for it.
2. Jev-template mode (fallback): assemble an interpretation from the structured
   Jev signals using templates. Always works, no extra dependency, but less
   nuanced than a real generative model.

Both return the same `Explanation` structure so the UI doesn't care which ran.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..domain.analyzers import AnalysisResult
from ..domain.context import UserContext
from ..infrastructure.jev_client import load_dotenv

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Explanation:
    summary: str                          # 一句话核心解读
    subtext: str                          # 潜台词 / 言外之意
    emotion_read: str                     # 对方情绪状态解读
    motive: str                           # 动机推测
    relationship_signal: str              # 关系信号（亲密/事务/操纵/礼貌…）
    advice: str                           # 应对建议
    suggested_replies: list[str] = field(default_factory=list)  # 回复选项
    source: str = "jev-template"          # "llm" | "jev-template"
    raw: str = ""                         # LLM 原始返回（debug 用）

    def as_lines(self) -> list[tuple[str, str]]:
        """For UI rendering: (label, text) pairs, skipping empties."""
        out = []
        for label, val in (
            ("核心解读", self.summary),
            ("潜台词", self.subtext),
            ("对方情绪", self.emotion_read),
            ("动机推测", self.motive),
            ("关系信号", self.relationship_signal),
            ("应对建议", self.advice),
        ):
            if val:
                out.append((label, val))
        return out


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible) — the slot for the user's "fast model"
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 45.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # DashScope qwen3 defaults to a slow "thinking" mode; for short reply
        # generation we want the fast non-thinking path. Most OpenAI-compatible
        # endpoints simply ignore unknown fields, so this is safe to send.
        self.extra_body = extra_body if extra_body is not None else {"enable_thinking": False}

    def chat(self, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
        }
        payload.update(self.extra_body)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def make_llm_client() -> LLMClient | None:
    """Build an LLM client from env, or None if not configured."""
    load_dotenv()
    base = os.environ.get("RADAR_LLM_BASE_URL")
    key = os.environ.get("RADAR_LLM_API_KEY")
    model = os.environ.get("RADAR_LLM_MODEL")
    if base and key and model:
        log.info("LLM explainer enabled: model=%s base=%s", model, base)
        return LLMClient(base, key, model)
    log.info("LLM explainer not configured — using Jev-template mode "
             "(set RADAR_LLM_BASE_URL/API_KEY/MODEL to enable)")
    return None


# ---------------------------------------------------------------------------
# Explainer
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一个精通人际沟通与情感洞察的「军师」。用户会给你一条别人发给他的即时消息，"
    "以及一份由快速判断模型产出的结构化信号（意图/情绪/紧急度/PUA风险/场景）。"
    "你的任务：深度解读这条消息的真实含义，并给出可直接使用的回复选项。\n"
    "要求：\n"
    "1. 洞察潜台词，不要只复述字面意思。每个字段简洁，summary 不超过 40 字。\n"
    "2. 如果 PUA/操纵风险高，明确指出话术类型并提醒设立边界，不要建议讨好式回复。\n"
    "3. 结合用户的日程/忙碌处境，回复建议要现实可行。\n"
    "4. 只输出 JSON，字段：summary, subtext, emotion_read, motive, "
    "relationship_signal, advice, suggested_replies。\n"
    "5. suggested_replies 是 2-3 个可直接发送的回复，每个形如 "
    '{"tone":"语气标签(如 专业冷静/温和/带边界感)","content":"回复正文"}。'
)


class Explainer:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm if llm is not None else make_llm_client()

    # ------------------------------------------------------------------

    def explain(
        self,
        message_text: str,
        analysis: AnalysisResult,
        context: UserContext | None = None,
        sender: str = "",
        conversation: str = "",
    ) -> Explanation:
        if self.llm is not None:
            try:
                return self._explain_llm(message_text, analysis, context, sender, conversation)
            except Exception as e:  # noqa: BLE001 — any LLM failure falls back
                log.warning("LLM explain failed (%s) — falling back to template", e)
        return self._explain_template(message_text, analysis, context)

    # ------------------------------------------------------------------

    def _explain_llm(
        self,
        message_text: str,
        analysis: AnalysisResult,
        context: UserContext | None,
        sender: str,
        conversation: str,
    ) -> Explanation:
        signals = self._signals_brief(analysis)
        ctx_brief = context.serialize() if context else "（无日程上下文）"
        user_prompt = (
            f"【我的处境】\n{ctx_brief}\n\n"
            f"【会话】{conversation or '单聊'}  【发送人】{sender or '对方'}\n"
            f"【消息原文】{message_text}\n\n"
            f"【Jev 结构化信号】\n{signals}\n\n"
            f"请深度解读并给出回复选项，只输出 JSON。"
        )
        assert self.llm is not None
        raw = self.llm.chat(SYSTEM_PROMPT, user_prompt)
        parsed = _parse_json_loose(raw)
        return Explanation(
            summary=str(parsed.get("summary", "")).strip(),
            subtext=str(parsed.get("subtext", "")).strip(),
            emotion_read=str(parsed.get("emotion_read", "")).strip(),
            motive=str(parsed.get("motive", "")).strip(),
            relationship_signal=str(parsed.get("relationship_signal", "")).strip(),
            advice=str(parsed.get("advice", "")).strip(),
            suggested_replies=_normalize_replies(parsed.get("suggested_replies")),
            source="llm",
            raw=raw,
        )

    def _signals_brief(self, a: AnalysisResult) -> str:
        intents = ", ".join(f"{h.label}={h.score:.2f}" for h in a.top_intents[:6])
        pua = ", ".join(a.pua_top) if a.pua_top else "无"
        return (
            f"紧急度={a.urgency}, 重要度={a.importance}, 综合分={a.composite_score:.2f}\n"
            f"情绪={a.emotion_valence}/{a.emotion_intensity}\n"
            f"意图: {intents}\n"
            f"PUA风险={a.pua_level}({a.pua_risk:.2f}), 信号: {pua}\n"
            f"场景={a.auto_reply_scene}, 可自动回复={a.can_auto_reply}"
            f"(置信{a.auto_reply_confidence:.2f}/安全{a.auto_reply_safe:.2f})\n"
            f"处境信号: 截止冲突={a.deadline_conflict:.2f}, 被占用={a.blocked_now:.2f}, "
            f"已在计划={a.already_planned:.2f}"
        )

    # ------------------------------------------------------------------

    def _explain_template(
        self,
        message_text: str,
        a: AnalysisResult,
        context: UserContext | None,
    ) -> Explanation:
        """Assemble a readable interpretation purely from Jev signals."""
        top = {h.key: h.score for h in a.top_intents}
        label_of = {h.key: h.label for h in a.top_intents}

        # --- summary ---
        main_intent = a.top_intents[0].label if a.top_intents else "表达"
        summary = f"对方主要在「{main_intent}」，情绪偏{ _CN_VALENCE.get(a.emotion_valence, a.emotion_valence) }。"

        # --- subtext ---
        subtext = _infer_subtext(top, label_of, a)

        # --- emotion read ---
        emo = _CN_VALENCE.get(a.emotion_valence, a.emotion_valence)
        intensity = {"low": "平和", "medium": "明显", "high": "强烈"}.get(a.emotion_intensity, "")
        emotion_read = f"情绪{intensity}，整体偏{emo}。"
        if a.emotion_valence == "negative" and a.emotion_intensity == "high":
            emotion_read += "对方此刻情绪上头，先安抚再讲理。"

        # --- motive ---
        motive = _infer_motive(top, a)

        # --- relationship signal ---
        signal = _infer_relationship_signal(a)

        # --- advice ---
        advice = _infer_advice(a, context)

        # --- reply options ---
        replies = _build_reply_options(a)

        return Explanation(
            summary=summary,
            subtext=subtext,
            emotion_read=emotion_read,
            motive=motive,
            relationship_signal=signal,
            advice=advice,
            suggested_replies=replies,
            source="jev-template",
        )


_CN_VALENCE = {"negative": "负面", "neutral": "中性", "positive": "正面"}


# ---------------------------------------------------------------------------
# Template inference helpers
# ---------------------------------------------------------------------------

def _infer_subtext(top: dict[str, float], label_of: dict[str, str], a: AnalysisResult) -> str:
    if a.pua_alert:
        sig = "、".join(a.pua_top[:2]) if a.pua_top else "操纵话术"
        return f"字面之下是{sig}的倾向——通过让你自我怀疑或愧疚来影响你，并非就事论事。"
    if top.get("has_deadline", 0) >= 0.5 and top.get("task_request", 0) >= 0.5:
        return "潜台词是「这件事优先级很高，我希望你立刻放下手头的事来处理」。"
    if top.get("needs_reply", 0) >= 0.6 and top.get("casual_chat", 0) >= 0.5:
        return "潜台词更可能是「我想找你聊两句 / 希望你回应我」，内容本身不重要。"
    if top.get("info_question", 0) >= 0.6:
        return "潜台词是「我需要一个确定的答复来推进我自己的事」。"
    if top.get("positive_emotion", 0) >= 0.6:
        return "潜台词是友好的、带善意的，可能在拉近关系。"
    if top.get("broadcast", 0) >= 0.5:
        return "这是群发/通知，潜台词是「知会一下」，不针对你个人。"
    return "暂无明显言外之意，按字面理解即可。"


def _infer_motive(top: dict[str, float], a: AnalysisResult) -> str:
    if a.pua_alert:
        return "试图影响或控制你的情绪/行为，让你顺着对方。"
    if top.get("task_request", 0) >= 0.5:
        return "希望你完成某件具体的事。"
    if top.get("info_question", 0) >= 0.5:
        return "想从你这里获取信息或确认。"
    if top.get("attention_seeking", 0) >= 0.5 or (top.get("needs_reply", 0) >= 0.6 and top.get("casual_chat", 0) >= 0.5):
        return "想获得你的关注或陪伴。"
    if top.get("thanks", 0) >= 0.5:
        return "表达感谢，维护关系。"
    if top.get("sharing_info", 0) >= 0.5:
        return "分享信息，同步进展。"
    return "一般性沟通。"


def _infer_relationship_signal(a: AnalysisResult) -> str:
    if a.pua_level == "high":
        return "🚨 操纵/PUA 信号强烈——这段沟通不健康，注意保护自己。"
    if a.pua_level == "medium":
        return "⚠️ 有操纵话术苗头，留意对方的沟通模式。"
    pos = a.all_intents.get("positive_emotion")
    neg = a.all_intents.get("negative_emotion")
    casual = a.all_intents.get("casual_chat")
    if pos and pos.score >= 0.6 and casual and casual.score >= 0.5:
        return "💛 友好/亲密信号，对方愿意和你轻松互动。"
    if neg and neg.score >= 0.6:
        return "❗ 负面情绪信号，关系此刻有张力。"
    if a.all_intents.get("work_coord") and a.all_intents["work_coord"].score >= 0.5:
        return "💼 事务性/工作关系，就事论事即可。"
    return "➖ 中性，常规沟通。"


def _infer_advice(a: AnalysisResult, context: UserContext | None) -> str:
    if a.pua_alert:
        return "不要急着自证或道歉。冷静、简短地设立边界，必要时减少回应频率。"
    if a.needs_coordination:
        if context and context.current_event():
            return "你正在忙，先快速回应「在开会，稍后处理」，避免对方空等，也别硬承诺做不到的时间。"
        return "你当前分身乏术，先给对方一个明确的时间预期，而不是含糊的「马上」。"
    if a.can_auto_reply:
        return "这条可以放心用一句简单话术自动/快速回复，不必费神。"
    if a.urgency == "high":
        return "优先处理。先回一句确认收到，给出明确时间点，再去执行。"
    if a.all_intents.get("needs_reply") and a.all_intents["needs_reply"].score >= 0.6:
        return "对方在等你回应，别已读不回；哪怕简短回一句也好。"
    return "常规处理即可，不必紧急。"


def _build_reply_options(a: AnalysisResult) -> list[str]:
    """Give 2-3 reply options at different tones, anchored on the rule reply."""
    opts: list[str] = []
    main = a.reply_suggestion
    if main:
        opts.append(main)
    if a.pua_alert:
        opts.append("我们好好说话，这样的表达我不太舒服。")
        opts.append("我需要一点空间，晚点再聊。")
    elif a.can_auto_reply:
        if "不客气" not in main and "在的" not in main:
            opts.append("收到～")
        opts.append("好嘞，没问题。")
    elif a.needs_coordination:
        opts.append("稍等，我这边忙完马上回你。")
    elif a.urgency == "high":
        opts.append("收到，我马上处理，有进展同步你。")
    else:
        if a.emotion_valence == "positive":
            opts.append("哈哈好呀～")
        else:
            opts.append("好的，我看下。")
    # de-dup, keep order, max 3
    seen = set()
    out = []
    for o in opts:
        if o and o not in seen:
            seen.add(o)
            out.append(o)
        if len(out) >= 3:
            break
    return out


def _normalize_replies(raw_replies: Any) -> list[str]:
    """Normalize suggested_replies into a list of display strings.

    The LLM may return plain strings OR {"tone":..., "content":...} objects.
    We flatten the latter to "[tone] content" so the UI shows the style hint
    while keeping a copyable reply body.
    """
    out: list[str] = []
    for x in (raw_replies or []):
        if isinstance(x, dict):
            content = str(x.get("content") or x.get("reply") or x.get("text") or "").strip()
            tone = str(x.get("tone") or x.get("style") or "").strip()
            if not content:
                continue
            out.append(f"[{tone}] {content}" if tone else content)
        elif x is not None:
            s = str(x).strip()
            if s and s.lower() != "none":
                out.append(s)
    return out


def _parse_json_loose(raw: str) -> dict[str, Any]:
    """Parse JSON that may be wrapped in ```json fences or have stray text."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # try direct
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # try to grab the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            pass
    log.warning("could not parse LLM JSON, returning empty. raw=%r", raw[:200])
    return {}
