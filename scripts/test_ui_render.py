"""Render-test both UI dimensions without waiting on Jev.

Constructs a RadarItem with ALL signals fired (PUA high, auto-replyable,
schedule conflict, rich intents) so every branch of CardWidget (list dimension)
and DetailWidget (analysis dimension) gets exercised. Uses MockJevClient for a
fast, deterministic base analysis, then enriches the fields.

Run:
    .venv/bin/python scripts/test_ui_render.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("RADAR_MOCK", "true")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chirp.domain.analyzers import IntentHit, MessageAnalyzer  # noqa: E402
from chirp.domain.context import CalendarEvent, TodoItem, UserContext  # noqa: E402
from chirp.application.explainer import Explainer  # noqa: E402
from chirp.infrastructure.jev_client import MockJevClient  # noqa: E402
from chirp.ui.window import CardWidget, DetailWidget, RadarItem  # noqa: E402
from chirp.infrastructure.sources import Message  # noqa: E402


def build_rich_item() -> RadarItem:
    now = int(time.time() * 1000)
    msg = Message(
        conversation_id="cidTEST",
        conversation_name="测试对象",
        is_group=False,
        sender_name="对方",
        sender_id="sTEST",
        content="你不回我消息就是不在乎我，我为你付出了那么多你都看不到吗？",
        timestamp_ms=now - 120_000,
        unread_count=2,
        message_id="msgTEST",
        deep_link="dingtalk://dingtalkclient/action/open_conversation?conversation_id=cidTEST",
    )
    analyzer = MessageAnalyzer(MockJevClient())
    a = analyzer.analyze(msg.content, sender="对方", conversation="测试对象")

    # Force every signal so all UI branches render
    a.pua_level = "high"
    a.pua_risk = 0.97
    a.pua_top = ["制造愧疚 97", "情感绑架 94", "服从测试 81"]
    a.can_auto_reply = True
    a.auto_reply_confidence = 0.91
    a.auto_reply_safe = 0.83
    a.auto_reply_scene = "敏感或高风险对话"
    a.deadline_conflict = 0.82
    a.blocked_now = 0.76
    a.can_handle_now = 0.1
    a.already_planned = 0.2
    a.context_used = True
    a.busy_now = True
    a.urgency = "high"
    a.importance = "high"
    a.emotion_valence = "negative"
    a.emotion_intensity = "high"
    a.composite_score = 9.3
    a.top_intents = [
        IntentHit("negative_emotion", "负面情绪", 0.97),
        IntentHit("needs_reply", "需回复", 0.95),
        IntentHit("pua_blackmail", "情感绑架", 0.94),
    ]

    # A context with an in-progress meeting (exercises coordination reply)
    now_dt = time.localtime()
    ctx = UserContext(
        now_ms=now,
        today_events=[CalendarEvent("ev1", "产品评审会", now - 3600_000, now + 3600_000)],
        next_event=None,
        due_today_todos=[TodoItem("t1", "提交周报", now + 7200_000, 30)],
        open_todos=[TodoItem("t2", "回复客户邮件", 0, 20)],
    )

    explainer = Explainer(llm=None)  # force template mode
    e = explainer._explain_template(msg.content, a, ctx)
    return RadarItem(message=msg, analysis=a, explanation=e)


def main() -> int:
    app = QApplication(sys.argv)
    item = build_rich_item()

    card = CardWidget(item)
    card.setWindowTitle("列表维度 / List dimension")
    card.resize(360, 160)
    card.show()

    detail = DetailWidget()
    detail.set_item(item)
    detail.setWindowTitle("详细分析维度 / Detail dimension")
    detail.resize(380, 600)
    detail.show()

    print("[render] CardWidget + DetailWidget shown; auto-closing in 4s")
    QTimer.singleShot(4000, app.quit)
    rc = app.exec()
    print(f"[render] OK (rc={rc}) — both dimensions rendered without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
