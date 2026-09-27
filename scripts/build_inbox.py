"""Build .cache/inbox.json from raw dws output files.

This script does NOT call dws (it can't — dws only runs inside QwenWork's Bash
tool). Instead it merges pre-exported raw JSON files:

    .cache/raw/recent.json    <- dws chat +recent-conversations --output ...
    .cache/raw/msg_*.json     <- dws chat +chat-messages --group <cid> --output ...

For each conversation it picks the latest message NOT sent by you
(RADAR_SELF_ID), so the radar only shows things awaiting your reply.

Refresh workflow (run these dws commands inside QwenWork's Bash tool, then run
this script):

    dws chat +recent-conversations --output .cache/raw/recent.json --format json
    # for each cid in recent.json:
    dws chat +chat-messages --group '<cid>' --order desc --no-reactions \
        --output .cache/raw/msg_<n>.json --format json
    python scripts/build_inbox.py

Run:
    .venv/bin/python scripts/build_inbox.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.infrastructure.sources import _parse_ts, build_deep_link  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / ".cache" / "raw"
INBOX = ROOT / ".cache" / "inbox.json"


def load_env_self_id() -> str:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("RADAR_SELF_ID="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("RADAR_SELF_ID", "")


def main() -> int:
    self_id = load_env_self_id()
    if not self_id:
        print("WARNING: RADAR_SELF_ID not set in .env — cannot filter out your "
              "own messages. Set it to your DingTalk senderId.", file=sys.stderr)

    recent_path = RAW_DIR / "recent.json"
    if not recent_path.exists():
        print(f"ERROR: {recent_path} not found. Run the dws refresh commands "
              f"first (see module docstring).", file=sys.stderr)
        return 1

    recent = json.loads(recent_path.read_text(encoding="utf-8"))
    convs = (recent.get("data") or {}).get("conversations") \
            or (recent.get("result") or {}).get("conversations") or []
    cid_to_meta = {
        c["conversationId"]: {
            "name": c.get("name", "(未知)"),
            "is_group": (c.get("type", "") == "group"),
        }
        for c in convs if c.get("conversationId")
    }
    print(f"recent.json: {len(cid_to_meta)} conversations")

    # Collect all msg_*.json files
    msg_files = sorted(glob.glob(str(RAW_DIR / "msg_*.json")))
    print(f"found {len(msg_files)} msg_*.json files")

    messages = []
    for mf in msg_files:
        try:
            data = json.loads(Path(mf).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skip {Path(mf).name}: {e}")
            continue
        msgs = data.get("messages") or (data.get("data") or {}).get("messages") or []
        if not msgs:
            continue
        # pick latest non-self message (msgs are already desc by --order desc)
        chosen = None
        for m in msgs:
            if self_id and m.get("senderId") == self_id:
                continue
            chosen = m
            break
        if chosen is None:
            continue  # whole conversation is self-talk (e.g. file-transfer to self)

        cid = chosen.get("conversationId", "")
        meta = cid_to_meta.get(cid, {})
        mid = chosen.get("messageId", "")
        ts = _parse_ts(chosen.get("createTime"))
        messages.append({
            "conversation_id": cid,
            "conversation_name": meta.get("name", chosen.get("sender", "(未知)")),
            "is_group": meta.get("is_group", False),
            "sender_name": chosen.get("sender", ""),
            "sender_id": chosen.get("senderId", ""),
            "content": chosen.get("text", ""),
            "timestamp_ms": ts,
            "unread_count": 1,
            "message_id": mid,
            "deep_link": build_deep_link(cid, mid, chosen),
        })

    # sort by timestamp desc
    messages.sort(key=lambda m: m["timestamp_ms"], reverse=True)

    inbox = {
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "self_sender_id": self_id,
        "messages": messages,
    }
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ wrote {len(messages)} messages to {INBOX}")
    for m in messages[:10]:
        when = datetime.fromtimestamp(m["timestamp_ms"] / 1000).strftime("%m-%d %H:%M") if m["timestamp_ms"] else "?"
        print(f"  [{when}] {m['conversation_name']}: {m['content'][:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
