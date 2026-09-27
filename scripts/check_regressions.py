"""Offline regressions and screenshots: QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/check_regressions.py"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from chirp.domain.analyzers import MessageAnalyzer
from chirp.application.explainer import Explanation
from chirp.infrastructure.jev_client import MockJevClient
from chirp.ui.window import RadarItem, RadarWindow
from chirp.infrastructure.sources import CacheSource, DwsSource, MockSource


class PreviewWindow(RadarWindow):
    def _setup_worker(self):
        pass

    def _setup_timer(self):
        self._timer = QTimer(self)

    def _trigger_refresh(self):
        pass

    def closeEvent(self, event):
        QWidget.closeEvent(self, event)


class SourceTests(unittest.TestCase):
    def test_hydration_keeps_each_conversation_and_unread_count(self):
        source = DwsSource()
        summaries = [source._parse_conversation({
            "conversationId": f"chat-{i}", "name": f"会话{i}",
            "unreadCount": i + 1, "type": "direct",
        }) for i in range(3)]
        def latest(cid, is_group):
            return source._parse_message({"messageId": cid, "text": "你好"}, cid, is_group)
        with patch.object(source, "_fetch_unread_chats", return_value=summaries), \
                patch.object(source, "_fetch_latest_message", side_effect=latest):
            messages = source.fetch()
        self.assertEqual([m.conversation_name for m in messages], ["会话0", "会话1", "会话2"])
        self.assertEqual([m.unread_count for m in messages], [1, 2, 3])
        self.assertFalse(source.exclude_muted)

    def test_connection_failure_is_not_reported_as_empty_inbox(self):
        source = DwsSource()
        with patch.object(source, "_run", return_value=None), self.assertRaises(RuntimeError):
            source.fetch()

    def test_snapshot_retains_fetch_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inbox.json"
            path.write_text(json.dumps({"fetched_at": "2026-09-27T14:10:00+08:00", "messages": []}))
            source = CacheSource(path)
            self.assertEqual(source.fetch(), [])
            self.assertEqual(source.fetched_at, "2026-09-27T14:10:00+08:00")


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        analyzer = MessageAnalyzer(MockJevClient())
        source = MockSource()
        self.win = PreviewWindow(source, analyzer)
        self.items = []
        for msg in source.fetch():
            analysis = analyzer.analyze(msg.content, sender=msg.sender_name)
            explanation = Explanation(
                summary="对方有一件事情想和你确认，可以先回应，再一起安排时间。",
                subtext="可能希望尽快收到回应。", emotion_read="语气自然，保持轻松就好。",
                motive="确认安排。", relationship_signal="日常沟通。",
                advice="按自己的节奏回复，不必急着承诺。",
                suggested_replies=["吱！收到啦，我看一下 🐹", "我先确认一下安排，稍后回复你。", "这段时间我正在处理手头的事情，等我忙完之后再仔细看一下，然后给你一个完整的回复，好吗？"],
            )
            self.items.append(RadarItem(msg, analysis, explanation))
        self.win._on_items_ready(self.items)
        self.win.show()
        self.app.processEvents()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_repeated_detail_refresh_does_not_stack_buttons(self):
        self.win._on_card_clicked(self.items[0])
        detail = self.win.detail
        for _ in range(20):
            detail.update_explanation(self.items[0].explanation)
        self.app.processEvents()
        # Even before deferred deletion, only the current actions may be visible.
        visible = [w for w in detail.findChildren(QWidget, "detailActions") if not w.isHidden()]
        self.assertEqual(len(visible), 1)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(len(detail.findChildren(QPushButton, "actionPrimary")), 1)
        self.assertEqual(len(detail.findChildren(QPushButton, "actionGhost")), 1)
        self.assertEqual(len(detail.findChildren(QPushButton, "replyOption")), 3)
        reply = detail.findChildren(QPushButton, "replyOption")[-1]
        label = reply.findChild(QLabel)
        self.assertGreater(label.height(), label.fontMetrics().height())
        reply.click()
        self.assertEqual(self.app.clipboard().text(), self.items[0].explanation.suggested_replies[-1])
        self.assertFalse(self.win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.win.resize(480, 820)
        self.app.processEvents()
        detail.scroll.verticalScrollBar().setValue(detail.scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.win.grab().save(str(Path(__file__).resolve().parent.parent / "artifacts/detail-preview.png"))

    def test_list_count_distinguishes_chats_and_unread_messages(self):
        self.assertIn("6 个会话 · 20 条未读", self.win.inbox_summary.text())
        self.assertEqual(len(self.win._cards), 6)
        self.win._items = self.items[:1]
        self.win._render()
        self.app.processEvents()
        self.assertIn("1 个会话 · 2 条未读", self.win.inbox_summary.text())
        self.win.grab().save(str(Path(__file__).resolve().parent.parent / "artifacts/inbox-preview.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
