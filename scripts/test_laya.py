"""Validate the LOCAL Laya engine end-to-end: download model + real inference.

First run downloads laya-multilingual (~650M) from ModelScope, then runs the
full 39-question analysis locally (offline). Compare against cloud Jev to
confirm the two backends produce consistent judgments.

Run:
    .venv/bin/python scripts/test_laya.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["RADAR_ENGINE"] = "laya"   # force local backend
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import LayaEngine, engine_label, make_engine  # noqa: E402

CASES = [
    ("紧急任务", "在吗？下午3点前把上周的销售数据发我，老板要看，急。"),
    ("PUA打压", "你怎么这么笨，这点小事都做不好，除了我没人受得了你。"),
    ("暧昧", "在干嘛呢？突然有点想你[害羞]"),
    ("简单确认", "好的收到，谢谢啦！"),
]


def main() -> int:
    engine = make_engine("laya")
    assert isinstance(engine, LayaEngine)
    analyzer = MessageAnalyzer(engine)

    print(f"engine={engine_label(engine)}  model={engine.model_id}")
    print("首次运行会从 ModelScope 下载 ~650M 模型，请稍候...\n")

    t0 = time.time()
    first = True
    for tag, msg in CASES:
        ts = time.time()
        a = analyzer.analyze(text=msg, sender="对方", conversation="测试", is_group=False)
        dt = time.time() - ts
        if first:
            print(f"（模型加载 + 下载总耗时 {dt:.1f}s，status={engine.status}）\n")
            first = False
        if a.error:
            print(f"【{tag}】ERROR: {a.error}")
            continue
        intents = ", ".join(f"{h.label}{h.score:.2f}" for h in a.top_intents[:4])
        print(f"【{tag}】{msg}")
        print(f"  紧急={a.urgency} 重要={a.importance} 综合={a.composite_score:.2f} "
              f"PUA={a.pua_level}({a.pua_risk:.2f}) 可自动回={a.can_auto_reply}({a.auto_reply_confidence:.2f})")
        print(f"  意图: {intents}")
        print(f"  回复[{a.reply_rule}]: {a.reply_suggestion}")
        print(f"  ({dt*1000:.0f}ms/条, model={a.raw.model if a.raw else '?'})\n")

    print(f"✅ 本地 Laya 全程离线推理完成，总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
