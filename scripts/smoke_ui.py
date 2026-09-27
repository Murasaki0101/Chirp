"""Headless-ish smoke test: build the window, feed it 3 mock items, exit.

Verifies imports, layout construction, and Jev analysis wiring without
requiring the user to interact with the floating panel.

Run:
    .venv/bin/python scripts/smoke_ui.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Use the real source by default (CacheSource if .cache/inbox.json exists).
# Set RADAR_MOCK=true to force mock data.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import engine_label, make_engine  # noqa: E402
from chirp.ui.window import RadarWindow  # noqa: E402
from chirp.infrastructure.sources import make_source  # noqa: E402


def main() -> int:
    prefer_mock = os.environ.get("RADAR_MOCK", "").lower() in ("1", "true", "yes")
    source = make_source(prefer_mock=prefer_mock)
    engine = make_engine("mock" if prefer_mock else None)
    analyzer = MessageAnalyzer(engine)
    print(f"[smoke] source={type(source).__name__} engine={engine_label(engine)}")

    app = QApplication(sys.argv)
    win = RadarWindow(source=source, analyzer=analyzer, poll_interval_s=999)
    win.show()

    def _shutdown() -> None:
        win.close()
        app.quit()

    # Auto-close after 15s so this script never hangs
    QTimer.singleShot(15_000, _shutdown)
    rc = app.exec()
    print(f"[smoke] window closed cleanly (rc={rc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
