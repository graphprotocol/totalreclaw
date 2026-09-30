"""Topical dedup-context cross-language parity test (Python / PyO3 side).

PRD-04 DEP-3. Loads ``tests/parity/fixtures/dedup-context-v1.json`` (written by
``tests/parity/fixtures/generate-dedup-context-v1.py``) and asserts that
``totalreclaw_core.build_dedup_context`` returns each case's ``expected``
string byte-for-byte. The Rust side is
``rust/totalreclaw-core/src/dedup_context.rs::tests::parity_fixture_vectors``;
the WASM side is ``tests/parity/dedup-context-parity.test.ts``.

Needs a ``totalreclaw_core`` built from this tree (CI's ``python-tests`` job
does this). Locally:
``cd rust/totalreclaw-core && maturin build --release --features python-extension --out dist``
then ``pip install --force-reinstall dist/totalreclaw_core-*.whl``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import totalreclaw_core

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "parity" / "fixtures" / "dedup-context-v1.json"
)
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
HEADER = (
    "Existing memories (use these for dedup — classify as "
    "UPDATE/DELETE/NOOP if they conflict or overlap):"
)


def test_binding_is_exported():
    assert hasattr(totalreclaw_core, "build_dedup_context"), (
        "installed totalreclaw_core has no build_dedup_context — rebuild the "
        "wheel from this tree (see module docstring)"
    )


def test_fixture_header_matches_pre_dep3_python_header():
    assert FIXTURE["header"] == HEADER
    assert FIXTURE["default_cap"] == 30


def test_fixture_has_all_cases():
    assert len(FIXTURE["cases"]) == 14


@pytest.mark.parametrize(
    "case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]]
)
def test_fixture_case(case):
    out = totalreclaw_core.build_dedup_context(
        json.dumps(case["topical"]),
        json.dumps(case["pinned"]),
        json.dumps(case["recent"]),
        case["cap"],
    )
    assert out == case["expected"]


def test_non_ascii_json_escapes_round_trip():
    # json.dumps escapes non-ASCII as \uXXXX by default; core must decode it.
    out = totalreclaw_core.build_dedup_context(
        json.dumps([{"id": "t1", "text": "café ☕"}]), "[]", "[]", 30
    )
    assert out == HEADER + "\n[ID: t1] café ☕"


def test_null_sections_are_empty():
    assert totalreclaw_core.build_dedup_context("null", "null", "null", 30) == ""


def test_malformed_json_raises_value_error():
    with pytest.raises(ValueError, match="invalid topical JSON"):
        totalreclaw_core.build_dedup_context("not json", "[]", "[]", 30)


def test_negative_cap_is_rejected():
    with pytest.raises(OverflowError):
        totalreclaw_core.build_dedup_context("[]", "[]", "[]", -1)
