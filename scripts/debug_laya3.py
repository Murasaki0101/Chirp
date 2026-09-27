"""Isolate the STATE-FORMAT variable: same 34 batched questions, but compare
plain-message state vs the prefixed state that analyzers.build_state produces.

Run:
    .venv/bin/python scripts/debug_laya3.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import LayaEngine  # noqa: E402

MSG = "好的收到，谢谢啦！"
PROBE = ["thanks", "greeting", "pua_belittling", "pua_guilt", "pua_love_bombing",
         "pua_isolation", "casual_chat"]


def g(resp, key):
    a = resp.raw.get("answers", {}).get(key, {}) if isinstance(resp.raw, dict) else {}
    v = a.get("noul") if isinstance(a, dict) else None
    return f"{v:.3f}" if isinstance(v, float) else "None"


def main() -> int:
    e = LayaEngine()
    analyzer = MessageAnalyzer(e)
    questions = analyzer.build_questions(include_context=False)
    print(f"question set: {len(questions)} (batch_size={e.batch_size})\n")

    state_plain = MSG
    state_prefixed = analyzer.build_state(MSG, sender="对方", conversation="测试",
                                          is_group=False, context=None)
    print("带前缀 state 长这样：")
    print(repr(state_prefixed), "\n")

    for label, state in [("纯消息 state", state_plain), ("带前缀 state", state_prefixed)]:
        r = e.ask(state, questions)
        print(f"[{label}]")
        for k in PROBE:
            print(f"    {k:16} = {g(r, k)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
