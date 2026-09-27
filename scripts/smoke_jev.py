"""Jev API smoke test — verify the key works and inspect the real response shape.

Run: python3 scripts/smoke_jev.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.infrastructure.jev_client import JEV_ENDPOINT, load_dotenv  # noqa: E402


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("ERROR: TYPESAFE_API_KEY not set in .env or environment", file=sys.stderr)
        return 2

    payload = {
        "model": "jev-latest",
        "state": "同事在群里说：明天下午3点前把销售报告发我，急用。",
        "questions": {
            "needs_action": {
                "type": "noul",
                "instructions": "The message asks the reader to do something.",
            },
            "has_deadline": {
                "type": "noul",
                "instructions": "The message specifies a deadline or urgent time constraint.",
            },
            "is_negative": {
                "type": "noul",
                "instructions": "The message carries negative emotion (anger, complaint, pressure).",
            },
            "urgency": {
                "type": "score",
                "instructions": "Rate how urgently the reader must respond.",
                "criteria": ["low", "medium", "high"],
            },
        },
    }

    req = urllib.request.Request(
        JEV_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            print("HTTP", resp.status)
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except json.JSONDecodeError:
                print(body)
            return 0
    except urllib.error.HTTPError as e:
        print("HTTPError", e.code, file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print("URLError", e.reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
