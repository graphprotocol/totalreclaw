"""Tests for Memory Taxonomy v1 end-to-end in the Python client.

Mirrors the plugin's ``v1-taxonomy.test.ts`` — every assertion that matters
for cross-client parity has a direct TS counterpart. Run with::

    pytest python/tests/test_v1_taxonomy.py -v
"""
from __future__ import annotations

import json

import pytest

import totalreclaw_core as core
from totalreclaw.agent.extraction import (
    COMPACTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    LEGACY_V0_MEMORY_TYPES,
    V0_TO_V1_TYPE,
    VALID_MEMORY_SCOPES,
    VALID_MEMORY_SOURCES,
    VALID_MEMORY_TYPES,
    VALID_MEMORY_VOLATILITIES,
    ExtractedFact,
    apply_provenance_filter_lax,
    default_volatility,
    is_valid_memory_type,
    normalize_to_v1_type,
    parse_facts_response,
    parse_facts_response_for_compaction,
    parse_merged_response_v1,
)
from totalreclaw.claims_helper import (
    PROTOBUF_VERSION_V4,
    V1_SCHEMA_VERSION,
    build_canonical_claim,
    build_canonical_claim_v1,
    is_v1_blob,
    read_blob_unified,
)
from totalreclaw.reranker import (
    LEGACY_CLAIM_FALLBACK_WEIGHT,
    RerankerCandidate,
    rerank,
    source_weight,
)


# ---------------------------------------------------------------------------
# v1 type guards + mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("v1_type", ["claim", "preference", "directive", "commitment", "episode", "summary"])
def test_v1_guard_accepts_all_v1_types(v1_type: str) -> None:
    assert is_valid_memory_type(v1_type)


@pytest.mark.parametrize("v0_type", ["fact", "decision", "episodic", "goal", "context", "rule"])
def test_v1_guard_rejects_legacy_v0_tokens(v0_type: str) -> None:
    """v0 tokens fail the v1 guard — they must be coerced via normalize_to_v1_type."""
    # "preference" and "summary" overlap between v0 and v1 lists — skip those.
    assert not is_valid_memory_type(v0_type)


def test_v1_guard_rejects_unknown() -> None:
    assert not is_valid_memory_type("bogus")
    assert not is_valid_memory_type(42)
    assert not is_valid_memory_type(None)


@pytest.mark.parametrize(
    "v0,v1",
    [
        ("fact", "claim"),
        ("decision", "claim"),
        ("context", "claim"),
        ("rule", "directive"),
        ("goal", "commitment"),
        ("episodic", "episode"),
        ("preference", "preference"),
        ("summary", "summary"),
    ],
)
def test_normalize_v0_to_v1(v0: str, v1: str) -> None:
    assert normalize_to_v1_type(v0) == v1


@pytest.mark.parametrize("v1_type", VALID_MEMORY_TYPES)
def test_normalize_v1_passthrough(v1_type: str) -> None:
    assert normalize_to_v1_type(v1_type) == v1_type


def test_normalize_unknown_falls_back_to_claim() -> None:
    assert normalize_to_v1_type("bogus") == "claim"
    assert normalize_to_v1_type("") == "claim"
    assert normalize_to_v1_type(None) == "claim"


def test_v0_to_v1_mapping_completeness() -> None:
    """Every v0 token has an explicit v1 destination."""
    for v0 in LEGACY_V0_MEMORY_TYPES:
        assert v0 in V0_TO_V1_TYPE
        assert V0_TO_V1_TYPE[v0] in VALID_MEMORY_TYPES


# ---------------------------------------------------------------------------
# v1 canonical claim round-trip (build → validate → parse)
# ---------------------------------------------------------------------------


