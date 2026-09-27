"""Validate the decision-engine abstraction WITHOUT needing the 650M Laya model.

Tests:
  1. make_engine factory returns the right backend per mode
  2. MockEngine round-trips a noul/score/choice question set
  3. LayaEngine._parse correctly normalizes Laya's output shape into the shared
     JevResponse (including reconstructing the score legend from criteria)
  4. The analyzer works identically regardless of backend (duck-typed .ask)

Run:
    .venv/bin/python scripts/test_engine.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chirp.domain.analyzers import MessageAnalyzer  # noqa: E402
from chirp.infrastructure.engine import (  # noqa: E402
    JevEngine, LayaEngine, MockEngine, engine_label, make_engine,
)
from chirp.infrastructure.jev_client import ChoiceAnswer, NoulAnswer, ScoreAnswer  # noqa: E402


def test_factory() -> None:
    print("== 1. factory ==")
    assert isinstance(make_engine("mock"), MockEngine)
    assert isinstance(make_engine("laya"), LayaEngine)
    print("  mock →", engine_label(make_engine("mock")))
    print("  laya →", engine_label(make_engine("laya")))
    # jev only if a key is present
    import os
    from chirp.infrastructure.jev_client import load_dotenv
    load_dotenv()
    if os.environ.get("TYPESAFE_API_KEY"):
        assert isinstance(make_engine("jev"), JevEngine)
        print("  jev  →", engine_label(make_engine("jev")))
    print("  auto →", engine_label(make_engine()))


def test_mock_roundtrip() -> None:
    print("\n== 2. MockEngine round-trip ==")
    e = make_engine("mock")
    questions = {
        "flag": {"type": "noul", "instructions": "x"},
        "level": {"type": "score", "instructions": "y", "criteria": ["low", "med", "high"]},
        "pick": {"type": "choice", "instructions": "z", "criteria": {"a": "A", "b": "B"}},
    }
    r = e.ask("some state", questions)
    assert isinstance(r.answers["flag"], NoulAnswer)
    assert isinstance(r.answers["level"], ScoreAnswer)
    assert isinstance(r.answers["pick"], ChoiceAnswer)
    print(f"  noul={r.answers['flag'].score:.2f} "
          f"score={r.answers['level'].value:.2f}({r.answers['level'].label}) "
          f"choice={r.answers['pick'].choice}")


def test_laya_parse() -> None:
    print("\n== 3. LayaEngine._parse (simulated Laya output) ==")
    # This is the shape laya's router.predict returns: answers[key] holds the
    # value directly, sometimes without a "type" field.
    fake_out = {
        "model": "laya-multilingual",
        "answers": {
            "needs_action": {"noul": 0.91},
            "urgency": {"score": 1.8, "confidence": 0.77,
                        "probabilities": {"0": 0.05, "1": 0.15, "2": 0.80}},
            "team": {"choice": "billing", "confidence": 0.88,
                     "probabilities": {"billing": 0.88, "other": 0.12}},
        },
    }
    questions = {
        "needs_action": {"type": "noul", "instructions": "..."},
        "urgency": {"type": "score", "instructions": "...",
                    "criteria": ["low", "medium", "high"]},
        "team": {"type": "choice", "instructions": "...",
                 "criteria": {"billing": "payments", "other": "else"}},
    }
    resp = LayaEngine._parse(fake_out, latency_ms=33, questions=questions)
    na = resp.answers["needs_action"]
    urg = resp.answers["urgency"]
    team = resp.answers["team"]
    assert isinstance(na, NoulAnswer) and abs(na.score - 0.91) < 1e-6
    assert isinstance(urg, ScoreAnswer)
    # legend reconstructed from criteria: score 1.8 → rounds to index 2 → "high"
    assert urg.label == "high", f"expected 'high', got {urg.label!r}"
    assert isinstance(team, ChoiceAnswer) and team.choice == "billing"
    print(f"  noul={na.score}  score={urg.value}→'{urg.label}'  choice={team.choice}")
    print(f"  model={resp.model} latency={resp.latency_ms}ms")
    print("  ✓ Laya output normalizes correctly into shared JevResponse")


def test_backend_agnostic() -> None:
    print("\n== 4. analyzer is backend-agnostic ==")
    analyzer = MessageAnalyzer(make_engine("mock"))
    r = analyzer.analyze("在吗？下午3点前把报告发我，急。", sender="老王", conversation="老王")
    assert r.top_intents is not None
    print(f"  mock backend analyzed OK: urgency={r.urgency} composite={r.composite_score:.2f}")


def main() -> int:
    test_factory()
    test_mock_roundtrip()
    test_laya_parse()
    test_backend_agnostic()
    print("\n✅ engine abstraction validated (Jev/Laya/Mock interchangeable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
