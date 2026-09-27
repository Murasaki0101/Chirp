"""Diagnose: does question COUNT break Laya? Compare 6 vs 34 questions on the
same message, and test whether raising max_len fixes it.

Run:
    .venv/bin/python scripts/debug_laya2.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import LayaEngine  # noqa: E402

MSG = "好的收到，谢谢啦！"   # should be: thanks high, PUA ~0
SMALL = {
    "thanks": {"type": "noul", "instructions": "The message expresses thanks or appreciation."},
    "pua_belittling": {"type": "noul", "instructions": "The sender belittles or demeans the reader."},
    "pua_guilt": {"type": "noul", "instructions": "The sender induces guilt to manipulate."},
}


def noul(resp, key):
    a = resp.raw.get("answers", {}).get(key, {}) if isinstance(resp.raw, dict) else {}
    return a.get("noul") if isinstance(a, dict) else None


def main() -> int:
    e = LayaEngine()
    analyzer = MessageAnalyzer(e)
    big = analyzer.build_questions(include_context=False)
    print(f"big question set size: {len(big)}\n")

    print(f"MSG: {MSG}\n")

    r_small = e.ask(MSG, SMALL)
    print("[3 题]  thanks=%.4f  pua_belittling=%.4f  pua_guilt=%.4f" % (
        noul(r_small, "thanks") or -1,
        noul(r_small, "pua_belittling") or -1,
        noul(r_small, "pua_guilt") or -1))

    r_big = e.ask(MSG, big)
    print("[34 题] thanks=%.4f  pua_belittling=%.4f  pua_guilt=%.4f  pua_blackmail=%.4f" % (
        noul(r_big, "thanks") or -1,
        noul(r_big, "pua_belittling") or -1,
        noul(r_big, "pua_guilt") or -1,
        noul(r_big, "pua_blackmail") or -1))

    # Try raising max_len on the big set
    try:
        import laya  # noqa: F401
        e._ensure_loaded()
        out = e._agent.predict(MSG, big, max_len=2048)
        ans = out.get("answers", {})
        def g(k):
            v = ans.get(k, {})
            return v.get("noul") if isinstance(v, dict) else None
        print("[34 题 max_len=2048] thanks=%.4f  pua_belittling=%.4f  pua_guilt=%.4f" % (
            g("thanks") or -1, g("pua_belittling") or -1, g("pua_guilt") or -1))
    except Exception as ex:  # noqa: BLE001
        print(f"max_len test failed: {ex}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
