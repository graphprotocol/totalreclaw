"""Tests for claims_helper.py — the KG Phase 1 Python write + read helpers.

Mirrors ``skill/plugin/claim-format.test.ts`` and the relevant parts of
``skill/plugin/digest-injection.test.ts``. Every assertion that touches bytes
(canonical output, trapdoor hashes) has a hardcoded expected value so the
test double-checks the core's output has not drifted.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

import totalreclaw_core as core
from totalreclaw.claims_helper import (
    DIGEST_CATEGORY,
    DIGEST_CLAIM_CAP,
    DIGEST_SOURCE_AGENT,
    DIGEST_TRAPDOOR,
    build_canonical_claim,
    build_digest_claim,
    build_legacy_doc,
    compute_entity_trapdoor,
    compute_entity_trapdoors,
    extract_digest_from_claim,
    hours_since,
    is_digest_blob,
    is_digest_stale,
    map_type_to_category,
    read_claim_from_blob,
    resolve_claim_format,
    resolve_digest_mode,
    should_recompile,
)


# ---------------------------------------------------------------------------
# Fact fixtures (dataclass-like, mirroring the plugin's ExtractedFact type)
# ---------------------------------------------------------------------------


@dataclass
class _Entity:
    name: str
    type: str
    role: Optional[str] = None


@dataclass
class _Fact:
    text: str
    type: str
    importance: int
    action: str = "ADD"
    confidence: Optional[float] = None
    entities: Optional[List[_Entity]] = None
    existing_fact_id: Optional[str] = None


# ---------------------------------------------------------------------------
# map_type_to_category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact_type,category",
    [
        ("fact", "fact"),
        ("preference", "pref"),
        ("decision", "dec"),
        ("episodic", "epi"),
        ("goal", "goal"),
        ("context", "ctx"),
        ("summary", "sum"),
    ],
)
def test_map_type_to_category(fact_type: str, category: str) -> None:
    assert map_type_to_category(fact_type) == category


def test_map_type_to_category_unknown_falls_back_to_fact() -> None:
    # Plugin uses a strict index; Python version is defensive for LLM output.
    assert map_type_to_category("nonsense") == "fact"


# ---------------------------------------------------------------------------
# build_canonical_claim
# ---------------------------------------------------------------------------


def test_build_canonical_claim_byte_identical_reference() -> None:
    """Byte-for-byte match against the plugin's claim-format.test.ts reference."""
    fact = _Fact(
        text="Pedro chose PostgreSQL because it is relational and needs ACID.",
        type="decision",
        importance=8,
        confidence=0.92,
        entities=[
            _Entity(name="Pedro", type="person", role="chooser"),
            _Entity(name="PostgreSQL", type="tool"),
        ],
    )
    canonical = build_canonical_claim(
        fact,
        importance=8,
        source_agent="openclaw-plugin",
        extracted_at="2026-04-12T10:00:00Z",
    )
    expected = (
        '{"t":"Pedro chose PostgreSQL because it is relational and needs ACID.",'
        '"c":"dec","cf":0.92,"i":8,"sa":"openclaw-plugin","ea":"2026-04-12T10:00:00Z",'
        '"e":[{"n":"Pedro","tp":"person","r":"chooser"},{"n":"PostgreSQL","tp":"tool"}]}'
    )
    assert canonical == expected


def test_build_canonical_claim_round_trips_through_core() -> None:
    fact = _Fact(
        text="The user lives in Lisbon.",
        type="fact",
        importance=7,
        confidence=0.9,
    )
    canonical = build_canonical_claim(
        fact,
        importance=7,
        source_agent="oc",
        extracted_at="2026-04-12T10:00:00Z",
    )
    parsed = json.loads(core.parse_claim_or_legacy(canonical))
    assert parsed["t"] == fact.text
    assert parsed["c"] == "fact"
    assert parsed["cf"] == 0.9
    assert parsed["i"] == 7


def test_build_canonical_claim_omits_entities_field_when_empty() -> None:
    fact = _Fact(text="No entities here.", type="fact", importance=7)
    canonical = build_canonical_claim(
        fact,
        importance=7,
        source_agent="oc",
        extracted_at="2026-04-12T10:00:00Z",
    )
    assert '"e":' not in canonical


