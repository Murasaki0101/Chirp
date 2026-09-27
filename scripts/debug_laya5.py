"""Final Laya diagnosis: do SIMPLER instructions (no embedded Chinese examples)
restore Laya's discrimination? Compare analyzers' verbose instructions vs short
ones, on a message that should be PUA-free vs one that IS PUA.

Run:
    .venv/bin/python scripts/debug_laya5.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.infrastructure.engine import LayaEngine  # noqa: E402

# Short, example-free instructions
SIMPLE = {
    "pua_belittling":   {"type": "noul", "instructions": "The sender insults or demeans the reader."},
    "pua_isolation":    {"type": "noul", "instructions": "The sender tries to isolate the reader from other people."},
    "pua_love_bombing": {"type": "noul", "instructions": "The sender shows excessive affection or flattery."},
    "pua_guilt":        {"type": "noul", "instructions": "The sender uses guilt to control the reader."},
    "thanks":           {"type": "noul", "instructions": "The message expresses thanks."},
}

# Verbose instructions WITH Chinese examples (like analyzers.py uses)
VERBOSE = {
    "pua_belittling":   {"type": "noul", "instructions": "The sender belittles, demeans, or undermines the reader's value, ability, or appearance (e.g. '你这样不行', '除了我没人受得了你', '你怎么这么笨', '就你这水平')."},
    "pua_isolation":    {"type": "noul", "instructions": "The sender tries to isolate the reader from friends or family, or implies that only they truly care about / understand the reader (e.g. '别听你朋友的', '只有我是真心对你好')."},
    "pua_love_bombing": {"type": "noul", "instructions": "The sender shows excessive flattery, affection, or rushes intimacy far beyond the normal pace of the relationship (love bombing), e.g. very early '你是我的唯一', over-the-top praise, pushing to commit fast."},
    "pua_guilt":        {"type": "noul", "instructions": "The sender induces guilt or obligation by emphasizing their own sacrifices or what the reader supposedly 'owes' them, in order to make the reader comply (e.g. '我为你付出这么多', '你对得起我吗')."},
    "thanks":           {"type": "noul", "instructions": "The message expresses thanks or appreciation."},
}

MSGS = [
    ("应无PUA", "好的收到，谢谢啦！"),
    ("真PUA", "你怎么这么笨，除了我没人受得了你，也就我还愿意搭理你。"),
]


def g(resp, key):
    a = resp.raw.get("answers", {}).get(key, {}) if isinstance(resp.raw, dict) else {}
    v = a.get("noul") if isinstance(a, dict) else None
    return f"{v:.3f}" if isinstance(v, float) else " None "


def main() -> int:
    e = LayaEngine()
    e._ensure_loaded()
    keys = ["thanks", "pua_belittling", "pua_isolation", "pua_love_bombing", "pua_guilt"]
    for qlabel, Q in [("简化 instructions", SIMPLE), ("含中文例子(analyzers)", VERBOSE)]:
        print(f"\n===== {qlabel} =====")
        print(f"{'消息':<10} | " + " | ".join(f"{k[:13]:>13}" for k in keys))
        for tag, msg in MSGS:
            r = e.ask(msg, Q)
            print(f"{tag:<10} | " + " | ".join(f"{g(r, k):>13}" for k in keys))
    print("\n期望：『应无PUA』行 pua_* 全低；『真PUA』行 pua_belittling/isolation 高")
    return 0


if __name__ == "__main__":
    sys.exit(main())
