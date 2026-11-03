"""Write-gate cross-language parity (PyO3 + pure-Python fallback).

Loads ``tests/parity/fixtures/write-gate-v1.json`` (written by
``tests/parity/fixtures/generate-write-gate-v1.py``). The Rust side is
``rust/totalreclaw-core/src/write_gate.rs::tests::parity_fixture_vectors``;
the WASM side is ``tests/parity/write-gate-parity.test.ts``. The Python
fallback in ``totalreclaw.write_gate`` (used when the installed core predates
``decide_write``) must give the same answers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import totalreclaw_core
from totalreclaw import write_gate

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "parity" / "fixtures" / "write-gate-v1.json")
    .read_text(encoding="utf-8")
)
HAS_CORE = hasattr(totalreclaw_core, "decide_write")


def _check(got: dict, want: dict, name: str) -> None:
    assert got["action"] == want["action"], name
    if want["match"] is None:
        assert got["match"] is None, name
        return
    for k in ("fact_id", "text", "kind", "pinned"):
        assert got["match"][k] == want["match"][k], (name, k)
    assert abs(got["match"]["similarity"] - want["match"]["similarity"]) < 1e-6, name


@pytest.mark.parametrize("case", FIXTURE["normalize"], ids=[repr(c["input"]) for c in FIXTURE["normalize"]])
def test_python_fallback_normalize(case):
    assert write_gate._py_normalize(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["decide"], ids=[c["name"] for c in FIXTURE["decide"]])
def test_python_fallback_decide(case):
    threshold = case["threshold"] if case["threshold"] is not None else write_gate.STORE_DEDUP_THRESHOLD
    got = write_gate._py_decide(
        case["text"], case["embedding"] or [], write_gate._clean_candidates(case["candidates"]),
        threshold, case["mode"],
    )
    _check(got, case["expected"], case["name"])


@pytest.mark.skipif(not HAS_CORE, reason="installed totalreclaw_core predates decide_write")
@pytest.mark.parametrize("case", FIXTURE["normalize"], ids=[repr(c["input"]) for c in FIXTURE["normalize"]])
def test_core_normalize(case):
    assert totalreclaw_core.normalize_fact_text(case["input"]) == case["expected"]


@pytest.mark.skipif(not HAS_CORE, reason="installed totalreclaw_core predates decide_write")
@pytest.mark.parametrize("case", FIXTURE["decide"], ids=[c["name"] for c in FIXTURE["decide"]])
def test_core_decide(case):
    out = totalreclaw_core.decide_write(
        case["text"], json.dumps(case["embedding"]), json.dumps(case["candidates"]),
        case["mode"], case["threshold"],
    )
    _check(json.loads(out), case["expected"], case["name"])


@pytest.mark.skipif(not HAS_CORE, reason="installed totalreclaw_core predates decide_write")
def test_core_rejects_bad_input():
    with pytest.raises(ValueError):
        totalreclaw_core.decide_write("t", "[]", "[]", "sometimes")
    with pytest.raises(ValueError):
        totalreclaw_core.decide_write("t", "[]", "[]", "auto", 1.5)
    with pytest.raises(ValueError):
        totalreclaw_core.decide_write("t", "nope", "[]", "auto")


def test_wrapper_uses_fallback_when_core_lacks_decide_write(monkeypatch):
    class NoGate:
        pass

    monkeypatch.setattr(write_gate, "_core", lambda: NoGate())
    d = write_gate.decide_write(
        "User is vegetarian", None, [{"id": "f1", "text": "user is vegetarian."}], mode="auto",
    )
    assert d.action == "skip" and d.match.kind == "exact"
    assert write_gate.normalize_fact_text("User is vegetarian.") == "user is vegetarian"