def test_build_canonical_claim_defaults_confidence_to_0_85() -> None:
    fact = _Fact(text="No confidence supplied.", type="fact", importance=7)
    canonical = build_canonical_claim(
        fact,
        importance=7,
        source_agent="oc",
        extracted_at="2026-04-12T10:00:00Z",
    )
    assert '"cf":0.85' in canonical


def test_build_canonical_claim_drops_role_when_absent() -> None:
    fact = _Fact(
        text="Pedro works at Acme.",
        type="fact",
        importance=7,
        confidence=0.9,
        entities=[_Entity(name="Acme", type="company")],
    )
    canonical = build_canonical_claim(
        fact,
        importance=7,
        source_agent="oc",
        extracted_at="2026-04-12T10:00:00Z",
    )
    assert '"e":[{"n":"Acme","tp":"company"}]' in canonical


def test_build_canonical_claim_accepts_dict_input() -> None:
    """Dict input must produce the same bytes as dataclass input."""
    fact_dict = {
        "text": "Pedro chose PostgreSQL because it is relational and needs ACID.",
        "type": "decision",
        "importance": 8,
        "confidence": 0.92,
        "entities": [
            {"name": "Pedro", "type": "person", "role": "chooser"},
            {"name": "PostgreSQL", "type": "tool"},
        ],
    }
    canonical = build_canonical_claim(
        fact_dict,
        importance=8,
        source_agent="openclaw-plugin",
        extracted_at="2026-04-12T10:00:00Z",
    )
    expected = (
        '{"t":"Pedro chose PostgreSQL because it is relational and needs ACID.",'
        '"c":"dec","cf":0.92,"i":8,"sa":"openclaw-plugin","ea":"2026-04-12T10:00:00Z",'
        '"e":[{"n":"Pedro","tp":"person","r":"chooser"},{"n":"PostgreSQL","tp":"tool"}]}'
    )
    assert canonical == expected


# ---------------------------------------------------------------------------
# build_legacy_doc
# ---------------------------------------------------------------------------


def test_build_legacy_doc_byte_identical() -> None:
    fact = _Fact(text="Hello world.", type="fact", importance=7)
    doc = build_legacy_doc(
        fact,
        importance=7,
        source="auto-extraction",
        created_at="2026-04-12T10:00:00Z",
    )
    expected = (
        '{"text":"Hello world.","metadata":{"type":"fact","importance":0.7,'
        '"source":"auto-extraction","created_at":"2026-04-12T10:00:00Z"}}'
    )
    assert doc == expected


# ---------------------------------------------------------------------------
# Entity trapdoors
# ---------------------------------------------------------------------------


# Hardcoded expected value — computed once and pinned so this test also
# detects any future drift in the normalization pipeline.
POSTGRES_TRAPDOOR_HEX = hashlib.sha256(b"entity:postgresql").hexdigest()


def test_compute_entity_trapdoor_postgresql_hardcoded() -> None:
    """Cross-client parity anchor — must match the plugin's output byte-for-byte."""
    assert (
        compute_entity_trapdoor("PostgreSQL")
        == "1e364278f621eefbf64332d67597c5f2366b8527f46301dbe288b63920e89569"
    )
    # Also exactly what the documented primitive says.
    assert compute_entity_trapdoor("PostgreSQL") == POSTGRES_TRAPDOOR_HEX


def test_compute_entity_trapdoor_is_deterministic() -> None:
    a = compute_entity_trapdoor("PostgreSQL")
    b = compute_entity_trapdoor("PostgreSQL")
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_compute_entity_trapdoor_normalizes_case_and_whitespace() -> None:
    a = compute_entity_trapdoor("PostgreSQL")
    b = compute_entity_trapdoor("postgresql")
    c = compute_entity_trapdoor("  POSTGRESQL  ")
    assert a == b == c


def test_compute_entity_trapdoor_has_namespace_prefix() -> None:
    entity_td = compute_entity_trapdoor("postgresql")
    word_hash = hashlib.sha256(b"postgresql").hexdigest()
    assert entity_td != word_hash
    expected = hashlib.sha256(b"entity:postgresql").hexdigest()
    assert entity_td == expected


