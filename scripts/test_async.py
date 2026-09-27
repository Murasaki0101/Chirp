"""Validate the async two-phase rendering:
  Phase 1: Jev/Laya judgment done → items_ready fires IMMEDIATELY (explanation=None)
  Phase 2: LLM interpretation arrives later → explanation_ready fires per item

Uses MockEngine (instant judgment) + the real configured LLM (slow), so the
two phases are clearly separated in time.

Run:
    .venv/bin/python scripts/test_async.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import make_engine  # noqa: E402
from chirp.application.explainer import Explainer  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv  # noqa: E402
from chirp.ui.window import RadarWindow  # noqa: E402
from chirp.infrastructure.sources import MockSource  # noqa: E402

T0 = time.time()


def ts() -> str:
    return f"+{time.time() - T0:5.2f}s"


def main() -> int:
    load_dotenv()
    msg = {
        "conversation_id": "cidASYNC", "conversation_name": "老王", "is_group": False,
        "sender_name": "老王", "content": "在吗？下午3点前把上周的销售数据发我，老板要看，急。",
        "timestamp_ms": int(time.time() * 1000), "unread_count": 1, "message_id": "mASYNC",
    }
    source = MockSource([msg])
    engine = make_engine("mock")            # instant judgment
    analyzer = MessageAnalyzer(engine)
    explainer = Explainer()                 # real LLM if configured
    print(f"{ts()} LLM configured: {explainer.llm is not None}")

    app = QApplication(sys.argv)
    win = RadarWindow(source=source, analyzer=analyzer, poll_interval_s=999,
                      explainer=explainer, engine=engine)

    def on_items(items):
        states = ["None(转圈)" if it.explanation is None else it.explanation.source for it in items]
        print(f"{ts()} [阶段1] items_ready: {len(items)} 条, explanation={states}")

    def on_expl(key, e):
        print(f"{ts()} [阶段2] explanation_ready: source={e.source}")
        print(f"          summary: {e.summary}")
        print(f"          replies: {e.suggested_replies[:2]}")

    win._worker.items_ready.connect(on_items)
    win._worker.explanation_ready.connect(on_expl)
    win.show()

    QTimer.singleShot(25000, lambda: (win.close(), app.quit()))
    app.exec()
    print(f"{ts()} done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
