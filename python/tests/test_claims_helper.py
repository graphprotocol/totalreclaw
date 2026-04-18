"""Tests for claims_helper.py — v1 write + read helpers (python 2.0.0).

Mirrors ``skill/plugin/claim-format.test.ts`` (v1 round-trip assertions) and
keeps the digest / entity-trapdoor parity anchors that were byte-exact
before the v1 switch.

What changed from python 1.x:
  * ``build_canonical_claim`` now emits a v1 JSON blob unconditionally.
  * The ``TOTALRECLAW_CLAIM_FORMAT=legacy`` env-var gate was removed.
  * Byte-exact reference fixtures for the v0 short-key format are gone —
    the v1 round-trip assertions replace them.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import List, Optional

import pytest

import totalreclaw_core as core
from totalreclaw.claims_helper import (
    DIGEST_CATEGORY,
    DIGEST_CLAIM_CAP,
    DIGEST_SOURCE_AGENT,
    DIGEST_TRAPDOOR,
    PROTOBUF_VERSION_V4,
    TYPE_TO_CATEGORY_V1,
    V1_SCHEMA_VERSION,
    build_canonical_claim,
    build_canonical_claim_v1,
    build_digest_claim,
    build_legacy_doc,
    compute_entity_trapdoor,
    compute_entity_trapdoors,
    extract_digest_from_claim,
    hours_since,
    is_digest_blob,
    is_digest_stale,
    is_v1_blob,
    map_type_to_category,
    read_blob_unified,
    read_claim_from_blob,
    resolve_digest_mode,
    should_recompile,
)


# ---------------------------------------------------------------------------
# Test fixtures (dataclass mirrors the Python ExtractedFact shape)
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
    # v1 fields
    source: Optional[str] = "user"
    scope: Optional[str] = "unspecified"
    reasoning: Optional[str] = None
    volatility: Optional[str] = None


# ---------------------------------------------------------------------------
# map_type_to_category — v1 types map to display tags used by the recall UI.
# Legacy v0 tokens still decode (read path), so their category short keys
# are retained in ``TYPE_TO_CATEGORY_V0``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact_type,category",
    [
        ("claim", "claim"),
        ("preference", "pref"),
        ("directive", "rule"),
        ("commitment", "goal"),
        ("episode", "epi"),
        ("summary", "sum"),
    ],
)
def test_map_type_to_category_v1(fact_type: str, category: str) -> None:
    assert map_type_to_category(fact_type) == category


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
        ("rule", "rule"),
    ],
)
def test_map_type_to_category_v0_still_decodes(fact_type: str, category: str) -> None:
    """Legacy v0 tokens still map to a category so pre-v1 blobs stay decodable."""
    # v1 map takes priority for overlap: "preference" and "summary" are in both.
    expected = TYPE_TO_CATEGORY_V1.get(fact_type, category)
    assert map_type_to_category(fact_type) == expected


def test_map_type_to_category_unknown_falls_back_to_fact() -> None:
    assert map_type_to_category("nonsense") == "fact"


# ---------------------------------------------------------------------------
# build_canonical_claim / build_canonical_claim_v1 — v1 write path
# ---------------------------------------------------------------------------


def test_build_canonical_claim_emits_v1_schema_version() -> None:
    """Default path writes a v1 JSON blob with schema_version '1.0'."""
    fact = _Fact(
        text="The user lives in Lisbon.",
        type="claim",
        importance=7,
        confidence=0.9,
        source="user",
        scope="personal",
    )
    canonical = build_canonical_claim(
        fact,
        importance=7,
        source_agent="python-client",  # ignored in v1
        extracted_at="2026-04-17T10:00:00.000Z",
    )
    payload = json.loads(canonical)
    assert payload["schema_version"] == V1_SCHEMA_VERSION
    assert payload["text"] == fact.text
    assert payload["type"] == "claim"
    assert payload["source"] == "user"
    assert payload["scope"] == "personal"
    assert payload["importance"] == 7
    assert payload["created_at"] == "2026-04-17T10:00:00.000Z"


def test_build_canonical_claim_v1_with_reasoning() -> None:
    fact = {
        "text": "Chose PostgreSQL over MongoDB for data that needs ACID",
        "type": "claim",
        "source": "user",
        "scope": "work",
        "reasoning": "Transactional integrity matters more than schema flexibility",
        "confidence": 0.95,
    }
    canonical = build_canonical_claim_v1(
        fact,
        importance=9,
        created_at="2026-04-17T10:00:00.000Z",
    )
    payload = json.loads(canonical)
    assert payload["reasoning"] == (
        "Transactional integrity matters more than schema flexibility"
    )
    assert payload["scope"] == "work"
    assert payload["importance"] == 9


def test_build_canonical_claim_v1_legacy_v0_types_coerced() -> None:
    """Legacy v0 type tokens map to their v1 equivalent at write time."""
    fact = {"text": "x", "type": "decision", "source": "user"}
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=7, created_at="2026-04-17T00:00:00.000Z")
    )
    assert payload["type"] == "claim"  # decision → claim

    fact["type"] = "rule"
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=7, created_at="2026-04-17T00:00:00.000Z")
    )
    assert payload["type"] == "directive"

    fact["type"] = "goal"
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=7, created_at="2026-04-17T00:00:00.000Z")
    )
    assert payload["type"] == "commitment"


def test_build_canonical_claim_v1_requires_source() -> None:
    """A v1 claim without a source must raise — provenance is mandatory."""
    fact = {"text": "x", "type": "claim"}  # no source
    with pytest.raises(ValueError, match="source is required"):
        build_canonical_claim_v1(fact, importance=5, created_at="2026-04-17T00:00:00.000Z")


def test_build_canonical_claim_defaults_missing_source_to_user_inferred() -> None:
    """The back-compat entry point supplies user-inferred when source is missing."""
    fact_dict = {"text": "x", "type": "claim"}  # no source key
    canonical = build_canonical_claim(fact_dict, importance=7, source_agent="whatever")
    payload = json.loads(canonical)
    assert payload["source"] == "user-inferred"


def test_build_canonical_claim_v1_entities_passed_through() -> None:
    fact = {
        "text": "Pedro chose PostgreSQL",
        "type": "claim",
        "source": "user",
        "entities": [
            {"name": "Pedro", "type": "person", "role": "chooser"},
            {"name": "PostgreSQL", "type": "tool"},
        ],
    }
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=8, created_at="2026-04-17T00:00:00.000Z")
    )
    assert len(payload["entities"]) == 2
    assert payload["entities"][0]["name"] == "Pedro"
    assert payload["entities"][0]["role"] == "chooser"
    assert payload["entities"][1]["name"] == "PostgreSQL"
    assert "role" not in payload["entities"][1]


def test_build_canonical_claim_v1_volatility_preserved() -> None:
    fact = {
        "text": "x",
        "type": "claim",
        "source": "user",
        "volatility": "stable",
    }
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=7, created_at="2026-04-17T00:00:00.000Z")
    )
    assert payload["volatility"] == "stable"


def test_build_canonical_claim_v1_rejects_invalid_source() -> None:
    fact = {"text": "x", "type": "claim", "source": "bogus"}
    with pytest.raises(ValueError, match="invalid source"):
        build_canonical_claim_v1(fact, importance=5, created_at="2026-04-17T00:00:00.000Z")


def test_protobuf_version_v4_constant_is_4() -> None:
    assert PROTOBUF_VERSION_V4 == 4


# ---------------------------------------------------------------------------
# Legacy doc builder — kept for back-compat, not on the default write path
# ---------------------------------------------------------------------------


def test_build_legacy_doc_still_works_for_back_compat() -> None:
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
# Entity trapdoors — byte-exact cross-client parity anchors
# ---------------------------------------------------------------------------


POSTGRES_TRAPDOOR_HEX = hashlib.sha256(b"entity:postgresql").hexdigest()


def test_compute_entity_trapdoor_postgresql_hardcoded() -> None:
    """Cross-client parity anchor — must match the plugin's output byte-for-byte."""
    assert (
        compute_entity_trapdoor("PostgreSQL")
        == "1e364278f621eefbf64332d67597c5f2366b8527f46301dbe288b63920e89569"
    )
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
            _Entity(name="   ", type="person"),
            _Entity(name="Real", type="person"),
        ]
    )
    assert any(t == compute_entity_trapdoor("Real") for t in td)


