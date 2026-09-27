"""Validate the deep-interpretation layer (explainer.py).

Runs analyze + explain on a few representative messages and prints the full
Explanation. Uses the Jev-template mode unless RADAR_LLM_* is configured.

Run:
    .venv/bin/python scripts/test_explainer.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.domain.context import load_context  # noqa: E402
from chirp.infrastructure.engine import engine_label, make_engine  # noqa: E402
from chirp.application.explainer import Explainer  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv  # noqa: E402

CASES = [
    ("情感绑架", "你不回我消息就是不在乎我，我为你付出了那么多你都看不到吗？"),
    ("暧昧", "在干嘛呢？突然有点想你[害羞]"),
    ("紧急任务", "下午3点前把上周的销售数据发我，老板要看，急。"),
]


def main() -> int:
    load_dotenv()
    engine = make_engine()
    analyzer = MessageAnalyzer(engine)
    explainer = Explainer()
    context = load_context()

    print(f"engine={engine_label(engine)}  explainer={'LLM' if explainer.llm else 'Jev-template'}\n")

    for tag, msg in CASES:
        a = analyzer.analyze(text=msg, sender="对方", conversation="测试",
                             is_group=False, context=context)
        e = explainer.explain(msg, a, context, sender="对方", conversation="测试")
        print("=" * 74)
        print(f"【{tag}】{msg}")
        print(f"  PUA={a.pua_level}({a.pua_risk:.2f})  紧急={a.urgency}  "
              f"可自动回={a.can_auto_reply}({a.auto_reply_confidence:.2f})")
        print(f"  --- 详细解读 [{e.source}] ---")
        for label, val in e.as_lines():
            print(f"  {label}: {val}")
        if e.suggested_replies:
            print(f"  回复选项:")
            for i, r in enumerate(e.suggested_replies, 1):
                print(f"    {i}. {r}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