def test_v1_claim_round_trip() -> None:
    """Build a v1 claim, validate through core, then re-parse and check fields."""
    fact = {
        "text": "Uses PostgreSQL as the primary OLTP database",
        "type": "claim",
        "source": "user",
        "scope": "work",
        "reasoning": "ACID matters more than schema flexibility",
        "confidence": 0.95,
        "volatility": "updatable",
    }
    blob = build_canonical_claim_v1(
        fact,
        importance=8,
        created_at="2026-04-17T00:00:00.000Z",
        claim_id="deadbeef-dead-beef-dead-beefdeadbeef",
    )
    # Must be valid JSON with schema_version "1.0"
    payload = json.loads(blob)
    assert payload["schema_version"] == V1_SCHEMA_VERSION
    assert payload["id"] == "deadbeef-dead-beef-dead-beefdeadbeef"
    assert payload["text"] == fact["text"]
    assert payload["type"] == "claim"
    assert payload["source"] == "user"
    assert payload["scope"] == "work"
    assert payload["reasoning"] == "ACID matters more than schema flexibility"
    assert payload["importance"] == 8
    assert payload["confidence"] == 0.95
    assert payload["volatility"] == "updatable"
    assert payload["created_at"] == "2026-04-17T00:00:00.000Z"

    # Must be detectable as v1 by the unified reader.
    assert is_v1_blob(blob)

    # read_blob_unified must surface all v1 metadata fields.
    decoded = read_blob_unified(blob)
    assert decoded["text"] == fact["text"]
    assert decoded["category"] == "claim"
    assert decoded["importance"] == 8
    assert decoded["metadata"]["source"] == "user"
    assert decoded["metadata"]["scope"] == "work"
    assert decoded["metadata"]["volatility"] == "updatable"
    assert decoded["metadata"]["reasoning"] == fact["reasoning"]


def test_v1_claim_entities_preserved() -> None:
    fact = {
        "text": "Pedro chose Rust over Go",
        "type": "claim",
        "source": "user",
        "entities": [
            {"name": "Pedro", "type": "person", "role": "chooser"},
            {"name": "Rust", "type": "tool"},
            {"name": "Go", "type": "tool"},
        ],
    }
    blob = build_canonical_claim_v1(fact, importance=8, created_at="2026-04-17T00:00:00.000Z")
    payload = json.loads(blob)
    assert len(payload["entities"]) == 3


def test_protobuf_version_v4_is_4() -> None:
    assert PROTOBUF_VERSION_V4 == 4


# ---------------------------------------------------------------------------
# Backward-compat decrypt (v0 short-key + legacy docs still decodable)
# ---------------------------------------------------------------------------


def test_backward_compat_decrypt_v0_short_key() -> None:
    """Pre-v1 vaults still decode via the v0 short-key branch."""
    v0 = json.dumps({"t": "legacy preference", "c": "pref", "cf": 0.9, "i": 7, "sa": "oc"})
    out = read_blob_unified(v0)
    assert out["text"] == "legacy preference"
    assert out["importance"] == 7
    assert out["category"] == "pref"


def test_backward_compat_decrypt_plugin_legacy_doc() -> None:
    """Pre-KG legacy docs with ``{text, metadata}`` still decode."""
    legacy = json.dumps({
        "text": "really old fact",
        "metadata": {"type": "fact", "importance": 0.6, "source": "auto"},
    })
    out = read_blob_unified(legacy)
    assert out["text"] == "really old fact"
    assert out["importance"] == 6
    assert out["category"] == "fact"


def test_backward_compat_decrypt_malformed_falls_back() -> None:
    """Non-JSON / malformed blobs fall through to a raw-text result."""
    out = read_blob_unified("not json at all")
    assert out["text"] == "not json at all"
    assert out["importance"] == 5
    assert out["category"] == "fact"


def test_backward_compat_decrypt_v1_and_v0_precedence() -> None:
    """v1 takes precedence: a blob that has both short-key AND v1 fields is read as v1."""
    # Construct a hybrid blob (unlikely but possible if someone crafts one).
    hybrid = json.dumps({
        "id": "x",
        "text": "v1 text",
        "type": "claim",
        "source": "user",
        "importance": 8,
        "created_at": "2026-04-17T00:00:00Z",
        "schema_version": "1.0",
        # v0 keys also present
        "t": "v0 text",
        "c": "pref",
        "i": 5,
    })
    out = read_blob_unified(hybrid)
    assert out["text"] == "v1 text"
    assert out["importance"] == 8


