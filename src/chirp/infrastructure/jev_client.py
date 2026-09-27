"""Jev (TypeSafe System One) API client.

Docs: https://docs.typesafe.ai
Endpoint: POST https://api.typesafe.ai/v1/systemone

Design notes
------------
Jev's killer feature is that fanning out ~40 yes/no ("noul") questions in a
single request costs roughly the same latency as 1 question. So we batch all
intent / emotion / urgency / importance questions for one message into a
single call, and score locally.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# .env loader (kept tiny so we don't require python-dotenv)
# ---------------------------------------------------------------------------

def get_env_path() -> Path:
    """Where the .env lives.

    - Dev (source): the project-dir .env.
    - Frozen (.app): ~/.dingtalk-radar/.env (user-writable).

    Credentials are always supplied by the process environment or this local
    user-owned file.  We deliberately never copy a bundled .env into the app
    configuration: a packaged application must not contain credentials.
    """
    if getattr(sys, "frozen", False):
        user_cfg = Path.home() / ".dingtalk-radar" / ".env"
        try:
            user_cfg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(user_cfg.parent, 0o700)
            if not user_cfg.exists():
                user_cfg.touch(mode=0o600)
            os.chmod(user_cfg, 0o600)
        except OSError:
            # The caller will still be able to use environment variables.
            pass
        return user_cfg
    return Path(__file__).resolve().parents[3] / ".env"


def load_dotenv(path: Path | str | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file. Does NOT override os.environ."""
    if path is None:
        path = get_env_path()
    path = Path(path)
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        # strip surrounding quotes
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k] = v
        os.environ.setdefault(k, v)
    return env


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class NoulAnswer:
    """A yes/no question. `score` in [0, 1] = probability of yes."""
    key: str
    score: float

    @property
    def yes(self) -> bool:
        return self.score >= 0.5


@dataclass
class ScoreAnswer:
    """A multi-criteria score. `value` is a float on the criteria index scale."""
    key: str
    value: float
    label: str           # e.g. "high" — the closest criteria label
    confidence: float
    probabilities: dict[str, float]


@dataclass
class ChoiceAnswer:
    key: str
    choice: str
    confidence: float
    probabilities: dict[str, float]


Answer = NoulAnswer | ScoreAnswer | ChoiceAnswer