# ---------------------------------------------------------------------------
# Digest mode flag (digest compilation is orthogonal to v0/v1 taxonomy)
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
# read_blob_unified / read_claim_from_blob — unified decrypt reader
# ---------------------------------------------------------------------------


def test_read_blob_unified_v1_payload() -> None:
    """v1 JSON blob is decoded with all v1 metadata surfaced."""
    v1 = json.dumps({
        "id": "abc",
        "text": "prefers dark mode",
        "type": "preference",
        "source": "user",
        "scope": "personal",
        "volatility": "stable",
        "reasoning": None,
        "importance": 8,
        "confidence": 0.95,
        "created_at": "2026-04-17T00:00:00.000Z",
        "schema_version": "1.0",
    })
    out = read_blob_unified(v1)
    assert out["text"] == "prefers dark mode"
    assert out["importance"] == 8
    assert out["category"] == "pref"
    assert out["metadata"]["source"] == "user"
    assert out["metadata"]["scope"] == "personal"
    assert out["metadata"]["volatility"] == "stable"
    assert out["metadata"]["schema_version"] == "1.0"


def test_is_v1_blob_detects_v1() -> None:
    v1 = json.dumps({
        "id": "abc", "text": "x", "type": "claim", "source": "user",
        "importance": 5, "created_at": "2026-04-17T00:00:00Z",
        "schema_version": "1.0",
    })
    assert is_v1_blob(v1) is True


