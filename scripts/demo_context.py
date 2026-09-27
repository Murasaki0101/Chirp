"""Demonstrate context-aware urgency.

Same message, two situations. This is the core argument for folding calendar /
todo / current-time into the Jev state: urgency is a property of
(message × my situation), not of the message alone.

Run:
    .venv/bin/python scripts/demo_context.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.domain.context import CalendarEvent, TodoItem, UserContext  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv, make_client  # noqa: E402

MSG = "在吗？下午3点前把上周的销售数据发我，老板要看，急。"
SENDER = "老王"
CONV = "老王"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def build_contexts() -> tuple[datetime, UserContext, UserContext]:
    """Pin 'now' to 14:50 today so the '3pm' deadline is ~10 min away."""
    now_dt = datetime.now().replace(hour=14, minute=50, second=0, microsecond=0)
    now = _ms(now_dt)

    # Situation A: free all afternoon — no meetings.
    free = UserContext(
        now_ms=now,
        today_events=[],
        next_event=None,
        due_today_todos=[],
        open_todos=[TodoItem(task_id="t1", title="整理周报", due_ms=0, priority=20)],
    )

    # Situation B: stuck in a 14:00–16:00 product review right now.
    meeting = CalendarEvent(
        event_id="ev1",
        title="产品评审会",
        start_ms=_ms(now_dt.replace(hour=14, minute=0)),
        end_ms=_ms(now_dt.replace(hour=16, minute=0)),
    )
    busy = UserContext(
        now_ms=now,
        today_events=[meeting],
        next_event=None,
        due_today_todos=[],
        open_todos=[TodoItem(task_id="t1", title="整理周报", due_ms=0, priority=20)],
    )
    return now_dt, free, busy


def show(tag: str, ctx: UserContext, a) -> None:
    print(f"\n{'='*72}\n场景 {tag}\n{'='*72}")
    print(ctx.serialize())
    print(f"\n  消息: {SENDER}: {MSG}")
    print(f"  --- Jev 判断 ---")
    print(f"  紧急度={a.urgency}  重要度={a.importance}  综合分={a.composite_score:.2f}")
    print(f"  截止冲突={a.deadline_conflict:.2f}  当前被占用={a.blocked_now:.2f}  "
          f"现在有空={a.can_handle_now:.2f}  已在计划={a.already_planned:.2f}")
    top = ", ".join(f"{h.label}({h.score:.2f})" for h in a.top_intents[:4])
    print(f"  意图: {top}")
    print(f"  💡 回复建议 [{a.reply_rule}]: {a.reply_suggestion}")


def main() -> int:
    load_dotenv()
    client = make_client()
    analyzer = MessageAnalyzer(client)
    now_dt, free, busy = build_contexts()

    print(f"演示时间固定为今天 {now_dt.strftime('%H:%M')}，消息要求「下午3点前」——只剩约 10 分钟。")
    print(f"消息: {SENDER}: {MSG}")

    a_free = analyzer.analyze(MSG, sender=SENDER, conversation=CONV, is_group=False, context=free)
    show("A · 下午空闲（无日程）", free, a_free)

    a_busy = analyzer.analyze(MSG, sender=SENDER, conversation=CONV, is_group=False, context=busy)
    show("B · 正在开 14:00-16:00 产品评审会", busy, a_busy)

    print(f"\n{'='*72}\n对比结论\n{'='*72}")
    print(f"  同一条消息，综合分 A={a_free.composite_score:.2f} vs B={a_busy.composite_score:.2f}")
    print(f"  A 回复: {a_free.reply_suggestion}")
    print(f"  B 回复: {a_busy.reply_suggestion}")
    print("\n  → 处境改变了「该不该现在硬接」以及「怎么回」。这就是 context-aware 的价值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
