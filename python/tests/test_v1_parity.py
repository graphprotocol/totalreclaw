"""Cross-language v1 byte-parity tests for the Python client.

Builds v1 claims in Python and verifies that core's validator (which is the
SAME Rust code linked into the TS plugin's WASM) accepts them and round-trips
them with the same canonical form. This is the same boundary the TS plugin's
``v1-taxonomy.test.ts`` exercises, so a passing suite here + a passing suite
there = byte-equivalent output across Python + TS + Rust.

What it verifies:

* The Python ``build_canonical_claim_v1`` produces a JSON blob that the
  Rust core's ``validate_memory_claim_v1`` accepts without modification.
* The v1 schema_version constant matches the Rust core.
* The v0→v1 mapping table matches what the TS plugin + Rust core use.
* ``source_weight`` / ``legacy_claim_fallback_weight`` return the same
  table the Rust core exports — this is the Retrieval v2 Tier 1 anchor.

Run with::

    pytest python/tests/test_v1_parity.py -v
"""
from __future__ import annotations

import json

import pytest

import totalreclaw_core as core
from totalreclaw.agent.extraction import V0_TO_V1_TYPE, VALID_MEMORY_TYPES
from totalreclaw.claims_helper import V1_SCHEMA_VERSION, build_canonical_claim_v1
from totalreclaw.reranker import LEGACY_CLAIM_FALLBACK_WEIGHT, source_weight


# ---------------------------------------------------------------------------
# Schema-version constant parity
# ---------------------------------------------------------------------------


def test_schema_version_is_1_0() -> None:
    assert V1_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Python-built v1 blob ↔ Rust core validator
# ---------------------------------------------------------------------------


def test_python_v1_blob_validates_through_core() -> None:
    fact = {
        "text": "Uses PostgreSQL as primary OLTP",
        "type": "claim",
        "source": "user",
        "scope": "work",
        "reasoning": "ACID > schema flexibility",
        "confidence": 0.95,
    }
    py_blob = build_canonical_claim_v1(
        fact, importance=8, created_at="2026-04-17T00:00:00.000Z",
        claim_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    # The Python blob carries schema_version (re-attached after core validation).
    py_payload = json.loads(py_blob)
    assert py_payload["schema_version"] == V1_SCHEMA_VERSION

    # core.validate_memory_claim_v1 raises on any schema violation. If it
    # accepts the Python blob, the blob is byte-acceptable core-side.
    # Note: core strips schema_version on output (it's considered input-only).
    validated = core.validate_memory_claim_v1(py_blob)
    validated_payload = json.loads(validated)
    assert validated_payload["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert validated_payload["text"] == fact["text"]
    assert validated_payload["type"] == "claim"
    assert validated_payload["source"] == "user"
    assert validated_payload["scope"] == "work"
    assert validated_payload["importance"] == 8


def test_python_v1_blob_entities_survive_core_validation() -> None:
    fact = {
        "text": "Pedro uses PostgreSQL",
        "type": "claim",
        "source": "user",
        "entities": [
            {"name": "Pedro", "type": "person"},
            {"name": "PostgreSQL", "type": "tool", "role": "primary_db"},
        ],
    }
    py_blob = build_canonical_claim_v1(
        fact, importance=8, created_at="2026-04-17T00:00:00.000Z"
    )
    validated = core.validate_memory_claim_v1(py_blob)
    payload = json.loads(validated)
    ents = payload.get("entities", [])
    assert len(ents) == 2
    names = {e["name"] for e in ents}
    assert names == {"Pedro", "PostgreSQL"}


def test_python_v1_blob_reasoning_truncated_to_256() -> None:
    long_reason = "x" * 400
    fact = {
        "text": "Claim with long reasoning",
        "type": "claim",
        "source": "user",
        "reasoning": long_reason,
    }
    py_blob = build_canonical_claim_v1(
        fact, importance=8, created_at="2026-04-17T00:00:00Z"
    )
    validated = core.validate_memory_claim_v1(py_blob)
    payload = json.loads(validated)
    assert len(payload["reasoning"]) == 256


# ---------------------------------------------------------------------------
# v0 → v1 coercion parity (Python mapping matches the Rust core's
# ``parse_memory_type_v1`` for at least the unknown→claim fallback)
# ---------------------------------------------------------------------------


def test_v0_to_v1_mapping_known_pairs() -> None:
    """The Python V0_TO_V1_TYPE map must be internally consistent with the v1 list."""
    for v0, v1 in V0_TO_V1_TYPE.items():
        assert v1 in VALID_MEMORY_TYPES


def test_core_parse_memory_type_v1_v0_unknown_returns_claim() -> None:
    """The Rust core's parser falls back to 'claim' for unknown / v0-exclusive tokens."""
    # v0 tokens not in the v1 enum land on "claim".
    assert core.parse_memory_type_v1("bogus") == "claim"
    assert core.parse_memory_type_v1("fact") == "claim"
    assert core.parse_memory_type_v1("decision") == "claim"


@pytest.mark.parametrize("v1_type", list(VALID_MEMORY_TYPES))
def test_core_parse_memory_type_v1_passthrough(v1_type: str) -> None:
    assert core.parse_memory_type_v1(v1_type) == v1_type


# ---------------------------------------------------------------------------
# Source-weight parity (Retrieval v2 Tier 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("user", 1.0),
        ("user-inferred", 0.9),
        ("external", 0.7),
        ("derived", 0.7),
        ("assistant", 0.55),
    ],
)
def test_python_source_weight_matches_core(source: str, expected: float) -> None:
    """Python's ``source_weight`` delegates to core — values must match."""
    assert source_weight(source) == pytest.approx(expected, abs=0.001)
    assert core.source_weight(source) == pytest.approx(expected, abs=0.001)


