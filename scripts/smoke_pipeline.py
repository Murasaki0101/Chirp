"""End-to-end pipeline test without UI.

Fetches mock messages, analyzes each with real Jev (or mock if no key),
prints a ranked table. This is the fastest way to verify the whole chain
before wiring it into the floating window.

Run:
    .venv/bin/python scripts/smoke_pipeline.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Use the real source by default (CacheSource if .cache/inbox.json exists,
# else DwsSource). Set RADAR_MOCK=true to force mock data.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.domain.context import load_context  # noqa: E402
from chirp.infrastructure.engine import engine_label, make_engine  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv  # noqa: E402
from chirp.infrastructure.sources import make_source  # noqa: E402


def main() -> int:
    load_dotenv()
    prefer_mock = os.environ.get("RADAR_MOCK", "").lower() in ("1", "true", "yes")
    source = make_source(prefer_mock=prefer_mock)
    engine = make_engine("mock" if prefer_mock else None)
    analyzer = MessageAnalyzer(engine)
    context = load_context()

    ctx_brief = (f"{len(context.today_events)}日程/"
                 f"{len(context.due_today_todos)}今到期待办/"
                 f"{len(context.open_todos)}待办")
    print(f"source={type(source).__name__}  engine={engine_label(engine)}  context=[{ctx_brief}]\n")

    messages = source.fetch()
    print(f"fetched {len(messages)} messages, analyzing...\n")

    items = []
    for i, m in enumerate(messages, 1):
        print(f"[{i}/{len(messages)}] {m.conversation_name}: {m.content[:40]}...")
        a = analyzer.analyze(
            text=m.content,
            sender=m.sender_name,
            conversation=m.conversation_name,
            is_group=m.is_group,
            context=context,
        )
        items.append((m, a))
        ctx_flags = []
        if a.deadline_conflict >= 0.5:
            ctx_flags.append(f"截止冲突{a.deadline_conflict:.2f}")
        if a.blocked_now >= 0.5:
            ctx_flags.append(f"被占用{a.blocked_now:.2f}")
        if a.already_planned >= 0.5:
            ctx_flags.append(f"已在计划{a.already_planned:.2f}")
        ctx_str = ("  ctx[" + ", ".join(ctx_flags) + "]") if ctx_flags else ""
        print(f"    -> urgency={a.urgency}  importance={a.importance}  "
              f"composite={a.composite_score}{ctx_str}  ({a.latency_ms}ms)")
        if a.error:
            print(f"    ERROR: {a.error}")

    items.sort(key=lambda x: x[1].composite_score, reverse=True)

    print("\n" + "=" * 78)
    print("RANKED (most urgent first)")
    print("=" * 78)
    for rank, (m, a) in enumerate(items, 1):
        intents = ", ".join(f"{h.label}({h.score:.2f})" for h in a.top_intents[:4])
        print(f"\n#{rank}  [{a.urgency.upper()}/{a.importance.upper()}]  "
              f"score={a.composite_score:.2f}  {m.conversation_name}")
        print(f"    msg: {m.content}")
        print(f"    intents: {intents}")
        print(f"    reply ({a.reply_rule}): {a.reply_suggestion}")
        print(f"    deep link: {m.deep_link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
