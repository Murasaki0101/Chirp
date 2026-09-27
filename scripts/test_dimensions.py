"""Validate ALL analysis dimensions on representative messages.

Covers: intent, emotion, urgency/importance, context-awareness, PUA detection,
and auto-reply judgment. This is the quickest way to see whether the upgraded
analyzer behaves sensibly across message types.

Run:
    .venv/bin/python scripts/test_dimensions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.domain.context import load_context  # noqa: E402
from chirp.infrastructure.engine import engine_label, make_engine  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv  # noqa: E402

CASES = [
    ("PUA打压", "你怎么这么笨，这点小事都做不好，除了我没人受得了你，也就我还愿意搭理你。"),
    ("情感绑架", "你不回我消息就是不在乎我，我为你付出了那么多你都看不到吗？"),
    ("简单确认(可自动回)", "好的收到，谢谢啦！"),
    ("打招呼(可自动回)", "在吗？"),
    ("紧急任务", "下午3点前把上周的销售数据发我，老板要看，急。"),
    ("暧昧", "在干嘛呢？突然有点想你[害羞]"),
    ("闲聊", "喵了个咪"),
    ("工作对接", "这个接口的返回格式我们确认下，你那边文档更新了吗？"),
]


def main() -> int:
    load_dotenv()
    engine = make_engine()
    analyzer = MessageAnalyzer(engine)
    context = load_context()

    print(f"engine={engine_label(engine)}  "
          f"context: {len(context.today_events)}日程/{len(context.open_todos)}待办\n")

    for tag, msg in CASES:
        a = analyzer.analyze(text=msg, sender="对方", conversation="测试",
                             is_group=False, context=context)
        print("=" * 74)
        print(f"【{tag}】{msg}")
        if a.error:
            print(f"  ERROR: {a.error}")
            continue
        print(f"  紧急={a.urgency} 重要={a.importance} 综合分={a.composite_score:.2f} "
              f"情绪={a.emotion_valence}/{a.emotion_intensity}")
        intents = ", ".join(f"{h.label}{h.score:.2f}" for h in a.top_intents[:4])
        print(f"  意图: {intents}")
        # PUA
        pua_flag = "🚨" if a.pua_alert else ("⚠" if a.pua_level == "low" else "✓")
        print(f"  PUA: {pua_flag} {a.pua_level} (risk={a.pua_risk:.2f})"
              + (f" → {', '.join(a.pua_top)}" if a.pua_top else ""))
        # Auto-reply
        auto_flag = "🤖可自动回" if a.can_auto_reply else "👤需人工"
        print(f"  自动回复: {auto_flag} | 场景={a.auto_reply_scene} "
              f"置信={a.auto_reply_confidence:.2f} 安全={a.auto_reply_safe:.2f}")
        # Context
        if a.context_used and (a.deadline_conflict >= 0.5 or a.blocked_now >= 0.5):
            print(f"  处境: 截止冲突={a.deadline_conflict:.2f} 被占用={a.blocked_now:.2f}")
        print(f"  💡 回复[{a.reply_rule}]: {a.reply_suggestion}")
        print(f"  ({a.latency_ms}ms, {a.tokens[0]}+{a.tokens[1]}tok)")

    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