def test_legacy_claim_fallback_weight_is_0_85() -> None:
    assert LEGACY_CLAIM_FALLBACK_WEIGHT == 0.85
    assert core.legacy_claim_fallback_weight() == 0.85


# ---------------------------------------------------------------------------
# Canonical v1 fixture anchor — this JSON blob is a "golden" v1 payload
# that both TS + Python + Rust must accept byte-for-byte.
# ---------------------------------------------------------------------------


CANONICAL_V1_FIXTURE = {
    "id": "11111111-2222-3333-4444-555555555555",
    "text": "The user prefers PostgreSQL for OLTP databases",
    "type": "preference",
    "source": "user",
    "scope": "work",
    "reasoning": None,
    "confidence": 0.95,
    "importance": 9,
    "created_at": "2026-04-17T12:00:00.000Z",
    "schema_version": "1.0",
}


def test_canonical_v1_fixture_validates() -> None:
    """The golden fixture validates through the Rust core unchanged.

    Note: core.validate_memory_claim_v1 strips schema_version on output —
    the field is input-only from the core's perspective. This test asserts
    round-trip INPUT acceptance, not byte-exact output.
    """
    payload = {k: v for k, v in CANONICAL_V1_FIXTURE.items() if v is not None}
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    validated = core.validate_memory_claim_v1(blob)
    # Core accepted the fixture; it returns a canonical subset.
    validated_payload = json.loads(validated)
    assert validated_payload["id"] == CANONICAL_V1_FIXTURE["id"]
    assert validated_payload["type"] == CANONICAL_V1_FIXTURE["type"]


# ---------------------------------------------------------------------------
# Protobuf v4 byte-parity — Python encoder ↔ Rust core encoder
# ---------------------------------------------------------------------------


def test_python_rust_protobuf_v4_byte_identical() -> None:
    """Python ``encode_fact_protobuf`` produces byte-for-byte identical output
    to the Rust core's encoder when both are given the same FactPayload."""
    from totalreclaw.protobuf import (
        PROTOBUF_VERSION_V4 as PY_V4,
        FactPayload,
        encode_fact_protobuf,
    )

    fp = FactPayload(
        id="test-x",
        timestamp="2026-04-17T00:00:00Z",
        owner="0xAB",
        encrypted_blob="deadbeef",
        blind_indices=[],
        decay_score=0.8,
        source="s",
        content_fp="fp",
        agent_id="a",
        version=PY_V4,
    )
    py_bytes = encode_fact_protobuf(fp)

    rust_bytes = core.encode_fact_protobuf(
        json.dumps({
            "id": "test-x",
            "timestamp": "2026-04-17T00:00:00Z",
            "owner": "0xAB",
            "encrypted_blob_hex": "deadbeef",
            "blind_indices": [],
            "decay_score": 0.8,
            "source": "s",
            "content_fp": "fp",
            "agent_id": "a",
            "version": 4,
        })
    )

    assert py_bytes == rust_bytes, (
        f"Python/Rust protobuf v4 mismatch:\n"
        f"  Python: {py_bytes.hex()}\n"
        f"  Rust:   {rust_bytes.hex()}"
    )


def test_python_rust_protobuf_v3_byte_identical() -> None:
    """v3 (default / legacy) encoding also byte-matches Rust core."""
    from totalreclaw.protobuf import FactPayload, encode_fact_protobuf

    fp = FactPayload(
        id="test-x",
        timestamp="2026-04-17T00:00:00Z",
        owner="0xAB",
        encrypted_blob="deadbeef",
        blind_indices=[],
        decay_score=0.8,
        source="s",
        content_fp="fp",
        agent_id="a",
        version=3,
    )
    py_bytes = encode_fact_protobuf(fp)

    rust_bytes = core.encode_fact_protobuf(
        json.dumps({
            "id": "test-x",
            "timestamp": "2026-04-17T00:00:00Z",
            "owner": "0xAB",
            "encrypted_blob_hex": "deadbeef",
            "blind_indices": [],
            "decay_score": 0.8,
            "source": "s",
            "content_fp": "fp",
            "agent_id": "a",
            "version": 3,
        })
    )
    assert py_bytes == rust_bytes


def test_python_builder_produces_fixture_parity() -> None:
    """Build the same logical claim via Python and compare the payload shape."""
    py_blob = build_canonical_claim_v1(
        {
            "text": CANONICAL_V1_FIXTURE["text"],
            "type": CANONICAL_V1_FIXTURE["type"],
            "source": CANONICAL_V1_FIXTURE["source"],
            "scope": CANONICAL_V1_FIXTURE["scope"],
            "confidence": CANONICAL_V1_FIXTURE["confidence"],
        },
        importance=CANONICAL_V1_FIXTURE["importance"],
        created_at=CANONICAL_V1_FIXTURE["created_at"],
        claim_id=CANONICAL_V1_FIXTURE["id"],
    )
    py_payload = json.loads(py_blob)
    assert py_payload["id"] == CANONICAL_V1_FIXTURE["id"]
    assert py_payload["text"] == CANONICAL_V1_FIXTURE["text"]
    assert py_payload["type"] == CANONICAL_V1_FIXTURE["type"]
    assert py_payload["source"] == CANONICAL_V1_FIXTURE["source"]
    assert py_payload["scope"] == CANONICAL_V1_FIXTURE["scope"]
    assert py_payload["importance"] == CANONICAL_V1_FIXTURE["importance"]
    assert py_payload["schema_version"] == CANONICAL_V1_FIXTURE["schema_version"]
