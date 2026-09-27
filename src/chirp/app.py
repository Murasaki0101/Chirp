"""Application bootstrap and dependency wiring."""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtWidgets import QApplication

from chirp.domain.analyzers import MessageAnalyzer
from chirp.infrastructure.engine import engine_label, make_engine
from chirp.infrastructure.jev_client import load_dotenv
from chirp.infrastructure.sources import make_source
from chirp.ui.window import APP_NAME, RadarWindow

log = logging.getLogger("radar")


def _setup_logging() -> None:
    level = os.environ.get("RADAR_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    """Create the configured application and enter the Qt event loop."""
    _setup_logging()
    load_dotenv()

    prefer_mock = os.environ.get("RADAR_MOCK", "").lower() in ("1", "true", "yes")
    poll_interval = int(os.environ.get("RADAR_POLL_INTERVAL", "30"))
    source = make_source(prefer_mock=prefer_mock)
    engine = make_engine("mock" if prefer_mock else None)
    analyzer = MessageAnalyzer(engine)

    log.info(
        "Source: %s | Engine: %s | poll=%ds",
        type(source).__name__, engine_label(engine), poll_interval,
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(True)

    window = RadarWindow(
        source=source,
        analyzer=analyzer,
        poll_interval_s=poll_interval,
        engine=engine,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