def test_is_v1_blob_rejects_legacy() -> None:
    v0 = json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 5, "sa": "oc"})
    assert is_v1_blob(v0) is False
    legacy = json.dumps({"text": "x", "metadata": {}})
    assert is_v1_blob(legacy) is False
    assert is_v1_blob("not json") is False


def test_read_blob_unified_v0_short_key_still_works() -> None:
    """Pre-v1 vault entries still decode via the v0 short-key branch."""
    out = read_blob_unified(
        json.dumps({"t": "prefers PostgreSQL", "c": "pref", "cf": 0.9, "i": 8, "sa": "oc"})
    )
    assert out["text"] == "prefers PostgreSQL"
    assert out["importance"] == 8
    assert out["category"] == "pref"


def test_read_blob_unified_v0_with_entities() -> None:
    out = read_blob_unified(
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


def test_read_blob_unified_clamps_high_importance() -> None:
    out = read_blob_unified(
        json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 99, "sa": "oc"})
    )
    assert out["importance"] == 10


def test_read_blob_unified_clamps_low_importance() -> None:
    out = read_blob_unified(
        json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 0, "sa": "oc"})
    )
    assert out["importance"] == 1


def test_read_blob_unified_legacy_format() -> None:
    out = read_blob_unified(
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


def test_read_blob_unified_legacy_rounds_0_85_to_9() -> None:
    out = read_blob_unified(
        json.dumps(
            {
                "text": "prefers dark mode",
                "metadata": {"type": "preference", "importance": 0.85},
            }
        )
    )
    assert out["importance"] == 9
    assert out["category"] == "preference"


def test_read_blob_unified_bare_legacy() -> None:
    out = read_blob_unified(json.dumps({"text": "bare"}))
    assert out["text"] == "bare"
    assert out["importance"] == 5


def test_read_blob_unified_malformed_json_falls_back() -> None:
    out = read_blob_unified("not valid json")
    assert out["text"] == "not valid json"
    assert out["importance"] == 5


def test_read_blob_unified_empty_object_falls_back() -> None:
    out = read_blob_unified("{}")
    assert out["text"] == "{}"


def test_read_blob_unified_digest_blob() -> None:
    out = read_blob_unified(
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


def test_read_claim_from_blob_back_compat_alias() -> None:
    """read_claim_from_blob remains a back-compat alias for read_blob_unified."""
    v0 = json.dumps({"t": "x", "c": "fact", "cf": 0.9, "i": 5, "sa": "oc"})
    assert read_claim_from_blob(v0) == read_blob_unified(v0)


# ---------------------------------------------------------------------------
# DIGEST_TRAPDOOR + constants (unchanged from v0)
# ---------------------------------------------------------------------------


def test_digest_trapdoor_matches_documented_primitive() -> None:
    expected = hashlib.sha256(b"type:digest").hexdigest()
    assert DIGEST_TRAPDOOR == expected
    assert len(DIGEST_TRAPDOOR) == 64


def test_digest_trapdoor_hardcoded_value() -> None:
    assert DIGEST_TRAPDOOR == (
        "5e6dcf483f027cb81f78f05474a4986c236af915dcb48cd8a376485eec5598ba"
    )


def test_digest_constants() -> None:
    assert DIGEST_CATEGORY == "dig"
    assert DIGEST_CLAIM_CAP == 200
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
    now_ms = 1776000000000
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
# should_recompile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,hours,expected",
    [
        (10, 1, True),
        (9, 1, False),
        (0, 24, True),
        (1, 23, False),
        (100, 48, True),
        (0, 0, False),
        (0, math.inf, True),
    ],
)
def test_should_recompile(count: int, hours: float, expected: bool) -> None:
    assert should_recompile(count, hours) is expected


# ---------------------------------------------------------------------------
# Explicit env-var absence: v1 is the default, no gates
# ---------------------------------------------------------------------------


def test_no_taxonomy_version_env_var_referenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOTALRECLAW_TAXONOMY_VERSION should have zero effect (removed in 2.0.0)."""
    # Set the legacy env var to a value that would have flipped to v0
    monkeypatch.setenv("TOTALRECLAW_TAXONOMY_VERSION", "v0")
    fact = {"text": "x", "type": "claim", "source": "user"}
    payload = json.loads(
        build_canonical_claim_v1(fact, importance=7, created_at="2026-04-17T00:00:00Z")
    )
    # Must still emit v1 (schema_version "1.0")
    assert payload["schema_version"] == V1_SCHEMA_VERSION


def test_no_claim_format_env_var_referenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOTALRECLAW_CLAIM_FORMAT=legacy should have zero effect (removed in 2.0.0)."""
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "legacy")
    fact = {"text": "x", "type": "claim", "source": "user"}
    canonical = build_canonical_claim(fact, importance=7)
    # Even with the env var, we emit v1 — no legacy doc.
    assert '"schema_version"' in canonical
    assert '"text":"x"' in canonical