@dataclass
class JevResponse:
    model: str
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw: dict[str, Any]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = JEV_ENDPOINT,
        model: str = JEV_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 1,
    ) -> None:
        # Ensure .env is loaded before we look up the key
        load_dotenv()
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevError(
                "TYPESAFE_API_KEY not set. Put it in .env or export it."
            )
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------

    def ask(
        self,
        state: str,
        questions: dict[str, dict[str, Any]],
    ) -> JevResponse:
        """Send one batch of questions about `state` and parse the response.

        `questions` maps a short answer-key -> question spec, e.g.::

            {
              "needs_action": {"type": "noul", "instructions": "..."},
              "urgency":      {"type": "score", "instructions": "...",
                                "criteria": ["low", "medium", "high"]},
              "team":         {"type": "choice", "instructions": "...",
                                "criteria": {"billing": "...", "tech": "..."}},
            }
        """
        if not questions:
            raise JevError("No questions to ask")

        payload = {
            "model": self.model,
            "state": state,
            "questions": questions,
        }
        body = json.dumps(payload).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            req = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                latency_ms = int((time.monotonic() - t0) * 1000)
                return self._parse(raw, latency_ms)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                # 429 / 5xx: retry with backoff. 4xx: fail fast.
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    wait = 0.5 * (2 ** attempt)
                    log.warning(
                        "Jev HTTP %s (attempt %d/%d), retrying in %.1fs: %s",
                        e.code, attempt + 1, self.max_retries + 1, wait, detail[:200],
                    )
                    time.sleep(wait)
                    last_err = JevError(f"HTTP {e.code}: {detail[:400]}")
                    continue
                raise JevError(f"HTTP {e.code}: {detail[:400]}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    wait = 0.5 * (2 ** attempt)
                    log.warning(
                        "Jev network error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self.max_retries + 1, wait, e,
                    )
                    time.sleep(wait)
                    last_err = e
                    continue
                raise JevError(f"network error: {e}") from e

        raise JevError(f"exhausted retries: {last_err}")

    # ------------------------------------------------------------------

    @staticmethod
    def _parse(raw: dict[str, Any], latency_ms: int) -> JevResponse:
        answers: dict[str, Answer] = {}
        for key, val in (raw.get("answers") or {}).items():
            qtype = val.get("type")
            if qtype == "noul":
                answers[key] = NoulAnswer(key=key, score=float(val.get("noul", 0.0)))
            elif qtype == "score":
                legend = val.get("legend") or {}
                # legend maps str(index) -> label. Pick label closest to score.
                score = float(val.get("score", 0.0))
                label = ""
                if legend:
                    # find nearest integer index
                    idx = str(int(round(score)))
                    label = legend.get(idx, "")
                answers[key] = ScoreAnswer(
                    key=key,
                    value=score,
                    label=label,
                    confidence=float(val.get("confidence", 0.0)),
                    probabilities={k: float(v) for k, v in (val.get("probabilities") or {}).items()},
                )
            elif qtype == "choice":
                answers[key] = ChoiceAnswer(
                    key=key,
                    choice=str(val.get("choice", "")),
                    confidence=float(val.get("confidence", 0.0)),
                    probabilities={k: float(v) for k, v in (val.get("probabilities") or {}).items()},
                )
            else:
                log.debug("Unknown Jev answer type %r for key %s", qtype, key)

        usage = raw.get("usage") or {}
        return JevResponse(
            model=str(raw.get("model", "")),
            answers=answers,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            latency_ms=latency_ms,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Mock client — used when TYPESAFE_API_KEY is missing so the UI still runs
# ---------------------------------------------------------------------------

class MockJevClient(JevClient):
    """Deterministic fake Jev for offline UI development."""

    def __init__(self) -> None:  # noqa: D107 - intentionally skip parent init
        self.api_key = "mock"
        self.endpoint = "mock://"
        self.model = "jev-mock"
        self.timeout = 1.0
        self.max_retries = 0

    def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> JevResponse:
        import hashlib
        h = int(hashlib.sha1(state.encode("utf-8")).hexdigest(), 16)
        answers: dict[str, Answer] = {}
        for i, (key, spec) in enumerate(questions.items()):
            qtype = spec.get("type")
            # deterministic-ish pseudo random in [0, 1]
            r = ((h >> (i * 3)) & 0xFFFF) / 0xFFFF
            if qtype == "noul":
                answers[key] = NoulAnswer(key=key, score=round(r, 2))
            elif qtype == "score":
                criteria = spec.get("criteria") or ["low", "medium", "high"]
                idx = int(r * len(criteria)) % len(criteria)
                legend = {str(i): c for i, c in enumerate(criteria)}
                probs = {str(i): (0.7 if i == idx else 0.15) for i in range(len(criteria))}
                answers[key] = ScoreAnswer(
                    key=key, value=float(idx), label=criteria[idx],
                    confidence=0.7, probabilities=probs,
                )
            elif qtype == "choice":
                criteria = spec.get("criteria") or {}
                keys = list(criteria.keys())
                pick = keys[int(r * len(keys)) % len(keys)] if keys else ""
                answers[key] = ChoiceAnswer(
                    key=key, choice=pick, confidence=0.6,
                    probabilities={k: (0.6 if k == pick else 0.1) for k in keys},
                )
        return JevResponse(
            model=self.model, answers=answers,
            input_tokens=0, output_tokens=0, latency_ms=1,
            raw={"mock": True, "state": state},
        )


def make_client(prefer_mock: bool = False) -> JevClient:
    """Factory: return a real client if a key is present, else a mock."""
    load_dotenv()
    if prefer_mock or not os.environ.get("TYPESAFE_API_KEY"):
        log.info("Using MockJevClient (no TYPESAFE_API_KEY or mock requested)")
        return MockJevClient()
    return JevClient()