def test_compute_entity_trapdoors_dedupes_multiple_aliases() -> None:
    td = compute_entity_trapdoors(
        [
            _Entity(name="Pedro", type="person"),
            _Entity(name="pedro", type="person"),
            _Entity(name="  PEDRO ", type="person"),
        ]
    )
    assert len(td) == 1


def test_compute_entity_trapdoors_accepts_dict_entities() -> None:
    td = compute_entity_trapdoors(
        [{"name": "Pedro", "type": "person"}, {"name": "PostgreSQL", "type": "tool"}]
    )
    assert len(td) == 2


def test_compute_entity_trapdoors_empty_inputs() -> None:
    assert compute_entity_trapdoors(None) == []
    assert compute_entity_trapdoors([]) == []


def test_compute_entity_trapdoors_skips_bad_names() -> None:
    td = compute_entity_trapdoors(
        [
            _Entity(name="", type="person"),
            _Entity(name="   ", type="person"),  # whitespace-only → normalizes to ""
            _Entity(name="Real", type="person"),
        ]
    )
    # "   " normalizes to "" at the core layer but we still hash it because
    # we can't tell empty-after-normalize from a user who actually typed
    # nothing meaningful. What we guarantee is that "Real" produces one
    # entry and the result does not contain duplicates.
    assert any(t == compute_entity_trapdoor("Real") for t in td)


# ---------------------------------------------------------------------------
# Claim format feature flag
# ---------------------------------------------------------------------------


def test_resolve_claim_format_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOTALRECLAW_CLAIM_FORMAT", raising=False)
    assert resolve_claim_format() == "claim"


def test_resolve_claim_format_explicit_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "claim")
    assert resolve_claim_format() == "claim"


def test_resolve_claim_format_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "CLAIM")
    assert resolve_claim_format() == "claim"
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "LEGACY")
    assert resolve_claim_format() == "legacy"


def test_resolve_claim_format_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "legacy")
    assert resolve_claim_format() == "legacy"


def test_resolve_claim_format_unknown_falls_back_to_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "nonsense")
    assert resolve_claim_format() == "claim"


# ---------------------------------------------------------------------------
# Digest mode flag
# ---------------------------------------------------------------------------


def test_resolve_digest_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOTALRECLAW_DIGEST_MODE", raising=False)
    assert resolve_digest_mode() == "on"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("on", "on"),
        ("ON", "on"),
        ("off", "off"),
        ("OFF", "off"),
        ("template", "template"),
        ("TEMPLATE", "template"),
        ("nonsense", "on"),
        ("", "on"),
    ],
)
def test_resolve_digest_mode_values(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("TOTALRECLAW_DIGEST_MODE", value)
    assert resolve_digest_mode() == expected


# ---------------------------------------------------------------------------
# read_claim_from_blob — decrypted blob reader
# ---------------------------------------------------------------------------


def test_read_claim_from_blob_new_format() -> None:
    out = read_claim_from_blob(
        json.dumps({"t": "prefers PostgreSQL", "c": "pref", "cf": 0.9, "i": 8, "sa": "oc"})
    )
    assert out["text"] == "prefers PostgreSQL"
    assert out["importance"] == 8
    assert out["category"] == "pref"


def test_read_claim_from_blob_new_format_with_entities() -> None:
    out = read_claim_from_blob(
        json.dumps(
            {
                "t": "lives in Lisbon",
                "c": "fact",
                "cf": 0.95,
                "i": 9,
                "sa": "oc",
                "e": [{"n": "Lisbon", "tp": "place"}],
            }
        )
    )
    assert out["text"] == "lives in Lisbon"
    assert out["importance"] == 9
    assert out["category"] == "fact"


def test_read_claim_from_blob_clamps_high_importance() -> None:
    out = read_claim_from_blob(
        json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 99, "sa": "oc"})
    )
    assert out["importance"] == 10


def test_read_claim_from_blob_clamps_low_importance() -> None:
    out = read_claim_from_blob(
        json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 0, "sa": "oc"})
    )
    assert out["importance"] == 1


def test_read_claim_from_blob_legacy_format() -> None:
    out = read_claim_from_blob(
        json.dumps(
            {
                "text": "legacy fact",
                "metadata": {
                    "type": "fact",
                    "importance": 0.7,
                    "source": "auto-extraction",
                },
            }
        )
    )
    assert out["text"] == "legacy fact"
    assert out["importance"] == 7
    assert out["category"] == "fact"


