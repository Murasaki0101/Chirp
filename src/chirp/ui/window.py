"""DingTalk Jev Radar — floating desktop widget.

Ranks your unread DingTalk messages by urgency using Jev (TypeSafe System One),
shows them in an always-on-top translucent panel, and lets you click a card to
jump straight into that chat in the DingTalk client.

Run:
    python main.py                # use DWS if authenticated, else Mock
    RADAR_MOCK=true python main.py # force Mock data source
"""
from __future__ import annotations

import json
from html import escape
import logging
import os
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject, Qt, QThread, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QGuiApplication, QIcon,
    QMouseEvent, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle,
    QVBoxLayout, QWidget,
)

from ..domain.analyzers import AnalysisResult, MessageAnalyzer
from ..domain.context import UserContext, load_context
from ..infrastructure.engine import engine_label, make_engine
from ..application.explainer import Explanation, Explainer
from ..infrastructure.jev_client import JevError, load_dotenv
from ..infrastructure.sources import CACHE_DIR, CacheSource, DwsSource, Message, MockSource, Source, make_source

log = logging.getLogger("radar")

APP_NAME = "吱一声"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
WATERMARK_FILE = CACHE_DIR / "watermarks.json"
WATERMARK_FILE.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Item = Message + AnalysisResult
# ---------------------------------------------------------------------------

@dataclass
class RadarItem:
    message: Message
    analysis: AnalysisResult
    explanation: Explanation | None = None


# ---------------------------------------------------------------------------
# Watermarks — "already handled up to this timestamp" per conversation
# ---------------------------------------------------------------------------

class Watermarks:
    def __init__(self, path: Path = WATERMARK_FILE) -> None:
        self.path = path
        self._data: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("watermarks load failed: %s", e)
                self._data = {}

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("watermarks save failed: %s", e)

    def is_handled(self, msg: Message) -> bool:
        wm = self._data.get(msg.conversation_id, 0)
        # If we've handled up to timestamp T, any message with ts <= T is hidden.
        # Messages with no timestamp (ts=0) are hidden once we've handled anything
        # in that conversation.
        if msg.timestamp_ms == 0:
            return wm > 0
        return msg.timestamp_ms <= wm

    def mark_handled(self, msg: Message) -> None:
        ts = msg.timestamp_ms or int(time.time() * 1000)
        cur = self._data.get(msg.conversation_id, 0)
        if ts > cur:
            self._data[msg.conversation_id] = ts
        self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()


# ---------------------------------------------------------------------------
# Background worker: fetch + analyze
# ---------------------------------------------------------------------------

class RadarWorker(QObject):
    items_ready = Signal(list)                  # 阶段1：判定完，立即显示
    explanation_ready = Signal(str, object)     # 阶段2：(message_key, Explanation) 逐条回传
    status = Signal(str)                        # human-readable status line
    error = Signal(str)

    def __init__(
        self,
        source: Source,
        analyzer: MessageAnalyzer,
        watermarks: Watermarks,
        explainer: Explainer | None = None,
        max_parallel: int = 4,
    ) -> None:
        super().__init__()
        self.source = source
        self.analyzer = analyzer
        self.watermarks = watermarks
        self.explainer = explainer
        self.max_parallel = max_parallel
        self._cancelled = False
        # LLM 模式：详细解读异步生成（阶段2，慢）；模板模式：阶段1 同步生成（快）
        self._has_llm = bool(explainer is not None and getattr(explainer, "llm", None) is not None)

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run_once(self) -> None:
        self._cancelled = False
        try:
            self.status.emit("拉取未读消息…")
            messages = self.source.fetch()
        except Exception as e:  # noqa: BLE001 — surface anything to the UI
            log.exception("fetch failed")
            self.error.emit(f"拉取失败：{e}")
            return

        if self._cancelled:
            return

        # Load my current situation (calendar/todos/now) once per refresh.
        context = load_context()

        # Filter out already-handled conversations (until a newer message arrives)
        fresh = [m for m in messages if not self.watermarks.is_handled(m) and m.content.strip()]
        self.status.emit(f"分析 {len(fresh)} 条消息…")

        # --- 阶段 1：Jev/Laya 判定（快），完成后立即显示，不等 LLM ---
        items: list[RadarItem] = []
        if fresh:
            with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                futures = {pool.submit(self._analyze_one, m, context): m for m in fresh}
                for fut in as_completed(futures):
                    if self._cancelled:
                        break
                    m = futures[fut]
                    try:
                        items.append(fut.result())
                    except Exception as e:  # noqa: BLE001
                        log.warning("analyze failed for %s: %s", m.conversation_name, e)

        items.sort(key=lambda it: it.analysis.composite_score, reverse=True)
        self.items_ready.emit(items)   # 立即渲染（LLM 模式下 explanation 仍为 None → 转圈）

        stamp = datetime.now().strftime('%H:%M:%S')
        if not items:
            self.status.emit(f"就绪 · {stamp} · 0 个会话待处理")
            return

        # --- 阶段 2：LLM 详细解读（异步，逐条回传更新 UI）---
        if self._has_llm:
            total = len(items)
            self.status.emit(f"⏳ LLM 生成详细解读中… 0/{total}")
            done = 0
            for it in items:
                if self._cancelled:
                    break
                try:
                    expl = self.explainer.explain(
                        it.message.content, it.analysis, context,
                        sender=it.message.sender_name,
                        conversation=it.message.conversation_name,
                    )
                    self.explanation_ready.emit(it.message.key, expl)
                except Exception as e:  # noqa: BLE001
                    log.warning("LLM explain failed for %s: %s", it.message.conversation_name, e)
                done += 1
                self.status.emit(f"⏳ LLM 生成详细解读中… {done}/{total}")
        self.status.emit(f"就绪 · {datetime.now().strftime('%H:%M:%S')} · {len(items)} 个会话待处理")

    def _analyze_one(self, m: Message, context: UserContext | None = None) -> RadarItem:
        result = self.analyzer.analyze(
            text=m.content,
            sender=m.sender_name,
            conversation=m.conversation_name,
            is_group=m.is_group,
            context=context,
        )
        explanation = None
        # 模板模式（无 LLM）：同步生成解读（快），随阶段1 一起显示，UI 标注"仅供参考"。
        # LLM 模式：此处留空，由阶段2 异步生成，UI 先显示转圈。
        if self.explainer is not None and not self._has_llm:
            try:
                explanation = self.explainer.explain(
                    m.content, result, context,
                    sender=m.sender_name, conversation=m.conversation_name,
                )
            except Exception as e:  # noqa: BLE001 — interpretation is best-effort
                log.warning("template explain failed for %s: %s", m.conversation_name, e)
        return RadarItem(message=m, analysis=result, explanation=explanation)