# ---------------------------------------------------------------------------
# Provenance filter (tag-don't-drop, caps assistant-source at 7)
# ---------------------------------------------------------------------------


def test_provenance_filter_caps_assistant_importance_at_7() -> None:
    facts = [
        ExtractedFact(text="Fact from assistant", type="claim", importance=9,
                      action="ADD", source="assistant", scope="work"),
    ]
    conv = "[user]: hi\n\n[assistant]: Fact from assistant yes really"
    out = apply_provenance_filter_lax(facts, conv)
    assert len(out) == 1
    assert out[0].source == "assistant"
    assert out[0].importance == 7  # capped


def test_provenance_filter_user_turns_keep_full_importance() -> None:
    """If >30% of content words appear in a user turn, source:user stays."""
    conv = "[user]: I prefer dark mode because eyes"
    facts = [
        ExtractedFact(text="prefer dark mode", type="preference",
                      importance=9, action="ADD", source="user", scope="personal"),
    ]
    out = apply_provenance_filter_lax(facts, conv)
    assert len(out) == 1
    assert out[0].importance == 9


def test_provenance_filter_tags_untraced_facts_as_assistant() -> None:
    """Fact whose content words don't appear in user turns gets tagged assistant."""
    conv = "[user]: hello\n\n[assistant]: You should use PostgreSQL for OLTP"
    # Source is set to user-inferred (not yet downgraded). Fact content matches
    # assistant turn only → filter tags it assistant + caps importance at 7.
    facts = [
        ExtractedFact(text="User should use PostgreSQL for OLTP", type="claim",
                      importance=9, action="ADD", source="user-inferred", scope="work"),
    ]
    out = apply_provenance_filter_lax(facts, conv)
    assert len(out) == 1
    assert out[0].source == "assistant"
    assert out[0].importance == 7


def test_provenance_filter_drops_sub_5_importance() -> None:
    facts = [
        ExtractedFact(text="chatty noise", type="claim", importance=4, action="ADD",
                      source="user", scope="misc"),
    ]
    out = apply_provenance_filter_lax(facts, "[user]: chatty noise yes")
    # Below floor of 5 → dropped (unless DELETE).
    assert out == []


def test_provenance_filter_delete_bypasses_floor() -> None:
    facts = [
        ExtractedFact(text="old fact", type="claim", importance=1, action="DELETE",
                      existing_fact_id="x", source="user", scope="misc"),
    ]
    out = apply_provenance_filter_lax(facts, "[user]: old fact removed")
    assert len(out) == 1
    assert out[0].action == "DELETE"


# ---------------------------------------------------------------------------
# default_volatility heuristic
# ---------------------------------------------------------------------------


def test_default_volatility_commitment_is_updatable() -> None:
    f = ExtractedFact(text="x", type="commitment", importance=7, action="ADD")
    assert default_volatility(f) == "updatable"


def test_default_volatility_episode_is_stable() -> None:
    f = ExtractedFact(text="x", type="episode", importance=7, action="ADD")
    assert default_volatility(f) == "stable"


def test_default_volatility_directive_is_stable() -> None:
    f = ExtractedFact(text="x", type="directive", importance=7, action="ADD")
    assert default_volatility(f) == "stable"


def test_default_volatility_health_scope_is_stable() -> None:
    f = ExtractedFact(text="x", type="claim", importance=7, action="ADD", scope="health")
    assert default_volatility(f) == "stable"


def test_default_volatility_default_is_updatable() -> None:
    f = ExtractedFact(text="x", type="claim", importance=7, action="ADD", scope="work")
    assert default_volatility(f) == "updatable"