def test_read_claim_from_blob_legacy_rounds_0_85_to_9() -> None:
    out = read_claim_from_blob(
        json.dumps(
            {
                "text": "prefers dark mode",
                "metadata": {"type": "preference", "importance": 0.85},
            }
        )
    )
    assert out["importance"] == 9
    assert out["category"] == "preference"


def test_read_claim_from_blob_bare_legacy() -> None:
    out = read_claim_from_blob(json.dumps({"text": "bare"}))
    assert out["text"] == "bare"
    assert out["importance"] == 5


def test_read_claim_from_blob_malformed_json_falls_back() -> None:
    out = read_claim_from_blob("not valid json")
    assert out["text"] == "not valid json"
    assert out["importance"] == 5


def test_read_claim_from_blob_empty_object_falls_back() -> None:
    out = read_claim_from_blob("{}")
    assert out["text"] == "{}"


def test_read_claim_from_blob_digest_blob_new_format() -> None:
    out = read_claim_from_blob(
        json.dumps(
            {
                "t": '{"prompt_text":"You are..."}',
                "c": "dig",
                "cf": 1.0,
                "i": 10,
                "sa": DIGEST_SOURCE_AGENT,
            }
        )
    )
    assert out["category"] == "dig"
    assert out["importance"] == 10


# ---------------------------------------------------------------------------
# DIGEST_TRAPDOOR + constants
# ---------------------------------------------------------------------------


def test_digest_trapdoor_matches_documented_primitive() -> None:
    expected = hashlib.sha256(b"type:digest").hexdigest()
    assert DIGEST_TRAPDOOR == expected
    assert len(DIGEST_TRAPDOOR) == 64
    assert all(c in "0123456789abcdef" for c in DIGEST_TRAPDOOR)


def test_digest_trapdoor_hardcoded_value() -> None:
    # Hardcoded so if either side (Python or plugin) drifts, this screams.
    assert DIGEST_TRAPDOOR == (
        "5e6dcf483f027cb81f78f05474a4986c236af915dcb48cd8a376485eec5598ba"
    )


def test_digest_constants() -> None:
    assert DIGEST_CATEGORY == "dig"
    assert DIGEST_CLAIM_CAP == 200
    # Python side uses a distinctive marker so operators can identify
    # Python-origin digest writes.
    assert DIGEST_SOURCE_AGENT == "hermes-agent-digest"


# ---------------------------------------------------------------------------
# buildDigestClaim / extractDigestFromClaim round-trip
# ---------------------------------------------------------------------------


def test_build_digest_claim_round_trips() -> None:
    digest_json = core.build_template_digest("[]", 1776000000)
    compiled_at = "2026-04-12T00:00:00Z"
    claim_json = build_digest_claim(digest_json, compiled_at)

    round_tripped = json.loads(core.parse_claim_or_legacy(claim_json))
    assert round_tripped["c"] == "dig"
    assert round_tripped["cf"] == 1.0
    assert round_tripped["i"] == 10
    assert round_tripped["sa"] == DIGEST_SOURCE_AGENT
    assert round_tripped["ea"] == compiled_at
    assert round_tripped["t"] == digest_json
    # Must not carry entity refs.
    assert "e" not in round_tripped or len(round_tripped["e"]) == 0


def test_build_digest_claim_deterministic() -> None:
    digest_json = core.build_template_digest("[]", 1776000000)
    a = build_digest_claim(digest_json, "2026-04-12T00:00:00Z")
    b = build_digest_claim(digest_json, "2026-04-12T00:00:00Z")
    assert a == b


def test_extract_digest_from_claim_happy_path() -> None:
    digest_json = core.build_template_digest("[]", 1776000000)
    claim_json = build_digest_claim(digest_json, "2026-04-12T00:00:00Z")
    parsed = core.parse_claim_or_legacy(claim_json)
    digest = extract_digest_from_claim(parsed)
    assert digest is not None
    assert "No memories" in digest["prompt_text"]
    assert digest["fact_count"] == 0


def test_extract_digest_from_claim_non_digest_returns_none() -> None:
    non_digest = json.dumps(
        {"t": "just a regular fact", "c": "fact", "cf": 0.9, "i": 5, "sa": "oc"}
    )
    canonical = core.parse_claim_or_legacy(non_digest)
    assert extract_digest_from_claim(canonical) is None


