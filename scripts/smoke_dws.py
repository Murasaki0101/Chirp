"""Verify DwsSource parses REAL dws responses correctly.

Since `+unread-chats` may be empty (you've read everything), this script
probes `+recent-conversations` and `+chat-messages` instead, and runs the
same `_parse_*` code paths the real source uses. If this prints clean
Message objects, the field alignment is correct.

Run:
    .venv/bin/python scripts/smoke_dws.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.infrastructure.sources import DwsSource, _parse_ts  # noqa: E402


def main() -> int:
    src = DwsSource(hydrate_content=False)

    print("=" * 70)
    print("1) +recent-conversations (last 24h active chats)")
    print("=" * 70)
    data = src._run(["chat", "+recent-conversations"])
    if data is None:
        print("FAILED to run dws. Is the DingTalk connector authenticated?")
        return 1

    from chirp.infrastructure.sources import _extract_list
    convs = _extract_list(data, [("data", "conversations"), ("result", "conversations")])
    print(f"raw conversations: {len(convs)}")
    parsed = []
    for c in convs[:5]:
        m = src._parse_conversation(c)
        if m is None:
            print(f"  SKIP (no cid): {c}")
            continue
        parsed.append(m)
        print(f"  ✓ {m.conversation_name!r:24} group={m.is_group} "
              f"ts={m.timestamp_ms} ({_fmt_ts(m.timestamp_ms)})")
        print(f"    cid={m.conversation_id[:40]}...")
        print(f"    deep_link={m.deep_link[:80]}")

    if not parsed:
        print("\nNo conversations parsed. Check the raw JSON above.")
        return 1

    print("\n" + "=" * 70)
    print("2) +chat-messages on the first conversation (hydrate content)")
    print("=" * 70)
    target = parsed[0]
    latest = src._fetch_latest_message(target.conversation_id, target.is_group)
    if latest is None:
        print("FAILED to fetch messages")
        return 1
    print(f"  ✓ sender={latest.sender_name!r}  ts={_fmt_ts(latest.timestamp_ms)}")
    print(f"    msg_id={latest.message_id}")
    print(f"    content={latest.content!r}")
    print(f"    deep_link={latest.deep_link[:80]}")

    print("\n" + "=" * 70)
    print("3) Full DwsSource.fetch() (unread-chats → hydrate)")
    print("=" * 70)
    src2 = DwsSource(count=10, hydrate_content=True)
    msgs = src2.fetch()
    print(f"fetch() returned {len(msgs)} messages")
    for m in msgs[:5]:
        print(f"  • [{m.conversation_name}] {m.sender_name}: {m.content[:50]}")
    if not msgs:
        print("  (empty — you have no unread chats right now; that's OK)")

    print("\n✅ DWS field alignment verified.")
    return 0


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "n/a"
    from datetime import datetime
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
