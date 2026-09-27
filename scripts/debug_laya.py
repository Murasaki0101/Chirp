"""Debug: inspect RAW Laya output to diagnose PUA over-triggering.

Prints laya's unprocessed answers so we can tell whether the high scores come
from the model itself (calibration/capability) or from our parsing.

Run:
    .venv/bin/python scripts/debug_laya.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.infrastructure.engine import LayaEngine  # noqa: E402

QUESTIONS = {
    "thanks":         {"type": "noul", "instructions": "The message expresses thanks or appreciation."},
    "greeting":       {"type": "noul", "instructions": "The message is a greeting like 'hi' or '在吗'."},
    "pua_belittling": {"type": "noul", "instructions": "The sender belittles or demeans the reader's ability or worth."},
    "pua_guilt":      {"type": "noul", "instructions": "The sender induces guilt or obligation to manipulate the reader."},
    "task_request":   {"type": "noul", "instructions": "The message asks the reader to do a specific task."},
    "has_deadline":   {"type": "noul", "instructions": "The message specifies a deadline or time constraint."},
}

MSGS = [
    "好的收到，谢谢啦！",
    "你怎么这么笨，除了我没人受得了你。",
    "下午3点前把报告发我，急。",
]


def main() -> int:
    e = LayaEngine()
    print("loading laya (cached)...")
    for msg in MSGS:
        print("=" * 64)
        print("MSG:", msg)
        r = e.ask(msg, QUESTIONS)
        raw_answers = r.raw.get("answers", {}) if isinstance(r.raw, dict) else {}
        for k in QUESTIONS:
            v = raw_answers.get(k, {})
            noul = v.get("noul") if isinstance(v, dict) else v
            print(f"  {k:16} raw_noul={noul}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