# ---------------------------------------------------------------------------
# Card widget
# ---------------------------------------------------------------------------

URGENCY_STYLE = {
    "high":   ("#b42318", "紧急"),
    "medium": ("#946200", "较急"),
    "low":    ("#62606c", "常规"),
}
EMOJI_VALENCE = {"negative": "😠", "neutral": "😐", "positive": "🙂"}


class CardWidget(QFrame):
    clicked = Signal(object)     # emits the RadarItem
    dismissed = Signal(object)   # emits the RadarItem

    def __init__(self, item: RadarItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        m, a = self.item.message, self.item.analysis
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        # Row 1: conversation name + urgency badge + dismiss X
        top = QHBoxLayout()
        top.setSpacing(8)

        name = QLabel(f"{'🐹🐹 ' if m.is_group else '🐹 '}{m.conversation_name}")
        name.setObjectName("cardTitle")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setWordWrap(True)
        name.setFont(QFont("PingFang SC", 13, QFont.Weight.DemiBold))
        top.addWidget(name)

        if m.unread_count > 0:
            unread = QLabel(f"{m.unread_count} 未读")
            unread.setObjectName("unreadPill")
            top.addWidget(unread)

        top.addStretch(1)

        # PUA warning badge — prominent list-level signal
        if a.pua_alert:
            pua = QLabel("🚨 PUA")
            pua.setToolTip(f"操纵话术风险 {a.pua_level}（{', '.join(a.pua_top)}）")
            pua.setStyleSheet(
                "color:#b42318; background:#fff0ee; border-radius:8px;"
                " padding:1px 6px; font-size:10px; font-weight:700;"
            )
            top.addWidget(pua)

        # Auto-reply badge
        if a.can_auto_reply:
            ar = QLabel("🤖 可自动回")
            ar.setToolTip(f"自动回复置信 {a.auto_reply_confidence:.2f} / 安全 {a.auto_reply_safe:.2f}")
            ar.setStyleSheet(
                "color:#176347; background:#eaf6ef; border-radius:8px;"
                " padding:1px 6px; font-size:10px; font-weight:600;"
            )
            top.addWidget(ar)

        urg_color, urg_label = URGENCY_STYLE.get(a.urgency, URGENCY_STYLE["low"])
        urg = QLabel(f"● {urg_label}")
        urg.setObjectName("urgencyBadge")
        urg.setStyleSheet(f"color: {urg_color}; font-weight: 600;")
        top.addWidget(urg)

        close = QPushButton("✕")
        close.setObjectName("cardClose")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("忽略（不跳转）")
        close.clicked.connect(lambda: self.dismissed.emit(self.item))
        top.addWidget(close)

        root.addLayout(top)

        # Row 2: sender + time
        meta = QHBoxLayout()
        meta.setSpacing(6)
        if m.sender_name and m.is_group:
            sender = QLabel(f"@{m.sender_name}")
            sender.setObjectName("cardMeta")
            meta.addWidget(sender)
        if m.timestamp_ms:
            ts = QLabel(_humanize_ts(m.timestamp_ms))
            ts.setObjectName("cardMeta")
            meta.addWidget(ts)
        meta.addStretch(1)
        emo = QLabel(EMOJI_VALENCE.get(a.emotion_valence, "😐"))
        emo.setToolTip(f"情绪：{a.emotion_valence} / 强度 {a.emotion_intensity}")
        meta.addWidget(emo)
        root.addLayout(meta)

        # Row 3: message excerpt (list dimension — keep it to ~2 lines)
        content = QLabel(m.content)
        content.setObjectName("cardContent")
        content.setTextFormat(Qt.TextFormat.PlainText)
        content.setWordWrap(True)
        content.setMaximumHeight(64)
        root.addWidget(content)

        # Row 4: one-line interpretation, or a "generating" hint while the LLM works
        self.summary_label = QLabel()
        self.summary_label.setObjectName("cardSummary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setMaximumHeight(54)
        root.addWidget(self.summary_label)
        self.refresh_summary()

    # ------------------------------------------------------------------

    def refresh_summary(self) -> None:
        e = self.item.explanation
        if e is None:
            self.summary_label.setText("🔄 详细解读生成中…")
        else:
            tag = "（仅供参考）" if e.source == "jev-template" else ""
            self.summary_label.setText(f"💡 {e.summary}{tag}")

    def update_explanation(self, expl) -> None:
        """Called when the async LLM interpretation arrives."""
        self.item.explanation = expl
        self.refresh_summary()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            # Ignore clicks on the close button — it handles itself.
            child = self.childAt(ev.position().toPoint())
            if isinstance(child, QPushButton) and child.objectName() == "cardClose":
                return super().mousePressEvent(ev)
            self.clicked.emit(self.item)
        super().mousePressEvent(ev)


# ---------------------------------------------------------------------------
# Detail view — the "analysis dimension"
# ---------------------------------------------------------------------------

class ReplyButton(QPushButton):
    """A reply button whose height follows its wrapped label."""

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()

    def heightForWidth(self, width):
        return self.layout().heightForWidth(width)


class DetailWidget(QFrame):
    """Full deep-read of one message: interpretation, signals, reply options."""
    back = Signal()
    jump = Signal(object)       # RadarItem
    dismiss = Signal(object)    # RadarItem

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item: RadarItem | None = None
        self.setObjectName("detail")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("detailHeader")
        header.setMinimumHeight(48)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(8, 0, 10, 0)
        hl.setSpacing(6)
        self.btn_back = QPushButton("← 返回")
        self.btn_back.setObjectName("backBtn")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(self.back.emit)
        hl.addWidget(self.btn_back)
        self.title = QLabel("")
        self.title.setObjectName("detailTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setFont(QFont("PingFang SC", 13, QFont.Weight.Bold))
        hl.addWidget(self.title)
        hl.addStretch(1)
        outer.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("detailScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("detailBody")
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(18, 16, 18, 20)
        self.body_lay.setSpacing(12)
        self.body_lay.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

    # ------------------------------------------------------------------

    def set_item(self, item: RadarItem) -> None:
        changed = self.item is None or self.item.message.key != item.message.key
        self.item = item
        self._render()
        if changed:
            self.scroll.verticalScrollBar().setValue(0)

    def _ins(self, w: QWidget) -> None:
        """Insert a widget above the trailing stretch."""
        self.body_lay.insertWidget(self.body_lay.count() - 1, w)

    def _render(self) -> None:
        self._stop_spinner()
        self._spinner_label = None
        while self.body_lay.count() > 1:
            it = self.body_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        if self.item is None:
            return
        m, a, e = self.item.message, self.item.analysis, self.item.explanation
        self.title.setText(f"{'👥' if m.is_group else '👤'} {m.conversation_name}")

        # 1) original message
        orig = QLabel(m.content)
        orig.setObjectName("detailMsg")
        orig.setTextFormat(Qt.TextFormat.PlainText)
        orig.setWordWrap(True)
        orig.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._ins(orig)

        # 2) metrics
        metrics = QLabel(
            f"紧急 {a.urgency} · 重要 {a.importance} · 综合 {a.composite_score:.1f} · "
            f"情绪 {a.emotion_valence}/{a.emotion_intensity}"
            + (f" · {m.sender_name}" if m.sender_name else "")
        )
        metrics.setObjectName("detailMetrics")
        metrics.setWordWrap(True)
        self._ins(metrics)

        # 3) PUA alert box
        if a.pua_alert:
            box = QLabel(f"🚨 PUA 风险 {a.pua_level}（{a.pua_risk:.0%}）\n"
                         f"话术信号：{', '.join(a.pua_top) or '—'}")
            box.setObjectName("puaBox")
            box.setWordWrap(True)
            self._ins(box)

        # 4) interpretation lines — or a spinner while the LLM is generating
        if e is None:
            self._spinner_label = QLabel("⠋ 详细解读生成中…")
            self._spinner_label.setObjectName("detailSpinner")
            self._ins(self._spinner_label)
            self._start_spinner()
        else:
            self._stop_spinner()
            src_tag = "（仅供参考）" if e.source == "jev-template" else ""
            head = QLabel(f"🔍 详细解读{src_tag}")
            head.setObjectName("sectionHead")
            self._ins(head)
            for label, val in e.as_lines():
                row = QLabel(f"<b style='color:#875034'>{escape(label)}</b>　{escape(val)}")
                row.setObjectName("detailLine")
                row.setWordWrap(True)
                row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self._ins(row)

        # 5) intent chips
        if a.top_intents:
            chips = QLabel("意图：" + "  ".join(
                f"{h.label}{int(h.score * 100)}" for h in a.top_intents[:6]))
            chips.setObjectName("detailChips")
            chips.setWordWrap(True)
            self._ins(chips)

        # 6) context badges
        if a.context_used:
            ctx = []
            if a.deadline_conflict >= 0.5:
                ctx.append("⚠ 截止冲突")
            if a.blocked_now >= 0.5 or a.busy_now:
                ctx.append("🕐 你在忙")
            if a.already_planned >= 0.5:
                ctx.append("✓ 已在计划")
            if a.can_handle_now >= 0.5:
                ctx.append("🟢 现在有空")
            if ctx:
                cl = QLabel("处境：" + "　".join(ctx))
                cl.setObjectName("detailCtx")
                cl.setWordWrap(True)
                self._ins(cl)

        # 7) auto-reply judgment
        ar_state = "🤖 可自动回复" if a.can_auto_reply else "👤 建议人工回复"
        ar = QLabel(f"{ar_state} · 场景：{a.auto_reply_scene or '—'} · "
                    f"置信 {a.auto_reply_confidence:.2f} / 安全 {a.auto_reply_safe:.2f}")
        ar.setObjectName("detailAuto")
        ar.setWordWrap(True)
        self._ins(ar)

        # 8) reply options (click to copy)
        replies = (e.suggested_replies if e else []) or \
                  ([a.reply_suggestion] if a.reply_suggestion else [])
        if replies:
            rh = QLabel("🐹 鼠鼠帮你接话 · 点击复制")
            rh.setObjectName("sectionHead")
            self._ins(rh)
            for r in replies[:3]:
                btn = ReplyButton()
                btn.setObjectName("replyOption")
                policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                policy.setHeightForWidth(True)
                btn.setSizePolicy(policy)
                reply_lay = QVBoxLayout(btn)
                reply_lay.setContentsMargins(12, 10, 12, 10)
                reply_label = QLabel(r)
                reply_label.setTextFormat(Qt.TextFormat.PlainText)
                reply_label.setWordWrap(True)
                reply_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                reply_lay.addWidget(reply_label)
                btn.setAccessibleName(r)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _=False, t=r: self._copy(t))
                self._ins(btn)

        # 9) actions
        action_bar = QWidget()
        action_bar.setObjectName("detailActions")
        actions = QHBoxLayout(action_bar)
        actions.setContentsMargins(0, 4, 0, 0)
        actions.setSpacing(8)
        jump_btn = QPushButton("📂 跳转钉钉")
        jump_btn.setObjectName("actionPrimary")
        jump_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        jump_btn.clicked.connect(lambda: self.jump.emit(self.item))
        actions.addWidget(jump_btn)
        if a.can_auto_reply:
            auto_btn = QPushButton("🤖 一键回复")
            auto_btn.setObjectName("actionAuto")
            auto_btn.setToolTip("复制建议回复并打开钉钉，由你确认发送")
            auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            auto_btn.clicked.connect(self._auto_reply)
            actions.addWidget(auto_btn)
        ign_btn = QPushButton("忽略")
        ign_btn.setObjectName("actionGhost")
        ign_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ign_btn.clicked.connect(lambda: self.dismiss.emit(self.item))
        actions.addWidget(ign_btn)
        actions.addStretch(1)
        self._ins(action_bar)

    # ------------------------------------------------------------------

    # --- spinner animation while the LLM generates the interpretation ---
    def _start_spinner(self) -> None:
        if not hasattr(self, "_spinner_timer"):
            self._spinner_timer = QTimer(self)
            self._spinner_timer.setInterval(100)
            self._spinner_timer.timeout.connect(self._tick_spinner)
            self._spinner_idx = 0
        self._spinner_timer.start()

    def _stop_spinner(self) -> None:
        if hasattr(self, "_spinner_timer"):
            self._spinner_timer.stop()

    def _tick_spinner(self) -> None:
        frames = "⠋⠙⠺⠸⠼⠴⠦⠧⠇⠏"
        label = getattr(self, "_spinner_label", None)
        if label is not None:
            self._spinner_idx = (getattr(self, "_spinner_idx", 0) + 1) % len(frames)
            label.setText(f"{frames[self._spinner_idx]} 详细解读生成中…")

    def update_explanation(self, expl) -> None:
        """Async LLM interpretation arrived — re-render the detail with it."""
        if self.item is not None:
            self.item.explanation = expl
        self._render()

    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.title.setText("吱！已复制到剪贴板 🐾")
        QTimer.singleShot(1200, lambda: self.title.setText(
            f"{'👥' if self.item and self.item.message.is_group else '👤'} "
            f"{self.item.message.conversation_name if self.item else ''}"))

    def _auto_reply(self) -> None:
        if self.item and self.item.explanation and self.item.explanation.suggested_replies:
            self._copy(self.item.explanation.suggested_replies[0])
        self.jump.emit(self.item)


def _humanize_ts(ts_ms: int) -> str:
    if ts_ms <= 0:
        return ""
    diff = (time.time() * 1000 - ts_ms) / 1000
    if diff < 60:
        return f"{int(diff)}秒前"
    if diff < 3600:
        return f"{int(diff / 60)}分钟前"
    if diff < 86400:
        return f"{int(diff / 3600)}小时前"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class RadarWindow(QWidget):
    def __init__(
        self,
        source: Source,
        analyzer: MessageAnalyzer,
        poll_interval_s: int = 30,
        explainer: Explainer | None = None,
        engine: object | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.analyzer = analyzer
        self.engine = engine
        self.explainer = explainer if explainer is not None else Explainer()
        self.watermarks = Watermarks()
        self.poll_interval_s = poll_interval_s
        self._items: list[RadarItem] = []
        self._cards: dict = {}
        self._drag_offset: tuple[int, int] | None = None
        self._poll_paused = False

        self._setup_window()
        self._setup_ui()
        self._setup_worker()
        self._setup_timer()

        # Kick off first fetch shortly after show
        QTimer.singleShot(300, self._trigger_refresh)

    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(APP_DIR / "assets" / "icon.png")))
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAutoFillBackground(True)
        self.setObjectName("replyWindow")
        self.resize(480, 680)
        self.setMinimumSize(420, 360)

        # Place at top-right of the primary screen by default
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 24, geo.top() + 40)

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        panel = QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # --- Title bar ---
        title_bar = QFrame()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(48)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(12, 0, 8, 0)
        tb.setSpacing(6)

        title = QLabel(f"🐹 {APP_NAME}")
        title.setObjectName("appTitle")
        title.setFont(QFont("PingFang SC", 13, QFont.Weight.Bold))
        tb.addWidget(title)
        tb.addStretch(1)

        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setObjectName("titleBtn")
        self.btn_settings.setToolTip("设置（引擎开关 / API Key / LLM）")
        self.btn_settings.clicked.connect(self._open_settings)
        tb.addWidget(self.btn_settings)

        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setObjectName("titleBtn")
        self.btn_refresh.setToolTip("立即刷新")
        self.btn_refresh.clicked.connect(self._trigger_refresh)
        tb.addWidget(self.btn_refresh)

        self.btn_pause = QPushButton("⏸")
        self.btn_pause.setObjectName("titleBtn")
        self.btn_pause.setToolTip("暂停/继续自动轮询")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._on_pause_toggled)
        tb.addWidget(self.btn_pause)

        btn_clear = QPushButton("🗑")
        btn_clear.setObjectName("titleBtn")
        btn_clear.setToolTip("清空所有已隐藏标记（让所有未读重新出现）")
        btn_clear.clicked.connect(self._on_clear_watermarks)
        tb.addWidget(btn_clear)

        btn_min = QPushButton("—")
        btn_min.setObjectName("titleBtn")
        btn_min.setToolTip("收起")
        btn_min.clicked.connect(self._toggle_collapse)
        tb.addWidget(btn_min)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("titleBtn")
        btn_close.setToolTip("退出")
        btn_close.clicked.connect(self.close)
        tb.addWidget(btn_close)

        lay.addWidget(title_bar)

        # --- Stacked views: list (dimension 1) <-> detail (dimension 2) ---
        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")

        # Page 0 — list view: scrollable cards + empty state
        list_page = QWidget()
        list_page.setObjectName("listPage")
        lp_lay = QVBoxLayout(list_page)
        lp_lay.setContentsMargins(0, 0, 0, 0)
        lp_lay.setSpacing(0)

        self.inbox_summary = QLabel("待回复")
        self.inbox_summary.setObjectName("inboxSummary")
        lp_lay.addWidget(self.inbox_summary)
        self.source_notice = QLabel()
        self.source_notice.setObjectName("sourceNotice")
        self.source_notice.setWordWrap(True)
        lp_lay.addWidget(self.source_notice)
        mouse_note = QLabel("🐾 消息慢慢看，鼠鼠陪你回。")
        mouse_note.setObjectName("mouseNote")
        lp_lay.addWidget(mouse_note)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("scrollArea")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_host.setObjectName("listHost")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(16, 12, 16, 16)
        self.list_lay.setSpacing(12)
        self.list_lay.addStretch(1)
        self.scroll.setWidget(self.list_host)
        lp_lay.addWidget(self.scroll, 1)

        self.empty = QLabel("🐹 暂无待处理会话")
        self.empty.setObjectName("emptyState")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lp_lay.addWidget(self.empty)

        self.stack.addWidget(list_page)        # index 0 = list

        # Page 1 — detail view
        self.detail = DetailWidget()
        self.detail.back.connect(self._show_list)
        self.detail.jump.connect(self._on_detail_jump)
        self.detail.dismiss.connect(self._on_detail_dismiss)
        self.stack.addWidget(self.detail)      # index 1 = detail

        lay.addWidget(self.stack, 1)
        self.empty.hide()

        # --- Status bar ---
        self.status_bar = QLabel("启动中…")
        self.status_bar.setObjectName("statusBar")
        self.status_bar.setFixedHeight(32)
        self.status_bar.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.status_bar)

        self._collapsed = False
        self._apply_stylesheet()
        self._update_inbox_summary()

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
        QWidget { color: #302d38; font-family: "PingFang SC"; font-size: 13px; }
        #replyWindow, #panel { background: #faf7f2; }
        #panel { border: 1px solid #dfd5c9; border-radius: 16px; }
        QLabel { background: transparent; }
        #titleBar { background: #f3e8da; border-bottom: 1px solid #e2d4c3;
                    border-top-left-radius: 16px; border-top-right-radius: 16px; }
        #appTitle { color: #543c2e; font-size: 17px; font-weight: 700; }
        #titleBtn { background: transparent; color: #67574d; border: none;
                    border-radius: 8px; padding: 5px 7px; font-size: 15px; }
        #titleBtn:hover { background: #e5d6c5; color: #302d38; }
        #titleBtn:checked { background: #f4d9a9; color: #704813; }
        #titleBtn:disabled { color: #958b82; }
        #stack, #listPage, #listHost, #detail, #detailBody,
        #scrollArea, #detailScroll, QScrollArea > QWidget { background: #faf7f2; }
        #inboxSummary { color: #4b382c; font-size: 16px; font-weight: 700; padding: 16px 18px 4px; }
        #sourceNotice { color: #72604f; font-size: 12px; padding: 2px 18px 8px; }
        #mouseNote { color: #8a5438; font-size: 12px; padding: 0 18px 8px; }
        #emptyState { color: #716052; font-size: 15px; padding: 40px 20px; }
        #statusBar { color: #655a50; font-size: 12px; padding: 0 14px;
                     background: #f2eadf; border-top: 1px solid #e2d7c9; }
        #card { background: #ffffff; border-radius: 16px; border: 1px solid #e1d3c3; }
        #card:hover { background: #fffaf3; border: 1px solid #bd8c60; }
        #cardTitle { color: #392f2a; font-size: 15px; font-weight: 600; }
        #cardMeta { color: #756b63; font-size: 12px; }
        #cardContent { color: #302d38; font-size: 14px; }
        #unreadPill { background: #ffede1; color: #9b481e; border-radius: 8px;
                      padding: 3px 7px; font-size: 11px; font-weight: 600; }
        #cardClose { background: transparent; color: #786b60; border: none; border-radius: 5px; }
        #cardClose:hover { background: #ffe9e5; color: #a32b22; }
        #cardSummary { color: #735b45; font-size: 12px; }
        #detailHeader { background: #f3e8da; border-bottom: 1px solid #e2d4c3; }
        #backBtn { background: #ffffff; color: #65452d; border: 1px solid #d8c4ac;
                   border-radius: 8px; padding: 5px 10px; }
        #backBtn:hover { background: #fff3df; }
        #detailTitle { color: #392f2a; font-size: 15px; }
        #detailMsg { color: #302d38; background: #ffffff; font-size: 15px;
                     border-radius: 10px; padding: 12px; border-left: 4px solid #b8885b; }
        #detailMetrics { color: #70665c; font-size: 12px; }
        #puaBox { color: #a02920; background: #fff0eb; border: 1px solid #e8b4a4;
                  border-radius: 10px; padding: 10px; font-size: 13px; font-weight: 600; }
        #sectionHead { color: #59402e; font-size: 14px; font-weight: 700; padding-top: 4px; }
        #detailLine { color: #3f3935; font-size: 14px; }
        #detailSpinner { color: #81552f; font-size: 13px; padding: 8px 0; }
        #detailChips { color: #725237; font-size: 12px; }
        #detailCtx { color: #835a12; font-size: 12px; }
        #detailAuto { color: #286347; font-size: 12px; }
        #detailActions { background: #faf7f2; }
        #replyOption { background: #fff6e9; color: #55402e; border: 1px solid #dec7a7;
                       border-radius: 12px; text-align: left; }
        #replyOption QLabel { color: #55402e; background: transparent; font-size: 14px; }
        #replyOption:hover { background: #ffecd1; border-color: #b98c57; }
        #replyOption:pressed { background: #f7dfbe; }
        #actionPrimary, #actionAuto, #actionGhost { border-radius: 9px; padding: 9px 12px;
                                                    font-size: 13px; font-weight: 600; }
        #actionPrimary { background: #825638; color: #ffffff; border: 1px solid #825638; }
        #actionPrimary:hover { background: #684128; }
        #actionAuto { background: #e8f3e9; color: #285c3c; border: 1px solid #afcbb2; }
        #actionAuto:hover { background: #d6e9d8; }
        #actionGhost { background: #ffffff; color: #665749; border: 1px solid #d6c9b9; }
        #actionGhost:hover { background: #f2e7d9; }
        QScrollBar:vertical { background: #f2eadf; width: 8px; margin: 0; }
        QScrollBar::handle:vertical { background: #b7a28b; border-radius: 4px; min-height: 30px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QDialog, QMenu { background: #faf7f2; color: #302d38; }
        QLineEdit, QSpinBox { background: #ffffff; color: #302d38; border: 1px solid #c7b7a5;
                             border-radius: 5px; padding: 4px; }
        """)

    # ------------------------------------------------------------------

    def _setup_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = RadarWorker(self.source, self.analyzer, self.watermarks, self.explainer)
        self._worker.moveToThread(self._thread)
        self._worker.items_ready.connect(self._on_items_ready)
        self._worker.explanation_ready.connect(self._on_explanation_ready)
        self._worker.status.connect(self._on_status)
        self._worker.error.connect(self._on_error)
        self._thread.started.connect(lambda: None)  # worker runs on demand via invokeMethod
        self._thread.start()

    def _setup_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(self.poll_interval_s * 1000)
        self._timer.timeout.connect(self._trigger_refresh)
        self._timer.start()

    # ------------------------------------------------------------------

    def _trigger_refresh(self) -> None:
        if self._poll_paused and self.sender() is self._timer:
            return
        self.status_bar.setText("刷新中…")
        self.btn_refresh.setEnabled(False)
        # Run in worker thread via a queued signal-style call
        from PySide6.QtCore import QMetaObject, Qt as _Qt, Q_ARG
        QMetaObject.invokeMethod(
            self._worker, "run_once",
            _Qt.ConnectionType.QueuedConnection,
        )

    def _on_items_ready(self, items: list) -> None:
        self._items = items
        self._render()
        self.btn_refresh.setEnabled(True)

    def _on_status(self, text: str) -> None:
        self.status_bar.setText(text)
        if "就绪" in text:
            self.btn_refresh.setEnabled(True)

    def _on_error(self, text: str) -> None:
        self.status_bar.setText(f"⚠ {text}")
        self.btn_refresh.setEnabled(True)

    # ------------------------------------------------------------------

    def _render(self) -> None:
        self._update_inbox_summary()
        # clear existing cards
        while self.list_lay.count() > 1:  # keep trailing stretch
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

        self._cards = {}
        if not self._items:
            self.empty.show()
            self.scroll.hide()
            return
        self.empty.hide()
        self.scroll.show()

        for it in self._items:
            card = CardWidget(it)
            card.clicked.connect(self._on_card_clicked)
            card.dismissed.connect(self._on_card_dismissed)
            self.list_lay.insertWidget(self.list_lay.count() - 1, card)
            self._cards[it.message.key] = card

    def _update_inbox_summary(self) -> None:
        counts: dict[str, int] = {}
        for item in self._items:
            msg = item.message
            counts[msg.conversation_id] = max(counts.get(msg.conversation_id, 0), msg.unread_count)
        self.inbox_summary.setText(f"待回复  {len(counts)} 个会话 · {sum(counts.values())} 条未读")
        if isinstance(self.source, CacheSource):
            stamp = getattr(self.source, "fetched_at", "")
            try:
                stamp = datetime.fromisoformat(stamp).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                stamp = "时间未知"
            self.source_notice.setText(f"本地快照 · {stamp}，非实时未读\n每个会话展示最新一条消息；实时同步需连接钉钉。")
        elif isinstance(self.source, MockSource):
            self.source_notice.setText("演示消息 · 每个会话展示最新一条消息")
        else:
            self.source_notice.setText("钉钉未读 · 含免打扰会话 · 每个会话展示最新一条消息")

    def _on_explanation_ready(self, key: str, expl) -> None:
        """Async LLM interpretation arrived for one message — update in place
        (no full re-render, so the list doesn't flicker or lose scroll position)."""
        for it in self._items:
            if it.message.key == key:
                it.explanation = expl
                break
        card = getattr(self, "_cards", {}).get(key)
        if card is not None:
            card.update_explanation(expl)
        # If the detail view is currently showing this item, refresh it too.
        if (self.stack.currentIndex() == 1 and self.detail.item is not None
                and self.detail.item.message.key == key):
            self.detail.update_explanation(expl)

    # ------------------------------------------------------------------

    def _on_card_clicked(self, item: RadarItem) -> None:
        # List dimension → detail dimension
        self.detail.set_item(item)
        self.stack.setCurrentIndex(1)

    def _show_list(self) -> None:
        self.stack.setCurrentIndex(0)

    def _open_dingtalk(self, item: RadarItem) -> None:
        link = item.message.deep_link
        log.info("open deep link: %s", link)
        opened = QDesktopServices.openUrl(QUrl(link))
        if not opened:
            try:
                webbrowser.open(link)
            except Exception as e:  # noqa: BLE001
                log.warning("open failed: %s", e)

    def _on_detail_jump(self, item: RadarItem) -> None:
        if item is None:
            return
        self._open_dingtalk(item)
        self.watermarks.mark_handled(item.message)
        self._remove_item(item)
        self._show_list()

    def _on_detail_dismiss(self, item: RadarItem) -> None:
        if item is None:
            return
        self.watermarks.mark_handled(item.message)
        self._remove_item(item)
        self._show_list()

    def _on_card_dismissed(self, item: RadarItem) -> None:
        self.watermarks.mark_handled(item.message)
        self._remove_item(item)

    def _remove_item(self, item: RadarItem) -> None:
        key = item.message.key
        self._items = [x for x in self._items if x.message.key != key]
        self._render()

    # ------------------------------------------------------------------

    def _on_pause_toggled(self, paused: bool) -> None:
        self._poll_paused = paused
        if paused:
            self._timer.stop()
            self.status_bar.setText("已暂停自动刷新")
        else:
            self._timer.start()
            self._trigger_refresh()

    def _on_clear_watermarks(self) -> None:
        self.watermarks.clear()
        self.status_bar.setText("已清空所有隐藏标记，重新拉取…")
        self._trigger_refresh()

    def _open_settings(self) -> None:
        from .settings import SettingsDialog
        dlg = SettingsDialog(self)
        if not dlg.exec():
            return
        # Hot-reload engine + analyzer + explainer from the updated .env
        try:
            prefer_mock = os.environ.get("RADAR_MOCK", "").lower() in ("1", "true", "yes")
            self.engine = make_engine("mock" if prefer_mock else None)
            self.analyzer = MessageAnalyzer(self.engine)
            self._worker.analyzer = self.analyzer
            self.explainer = Explainer()          # re-reads LLM config from env
            self._worker.explainer = self.explainer
            self._worker._has_llm = self.explainer.llm is not None
            self.source = make_source(prefer_mock=prefer_mock)
            self._worker.source = self.source
            new_poll = int(os.environ.get("RADAR_POLL_INTERVAL", "30"))
            self.poll_interval_s = new_poll
            self._timer.setInterval(new_poll * 1000)
            self.status_bar.setText(f"设置已保存 · {engine_label(self.engine)} · 重新分析…")
            self._show_list()
            self._trigger_refresh()
        except Exception as e:  # noqa: BLE001
            log.exception("reload after settings failed")
            self.status_bar.setText(f"⚠ 重载失败：{e}")

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self.stack.setVisible(not self._collapsed)
        self.setMinimumHeight(80 if self._collapsed else 360)
        self.resize(self.width(), 80 if self._collapsed else 680)

    # ------------------------------------------------------------------

    # Drag-to-move on the title bar area
    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and ev.position().y() <= 48:
            self._drag_offset = (int(ev.globalPosition().x() - self.x()),
                                  int(ev.globalPosition().y() - self.y()))
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag_offset is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(int(ev.globalPosition().x()) - self._drag_offset[0],
                       int(ev.globalPosition().y()) - self._drag_offset[1])
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    # Right-click menu
    def contextMenuEvent(self, ev) -> None:
        menu = QMenu(self)
        act_refresh = QAction("立即刷新", self)
        act_refresh.triggered.connect(self._trigger_refresh)
        menu.addAction(act_refresh)

        act_pause = QAction("暂停轮询" if not self._poll_paused else "继续轮询", self)
        act_pause.triggered.connect(lambda: self.btn_pause.toggle())
        menu.addAction(act_pause)

        act_clear = QAction("清空隐藏标记", self)
        act_clear.triggered.connect(self._on_clear_watermarks)
        menu.addAction(act_clear)

        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_quit)

        menu.exec(ev.globalPos())

    def closeEvent(self, ev) -> None:
        try:
            self._worker.cancel()
            self._timer.stop()
            self._thread.quit()
            if not self._thread.wait(5000):
                log.warning("worker thread did not finish in 5s, terminating")
                self._thread.terminate()
                self._thread.wait(1000)
        except Exception as e:  # noqa: BLE001
            log.warning("shutdown error: %s", e)
        super().closeEvent(ev)
