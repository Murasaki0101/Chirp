"""Verify the user-provided LLM (DashScope qwen3.7-flash) is reachable via the
OpenAI-compatible /chat/completions endpoint that explainer.LLMClient uses.

Run:
    .venv/bin/python scripts/smoke_llm.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.application.explainer import make_llm_client  # noqa: E402
from chirp.infrastructure.jev_client import load_dotenv  # noqa: E402


def main() -> int:
    load_dotenv()
    llm = make_llm_client()
    if llm is None:
        print("LLM not configured (RADAR_LLM_* missing)")
        return 1
    print(f"LLM: model={llm.model}  base={llm.base_url}")
    try:
        resp = llm.chat(
            "你是即时消息回复助手，只输出 JSON，不要多余文字。",
            '用户收到消息："下午3点前把销售数据发我，老板要看，急。"\n'
            '请输出 JSON：{"summary":"一句话解读对方意图","suggested_replies":["回复选项1","回复选项2"]}',
        )
        print("--- RESP ---")
        print(resp[:800])
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"LLM call FAILED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
