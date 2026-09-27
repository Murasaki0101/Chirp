"""Unified decision-engine abstraction: cloud Jev vs local Laya vs Mock.

Both Jev (TypeSafe, closed-source, cloud) and Laya (convaiinnovations,
open-source, local) are "System One" decision models that share the SAME
question primitives — `choice` / `score` / `noul` — and nearly identical answer
shapes. So the analyzer never needs to know which backend is running; it just
calls `engine.ask(state, questions)` and gets back a uniform `JevResponse`.

Switch backends via:
  - env `RADAR_ENGINE=jev|laya|mock`, or
  - the in-app Settings panel (which writes the same config).

Laya runs fully offline after the first model download (~650M, pulled from
ModelScope by default for CN speed, HuggingFace as fallback).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Protocol

from .jev_client import (
    ChoiceAnswer,
    JevClient,
    JevError,
    JevResponse,
    MockJevClient,
    NoulAnswer,
    ScoreAnswer,
    load_dotenv,
)

log = logging.getLogger(__name__)

LAYA_MODEL_ID = os.environ.get("RADAR_LAYA_MODEL", "convaiinnovations/laya-multilingual")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class DecisionEngine(Protocol):
    """Anything the analyzer can ask questions of."""
    name: str

    def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> JevResponse:
        ...


# ---------------------------------------------------------------------------
# Jev (cloud)
# ---------------------------------------------------------------------------

class JevEngine:
    name = "jev"

    def __init__(self, api_key: str | None = None, **kw: Any) -> None:
        load_dotenv()
        self._client = JevClient(api_key=api_key, **kw)

    def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> JevResponse:
        return self._client.ask(state, questions)


# ---------------------------------------------------------------------------
# Mock (offline dev / no key)
# ---------------------------------------------------------------------------

class MockEngine:
    name = "mock"

    def __init__(self) -> None:
        self._client = MockJevClient()

    def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> JevResponse:
        return self._client.ask(state, questions)


# ---------------------------------------------------------------------------
# Laya (local, open-source System One model)
# ---------------------------------------------------------------------------

class LayaEngine:
    """Local decision engine backed by convaiinnovations/laya-multilingual.

    The model is lazy-loaded on first `ask()` (loading 650M takes a few
    seconds), downloaded via ModelScope by default. Set `status` to inspect
    load state for UI feedback: idle | loading | ready | error.
    """
    name = "laya"
    # Empirically, Laya stays better calibrated on the RAW message only; the
    # "会话/发送人/消息内容:" scaffolding that helps Jev actually degrades Laya
    # (e.g. pua_love_bombing 0.45→0.88 on a benign "谢谢"). So Laya uses plain state.
    use_plain_state = True

    def __init__(
        self,
        model_id: str = LAYA_MODEL_ID,
        download_source: str = "modelscope",
        device: str = "cpu",
        batch_size: int = 8,
    ) -> None:
        self.model_id = model_id
        self.download_source = download_source
        self.device = device
        # Laya degrades on large question batches (drops answers / miscalibrates),
        # unlike cloud Jev which handles 40+ questions stably. So we split into
        # small batches and merge. 8 is a safe size per empirical testing.
        self.batch_size = batch_size
        self._agent: Any = None
        self._load_error: str | None = None
        self.status = "idle"
        self.model_path: str = ""

    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._agent is not None:
            return
        if self._load_error:
            raise JevError(f"Laya previously failed to load: {self._load_error}")
        self.status = "loading"
        try:
            import laya  # imported lazily so the module works without torch
        except ImportError as e:
            self.status = "error"
            self._load_error = (
                "laya not installed. Run: pip install laya modelscope "
                f"(original error: {e})"
            )
            raise JevError(self._load_error) from e

        try:
            local = self._download()
            if local:
                self.model_path = local
                self._agent = laya.load(local)
            else:
                # fall back to laya's own HF hub resolution
                self._agent = laya.load("convaiinnovations/laya", subfolder="multilingual")
            self.status = "ready"
            log.info("Laya model ready (%s)", self.model_path or "hf-hub")
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self._load_error = str(e)
            raise JevError(f"Laya load failed: {e}") from e

    def _download(self) -> str | None:
        """Download the model to a local cache dir; return the path or None."""
        if self.download_source != "modelscope":
            return None
        try:
            from modelscope import snapshot_download
        except ImportError:
            log.warning("modelscope not installed; falling back to HF hub")
            return None
        try:
            path = snapshot_download(self.model_id)
            log.info("Laya downloaded via ModelScope → %s", path)
            return path
        except Exception as e:  # noqa: BLE001
            log.warning("ModelScope download failed (%s); will try HF hub", e)
            return None

    # ------------------------------------------------------------------

    def ask(self, state: str, questions: dict[str, dict[str, Any]]) -> JevResponse:
        self._ensure_loaded()
        t0 = time.monotonic()
        try:
            if len(questions) <= self.batch_size:
                out = self._agent.predict(state, questions)
                answers = out.get("answers", {}) if isinstance(out, dict) else {}
                model = (out.get("model") if isinstance(out, dict) else None) or "laya"
            else:
                # Split into small batches so Laya stays calibrated, then merge.
                answers = {}
                model = "laya"
                items = list(questions.items())
                for i in range(0, len(items), self.batch_size):
                    batch = dict(items[i:i + self.batch_size])
                    out = self._agent.predict(state, batch)
                    if isinstance(out, dict):
                        answers.update(out.get("answers", {}))
                        model = out.get("model", model) or model
        except Exception as e:  # noqa: BLE001
            raise JevError(f"Laya predict failed: {e}") from e
        latency_ms = int((time.monotonic() - t0) * 1000)
        return self._parse({"answers": answers, "model": model}, latency_ms, questions)

    @staticmethod
    def _parse(out: Any, latency_ms: int, questions: dict[str, dict[str, Any]]) -> JevResponse:
        """Normalize Laya's output into the shared JevResponse structure.

        Laya answers look like {"key": {"noul": 0.8}} / {"score": 1.2, ...} /
        {"choice": "billing", ...} — sometimes with a "type" field, sometimes
        without. We detect by which value key is present, and reconstruct the
        score legend from the question's criteria when Laya omits it.
        """
        raw_answers = out.get("answers", {}) if isinstance(out, dict) else {}
        answers: dict[str, Any] = {}
        for key, val in raw_answers.items():
            if not isinstance(val, dict):
                continue
            qtype = val.get("type")
            if "noul" in val or qtype == "noul":
                answers[key] = NoulAnswer(key=key, score=float(val.get("noul", 0.0)))
            elif "score" in val or qtype == "score":
                score = float(val.get("score", 0.0))
                criteria = questions.get(key, {}).get("criteria") or []
                legend = val.get("legend") or {str(i): c for i, c in enumerate(criteria)}
                label = val.get("label") or legend.get(str(int(round(score))), "")
                answers[key] = ScoreAnswer(
                    key=key, value=score, label=label,
                    confidence=float(val.get("confidence", 0.0)),
                    probabilities={k: float(v) for k, v in (val.get("probabilities") or {}).items()},
                )
            elif "choice" in val or qtype == "choice":
                answers[key] = ChoiceAnswer(
                    key=key, choice=str(val.get("choice", "")),
                    confidence=float(val.get("confidence", 0.0)),
                    probabilities={k: float(v) for k, v in (val.get("probabilities") or {}).items()},
                )
        usage = out.get("usage", {}) if isinstance(out, dict) else {}
        model = (out.get("model") if isinstance(out, dict) else None) or "laya"
        return JevResponse(
            model=str(model),
            answers=answers,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            latency_ms=latency_ms,
            raw=out if isinstance(out, dict) else {"raw": str(out)},
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_engine(mode: str | None = None, **kw: Any) -> DecisionEngine:
    """Build the configured decision engine.

    mode: "jev" | "laya" | "mock" | None (auto).
    Auto: prefer Laya if RADAR_ENGINE says local; else Jev when a key exists;
    else Mock so the UI still runs.
    """
    load_dotenv()
    mode = (mode or os.environ.get("RADAR_ENGINE", "")).strip().lower()

    if mode in ("jev", "cloud"):
        return JevEngine(**kw)
    if mode in ("laya", "local"):
        return LayaEngine(**kw)
    if mode == "mock":
        return MockEngine()

    # auto
    if os.environ.get("TYPESAFE_API_KEY"):
        try:
            return JevEngine(**kw)
        except JevError as e:
            log.warning("Jev engine unavailable (%s); falling back to Mock", e)
    log.info("No engine configured — using MockEngine "
             "(set RADAR_ENGINE=laya for local, or TYPESAFE_API_KEY for cloud)")
    return MockEngine()


def engine_label(engine: DecisionEngine) -> str:
    """Human-readable label for the status bar / settings."""
    return {
        "jev": "☁️ 云端 Jev",
        "laya": "🖥 本地 Laya",
        "mock": "🧪 Mock",
    }.get(getattr(engine, "name", "?"), getattr(engine, "name", "?"))