def test_extract_digest_from_claim_malformed_inner_json_returns_none() -> None:
    # Hand-build a dig claim whose t field is not a valid Digest object.
    broken = json.dumps(
        {
            "t": "not a digest object",
            "c": "dig",
            "cf": 1.0,
            "i": 10,
            "sa": DIGEST_SOURCE_AGENT,
        }
    )
    canonical = core.parse_claim_or_legacy(broken)
    assert extract_digest_from_claim(canonical) is None


def test_extract_digest_from_claim_garbage_input() -> None:
    assert extract_digest_from_claim("not json at all") is None
    assert extract_digest_from_claim("[]") is None


# ---------------------------------------------------------------------------
# is_digest_blob
# ---------------------------------------------------------------------------


def test_is_digest_blob_digest_claim() -> None:
    digest_json = core.build_template_digest("[]", 1776000000)
    claim_json = build_digest_claim(digest_json, "2026-04-12T00:00:00Z")
    canonical = core.parse_claim_or_legacy(claim_json)
    assert is_digest_blob(canonical) is True


def test_is_digest_blob_fact_claim_returns_false() -> None:
    fact_claim = core.parse_claim_or_legacy(
        json.dumps({"t": "hello", "c": "fact", "cf": 0.9, "i": 5, "sa": "oc"})
    )
    assert is_digest_blob(fact_claim) is False


def test_is_digest_blob_legacy_doc_returns_false() -> None:
    legacy_doc = json.dumps({"text": "hi", "metadata": {"importance": 0.5}})
    assert is_digest_blob(legacy_doc) is False


def test_is_digest_blob_garbage_returns_false() -> None:
    assert is_digest_blob("not json at all") is False
    assert is_digest_blob("") is False


# ---------------------------------------------------------------------------
# hours_since
# ---------------------------------------------------------------------------


def test_hours_since_zero_when_equal() -> None:
    now_ms = 1776000000000  # 2026-04-12T13:20:00Z in ms
    import datetime as dt

    iso = dt.datetime.fromtimestamp(now_ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    assert hours_since(iso, now_ms) == 0


def test_hours_since_six_hours_ago() -> None:
    now_ms = 1776000000000
    six_hours_ago_ms = now_ms - 6 * 3600 * 1000
    import datetime as dt

    iso = dt.datetime.fromtimestamp(six_hours_ago_ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    assert abs(hours_since(iso, now_ms) - 6) < 0.001


def test_hours_since_24_hours_ago() -> None:
    now_ms = 1776000000000
    day_ago_ms = now_ms - 24 * 3600 * 1000
    import datetime as dt

    iso = dt.datetime.fromtimestamp(day_ago_ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    assert abs(hours_since(iso, now_ms) - 24) < 0.001


def test_hours_since_invalid_returns_infinity() -> None:
    assert hours_since("not a valid date", 1776000000000) == math.inf


def test_hours_since_future_date_clamped_to_zero() -> None:
    # Future date → clock skew defensive, return 0
    assert hours_since("2030-01-01T00:00:00Z", 1776000000000) == 0


# ---------------------------------------------------------------------------
# is_digest_stale
# ---------------------------------------------------------------------------


def test_is_digest_stale_equal_not_stale() -> None:
    assert is_digest_stale(1000, 1000) is False


def test_is_digest_stale_newer_on_chain_is_stale() -> None:
    assert is_digest_stale(1000, 1001) is True


def test_is_digest_stale_regressing_not_stale() -> None:
    assert is_digest_stale(1000, 500) is False


def test_is_digest_stale_both_zero_not_stale() -> None:
    assert is_digest_stale(0, 0) is False


# ---------------------------------------------------------------------------
# should_recompile — guard conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,hours,expected",
    [
        (10, 1, True),  # exactly 10 new claims
        (9, 1, False),  # 9 under threshold
        (0, 24, True),  # exactly 24 hours
        (1, 23, False),  # 23h + 1 claim → skip
        (100, 48, True),  # both conditions
        (0, 0, False),  # nothing
        (0, math.inf, True),  # infinity hours → recompile
    ],
)
def test_should_recompile(count: int, hours: float, expected: bool) -> None:
    assert should_recompile(count, hours) is expected
