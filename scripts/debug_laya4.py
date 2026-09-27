"""Find the batch size + state format that keeps Laya calibrated.

Loads the model once, then sweeps batch_size over the full 34-question set
using the PLAIN message as state, watching the PUA probes that previously
over-triggered.

Run:
    .venv/bin/python scripts/debug_laya4.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import LayaEngine  # noqa: E402

MSG = "好的收到，谢谢啦！"     # ground truth: thanks high, ALL pua ~0
PROBE = ["thanks", "pua_belittling", "pua_guilt", "pua_love_bombing",
         "pua_isolation", "pua_blackmail", "casual_chat"]


def g(resp, key):
    a = resp.raw.get("answers", {}).get(key, {}) if isinstance(resp.raw, dict) else {}
    v = a.get("noul") if isinstance(a, dict) else None
    return f"{v:.3f}" if isinstance(v, float) else " None "


def main() -> int:
    e = LayaEngine()
    e._ensure_loaded()
    analyzer = MessageAnalyzer(e)
    questions = analyzer.build_questions(include_context=False)
    print(f"34-question set, PLAIN state, sweeping batch_size\n")
    print(f"{'batch':>6} | " + " | ".join(f"{k[:12]:>12}" for k in PROBE))
    print("-" * (10 + 15 * len(PROBE)))
    for bs in (2, 3, 4, 6, 8):
        e.batch_size = bs
        r = e.ask(MSG, questions)
        row = " | ".join(f"{g(r, k):>12}" for k in PROBE)
        print(f"{bs:>6} | {row}")
    print("\n期望：thanks≈0.99，所有 pua_*≈0.00，casual_chat 低")
    return 0


if __name__ == "__main__":
    sys.exit(main())