# ---------------------------------------------------------------------------
# parse_merged_response_v1 — dual-format input acceptance
# ---------------------------------------------------------------------------


def test_parse_merged_response_v1_canonical_shape() -> None:
    response = json.dumps({
        "topics": ["database choice", "team conventions"],
        "facts": [
            {"text": "Prefers PostgreSQL", "type": "preference",
             "source": "user", "scope": "work", "importance": 8, "action": "ADD"},
            {"text": "Never force-push main", "type": "directive",
             "source": "user", "scope": "work", "importance": 9, "action": "ADD"},
        ],
    })
    topics, facts = parse_merged_response_v1(response)
    assert topics == ["database choice", "team conventions"]
    assert len(facts) == 2
    assert facts[0].type == "preference"
    assert facts[1].type == "directive"
    assert facts[0].source == "user"
    assert facts[0].scope == "work"


def test_parse_merged_response_v1_bare_array() -> None:
    """Legacy bare-array format is wrapped into the merged-topic shape."""
    response = json.dumps([
        {"text": "A fact about something", "type": "claim",
         "source": "user", "importance": 8, "action": "ADD"},
    ])
    topics, facts = parse_merged_response_v1(response)
    assert topics == []
    assert len(facts) == 1


def test_parse_merged_response_v1_rejects_summary_with_user_source() -> None:
    """type:summary + source:user is an illegal combination."""
    response = json.dumps({
        "topics": [],
        "facts": [
            {"text": "A conclusion summary here", "type": "summary",
             "source": "user", "importance": 8, "action": "ADD"},
        ],
    })
    _, facts = parse_merged_response_v1(response)
    assert facts == []


def test_parse_merged_response_v1_floor_6() -> None:
    """Importance floor is 6 for the main extraction parser."""
    response = json.dumps({
        "topics": [],
        "facts": [
            {"text": "Borderline fact five here", "type": "claim",
             "source": "user", "importance": 5, "action": "ADD"},
            {"text": "Worth storing fact here", "type": "claim",
             "source": "user", "importance": 7, "action": "ADD"},
        ],
    })
    _, facts = parse_merged_response_v1(response)
    assert len(facts) == 1
    assert facts[0].importance == 7


def test_parse_facts_response_for_compaction_floor_5() -> None:
    """Compaction parser admits borderline facts (importance >= 5)."""
    response = json.dumps({
        "topics": [],
        "facts": [
            {"text": "Borderline fact here five", "type": "claim",
             "source": "user", "importance": 5, "action": "ADD"},
            {"text": "Regular fact seven here", "type": "claim",
             "source": "user", "importance": 7, "action": "ADD"},
        ],
    })
    facts = parse_facts_response_for_compaction(response)
    assert len(facts) == 2


# ---------------------------------------------------------------------------
# Retrieval v2 Tier 1 — source-weighted reranking
# ---------------------------------------------------------------------------


def test_source_weight_user_is_1() -> None:
    assert source_weight("user") == 1.0


def test_source_weight_assistant_is_0_55() -> None:
    assert source_weight("assistant") == pytest.approx(0.55, abs=0.001)


def test_source_weight_legacy_none_fallback() -> None:
    assert source_weight(None) == LEGACY_CLAIM_FALLBACK_WEIGHT
    assert LEGACY_CLAIM_FALLBACK_WEIGHT == 0.85


def test_source_weight_user_inferred_is_0_9() -> None:
    assert source_weight("user-inferred") == pytest.approx(0.9, abs=0.001)


def test_rerank_with_source_weights_promotes_user_over_assistant() -> None:
    """When two candidates are tied on text + importance, source=user wins."""
    query = "Who is my favorite database?"
    # Same text + same importance; only source differs.
    candidates = [
        RerankerCandidate(
            id="assistant-cand",
            text="The user prefers PostgreSQL",
            embedding=[1.0, 0.0, 0.0, 0.0],
            importance=0.8,
            created_at=0.0,
            category="pref",
            source="assistant",
        ),
        RerankerCandidate(
            id="user-cand",
            text="The user prefers PostgreSQL",
            embedding=[1.0, 0.0, 0.0, 0.0],
            importance=0.8,
            created_at=0.0,
            category="pref",
            source="user",
        ),
    ]
    query_emb = [1.0, 0.0, 0.0, 0.0]

    # Without source weights — order is not source-deterministic.
    # With source weights enabled, the user-sourced candidate ranks first.
    results = rerank(query, query_emb, candidates, top_k=2, apply_source_weights=True)
    assert len(results) == 2
    assert results[0].id == "user-cand"
    assert results[0].source == "user"
    assert results[0].source_weight == 1.0
    assert results[1].source == "assistant"
    assert results[1].source_weight == pytest.approx(0.55, abs=0.001)


def test_rerank_without_source_weights_omits_weight_field() -> None:
    candidates = [
        RerankerCandidate(id="a", text="test fact here", embedding=[1.0, 0.0],
                          importance=0.8, created_at=0.0, category="fact",
                          source="user"),
    ]
    results = rerank("query", [1.0, 0.0], candidates, top_k=1)
    assert results[0].source_weight is None


def test_rerank_legacy_candidate_gets_fallback_weight() -> None:
    """A candidate with source=None receives the legacy fallback weight."""
    candidates = [
        RerankerCandidate(id="legacy", text="legacy fact here",
                          embedding=[1.0, 0.0], importance=0.8, created_at=0.0,
                          category="fact", source=None),
    ]
    results = rerank("query", [1.0, 0.0], candidates, top_k=1, apply_source_weights=True)
    assert results[0].source_weight == LEGACY_CLAIM_FALLBACK_WEIGHT


# ---------------------------------------------------------------------------
# No env-var gates for v1
# ---------------------------------------------------------------------------


def test_v1_is_unconditional_no_env_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting TOTALRECLAW_TAXONOMY_VERSION=v0 must NOT flip to v0."""
    monkeypatch.setenv("TOTALRECLAW_TAXONOMY_VERSION", "v0")
    fact = {"text": "x", "type": "claim", "source": "user"}
    blob = build_canonical_claim(fact, importance=5)
    payload = json.loads(blob)
    assert payload["schema_version"] == V1_SCHEMA_VERSION


def test_claim_format_env_gate_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting TOTALRECLAW_CLAIM_FORMAT=legacy must NOT flip to legacy docs."""
    monkeypatch.setenv("TOTALRECLAW_CLAIM_FORMAT", "legacy")
    fact = {"text": "x", "type": "claim", "source": "user"}
    blob = build_canonical_claim(fact, importance=5)
    # Must still emit a v1 JSON payload.
    assert '"schema_version"' in blob


# ---------------------------------------------------------------------------
# Prompt content sanity — merged-topic shape, v1 type list
# ---------------------------------------------------------------------------


def test_extraction_prompt_mentions_v1_types() -> None:
    prompt = EXTRACTION_SYSTEM_PROMPT
    # Must list the 6 v1 types.
    for t in VALID_MEMORY_TYPES:
        assert t in prompt, f"Prompt missing v1 type {t!r}"
    # Must mention the merged output shape.
    assert '"topics"' in prompt
    assert '"facts"' in prompt
    # Must mention the required v1 source values.
    assert "user-inferred" in prompt
    assert "assistant" in prompt


def test_compaction_prompt_admits_floor_5() -> None:
    """Compaction prompt must mention the 5+ threshold (not 6+)."""
    assert "5+" in COMPACTION_SYSTEM_PROMPT or "5 " in COMPACTION_SYSTEM_PROMPT


def test_extraction_system_prompt_is_merged_topic() -> None:
    """The prompt must instruct a two-phase merged-topic output."""
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "PHASE 1" in prompt
    assert "PHASE 2" in prompt
