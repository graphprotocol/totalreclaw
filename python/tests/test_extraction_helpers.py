"""Focused unit tests for the pure logic in ``totalreclaw.agent.extraction``.

``agent/extraction.py`` (~1300 LOC) is otherwise exercised only through the
async LLM-extraction integration paths. These tests pin the hottest pure
helpers that decide what actually lands in the vault:

  - ``normalize_to_v1_type`` — v0→v1 type coercion + unknown fallback.
  - ``normalize_confidence`` — clamping / NaN / bool handling.
  - ``_parse_entity`` — silent-drop parsing of one entity dict.
  - ``_build_fact`` — the single-fact validation + filtering gate
    (importance floor, illegal type/source combos, field clamping).
  - ``parse_merged_response_v1`` — the LLM-response parser (fences, think
    tags, dual-format acceptance, bracket-scan recovery).
  - ``default_volatility`` / ``compute_lexical_importance_bump`` /
    ``is_product_meta_request`` — the scoring + filtering heuristics.

All pure — no network, no LLM, no embeddings.
"""
from __future__ import annotations

import json

import pytest

from totalreclaw.agent.extraction import (
    ExtractedEntity,
    ExtractedFact,
    _build_fact,
    _parse_entity,
    compute_lexical_importance_bump,
    default_volatility,
    is_product_meta_request,
    is_valid_memory_type,
    normalize_confidence,
    normalize_to_v1_type,
    parse_facts_response,
    parse_merged_response_v1,
    DEFAULT_EXTRACTION_CONFIDENCE,
)


# ---------------------------------------------------------------------------
# normalize_to_v1_type / is_valid_memory_type
# ---------------------------------------------------------------------------


class TestNormalizeToV1Type:
    @pytest.mark.parametrize(
        "token",
        ["claim", "preference", "directive", "commitment", "episode", "summary"],
    )
    def test_v1_tokens_pass_through(self, token):
        assert normalize_to_v1_type(token) == token
        assert is_valid_memory_type(token) is True

    @pytest.mark.parametrize(
        "v0,expected",
        [
            ("fact", "claim"),
            ("decision", "claim"),
            ("episodic", "episode"),
            ("goal", "commitment"),
            ("rule", "directive"),
            ("context", "claim"),
        ],
    )
    def test_v0_tokens_coerced(self, v0, expected):
        # Only assert the mapping is a valid v1 type and stable; the exact
        # target is defined by V0_TO_V1_TYPE.
        result = normalize_to_v1_type(v0)
        assert is_valid_memory_type(result)
        assert result == expected

    def test_case_insensitive(self):
        assert normalize_to_v1_type("CLAIM") == "claim"
        assert normalize_to_v1_type("Preference") == "preference"

    @pytest.mark.parametrize("bad", [None, "", "nonsense", 123, "  "])
    def test_unknown_falls_back_to_claim(self, bad):
        assert normalize_to_v1_type(bad) == "claim"

    def test_is_valid_rejects_non_str_and_unknown(self):
        assert is_valid_memory_type("fact") is False  # v0 token is not v1
        assert is_valid_memory_type(None) is False
        assert is_valid_memory_type(5) is False


# ---------------------------------------------------------------------------
# normalize_confidence
# ---------------------------------------------------------------------------


class TestNormalizeConfidence:
    def test_in_range_passes(self):
        assert normalize_confidence(0.5) == 0.5
        assert normalize_confidence(0) == 0.0
        assert normalize_confidence(1) == 1.0

    def test_clamps_out_of_range(self):
        assert normalize_confidence(2.5) == 1.0
        assert normalize_confidence(-1.0) == 0.0

    def test_bool_is_rejected(self):
        # bool is an int subclass — must NOT be treated as 0/1 confidence.
        assert normalize_confidence(True) == DEFAULT_EXTRACTION_CONFIDENCE
        assert normalize_confidence(False) == DEFAULT_EXTRACTION_CONFIDENCE

    @pytest.mark.parametrize("bad", [None, "0.9", [], {}, "high"])
    def test_non_numeric_falls_back(self, bad):
        assert normalize_confidence(bad) == DEFAULT_EXTRACTION_CONFIDENCE

    def test_nan_and_inf_fall_back(self):
        assert normalize_confidence(float("nan")) == DEFAULT_EXTRACTION_CONFIDENCE
        assert normalize_confidence(float("inf")) == DEFAULT_EXTRACTION_CONFIDENCE
        assert normalize_confidence(float("-inf")) == DEFAULT_EXTRACTION_CONFIDENCE


# ---------------------------------------------------------------------------
# _parse_entity
# ---------------------------------------------------------------------------


class TestParseEntity:
    def test_valid_entity(self):
        ent = _parse_entity({"name": "Alice", "type": "person", "role": "chooser"})
        assert isinstance(ent, ExtractedEntity)
        assert ent.name == "Alice"
        assert ent.type == "person"
        assert ent.role == "chooser"

    def test_type_lowercased(self):
        ent = _parse_entity({"name": "Acme", "type": "COMPANY"})
        assert ent is not None
        assert ent.type == "company"
        assert ent.role is None

    @pytest.mark.parametrize(
        "raw",
        [
            "not-a-dict",
            {"type": "person"},  # missing name
            {"name": "", "type": "person"},  # empty name
            {"name": "   ", "type": "person"},  # whitespace name
            {"name": "X", "type": "alien"},  # invalid type
            {"name": 42, "type": "person"},  # non-str name
        ],
    )
    def test_invalid_returns_none(self, raw):
        assert _parse_entity(raw) is None

    def test_name_and_role_truncated_to_128(self):
        long = "z" * 300
        ent = _parse_entity({"name": long, "type": "tool", "role": long})
        assert ent is not None
        assert len(ent.name) == 128
        assert len(ent.role) == 128

    def test_blank_role_dropped(self):
        ent = _parse_entity({"name": "N", "type": "concept", "role": "   "})
        assert ent is not None
        assert ent.role is None


# ---------------------------------------------------------------------------
# _build_fact
# ---------------------------------------------------------------------------


class TestBuildFact:
    def test_minimal_valid_fact(self):
        f = _build_fact({"text": "User lives in Lisbon", "importance": 8})
        assert f is not None
        assert f.text == "User lives in Lisbon"
        assert f.type == "claim"
        assert f.importance == 8
        assert f.action == "ADD"
        assert f.source == "user-inferred"
        assert f.scope == "unspecified"

    def test_short_text_rejected(self):
        assert _build_fact({"text": "hi", "importance": 9}) is None
        assert _build_fact({"text": "", "importance": 9}) is None

    def test_importance_floor_filters(self):
        # Below the default floor (6) → dropped.
        assert _build_fact({"text": "trivial detail", "importance": 3}) is None
        # At/above floor → kept.
        assert _build_fact({"text": "trivial detail", "importance": 6}) is not None

    def test_delete_bypasses_floor(self):
        f = _build_fact(
            {"text": "stale fact to remove", "importance": 1, "action": "DELETE"}
        )
        assert f is not None
        assert f.action == "DELETE"

    def test_custom_floor(self):
        # Compaction path uses floor 5.
        assert _build_fact({"text": "borderline fact", "importance": 5}, importance_floor=5) is not None
        assert _build_fact({"text": "borderline fact", "importance": 5}, importance_floor=6) is None

    def test_importance_clamped(self):
        f_hi = _build_fact({"text": "very important thing", "importance": 99})
        assert f_hi is not None and f_hi.importance == 10
        # low value below floor is filtered, so use DELETE to observe the clamp
        f_lo = _build_fact(
            {"text": "some fact here", "importance": -5, "action": "DELETE"}
        )
        assert f_lo is not None and f_lo.importance == 1

    def test_bad_importance_defaults_to_5(self):
        # Non-numeric importance defaults to 5; with floor 5 it survives.
        f = _build_fact({"text": "a decent fact", "importance": "lots"}, importance_floor=5)
        assert f is not None and f.importance == 5

    def test_illegal_summary_user_combo_rejected(self):
        assert _build_fact(
            {"text": "a summary of stuff", "type": "summary", "source": "user", "importance": 9}
        ) is None
        # summary + non-user source is allowed
        assert _build_fact(
            {"text": "a summary of stuff", "type": "summary", "source": "assistant", "importance": 9}
        ) is not None

    def test_invalid_source_and_scope_default(self):
        f = _build_fact(
            {"text": "a fact with junk axes", "importance": 8, "source": "alien", "scope": "mars"}
        )
        assert f is not None
        assert f.source == "user-inferred"
        assert f.scope == "unspecified"

    def test_invalid_action_defaults_to_add(self):
        f = _build_fact({"text": "a normal fact", "importance": 8, "action": "SMASH"})
        assert f is not None and f.action == "ADD"

    def test_v0_type_coerced(self):
        f = _build_fact({"text": "user decided to use Rust", "type": "decision", "importance": 8})
        assert f is not None and f.type == "claim"

    def test_text_truncated_to_512(self):
        f = _build_fact({"text": "x" * 1000, "importance": 8})
        assert f is not None and len(f.text) == 512

    def test_reasoning_truncated_to_256(self):
        f = _build_fact(
            {"text": "chose X because Y", "importance": 8, "reasoning": "r" * 400}
        )
        assert f is not None and len(f.reasoning) == 256

    def test_existing_fact_id_both_spellings(self):
        f1 = _build_fact({"text": "an updated fact", "importance": 8, "existingFactId": "abc"})
        f2 = _build_fact({"text": "an updated fact", "importance": 8, "existing_fact_id": "def"})
        assert f1 is not None and f1.existing_fact_id == "abc"
        assert f2 is not None and f2.existing_fact_id == "def"

    def test_entities_parsed_and_bad_ones_dropped(self):
        f = _build_fact(
            {
                "text": "Alice works at Acme",
                "importance": 8,
                "entities": [
                    {"name": "Alice", "type": "person"},
                    {"name": "", "type": "person"},  # dropped
                    "garbage",  # dropped
                ],
            }
        )
        assert f is not None
        assert f.entities is not None
        assert len(f.entities) == 1
        assert f.entities[0].name == "Alice"

    def test_no_valid_entities_yields_none_list(self):
        f = _build_fact(
            {"text": "a fact with only bad entities", "importance": 8, "entities": ["x", {}]}
        )
        assert f is not None and f.entities is None


# ---------------------------------------------------------------------------
# parse_merged_response_v1 / parse_facts_response
# ---------------------------------------------------------------------------


class TestParseMergedResponseV1:
    def test_canonical_merged_shape(self):
        response = json.dumps(
            {
                "topics": ["work", "prefs"],
                "facts": [
                    {"text": "User lives in Lisbon", "type": "claim", "importance": 8},
                    {"text": "Prefers dark mode", "type": "preference", "importance": 7},
                ],
            }
        )
        topics, facts = parse_merged_response_v1(response)
        assert topics == ["work", "prefs"]
        assert len(facts) == 2

    def test_bare_array_wrapped(self):
        response = json.dumps(
            [{"text": "User lives in Lisbon", "type": "fact", "importance": 8}]
        )
        topics, facts = parse_merged_response_v1(response)
        assert topics == []
        assert len(facts) == 1
        assert facts[0].type == "claim"  # v0 coerced

    def test_single_fact_object_wrapped(self):
        response = json.dumps({"text": "User lives in Lisbon", "importance": 8})
        topics, facts = parse_merged_response_v1(response)
        assert len(facts) == 1

    def test_strips_markdown_fence(self):
        inner = json.dumps({"topics": [], "facts": [{"text": "a solid fact here", "importance": 8}]})
        response = "```json\n" + inner + "\n```"
        _, facts = parse_merged_response_v1(response)
        assert len(facts) == 1

    def test_strips_think_tags(self):
        inner = json.dumps({"facts": [{"text": "a solid fact here", "importance": 8}]})
        response = "<think>reasoning here</think>" + inner
        _, facts = parse_merged_response_v1(response)
        assert len(facts) == 1

    def test_bracket_scan_recovery_from_prose(self):
        inner = json.dumps([{"text": "recovered fact text", "importance": 8}])
        response = "Sure! Here are the facts: " + inner + " Hope that helps."
        _, facts = parse_merged_response_v1(response)
        assert len(facts) == 1
        assert facts[0].text == "recovered fact text"

    def test_topics_capped_at_three(self):
        response = json.dumps(
            {"topics": ["a", "b", "c", "d", "e"], "facts": []}
        )
        topics, _ = parse_merged_response_v1(response)
        assert topics == ["a", "b", "c"]

    def test_non_string_topics_dropped(self):
        response = json.dumps({"topics": ["ok", 5, None, ""], "facts": []})
        topics, _ = parse_merged_response_v1(response)
        assert topics == ["ok"]

    def test_unparseable_returns_empty(self):
        topics, facts = parse_merged_response_v1("total garbage no json")
        assert topics == [] and facts == []

    def test_facts_not_a_list_returns_empty_facts(self):
        response = json.dumps({"topics": ["t"], "facts": "not-a-list"})
        topics, facts = parse_merged_response_v1(response)
        assert topics == ["t"] and facts == []

    def test_non_dict_facts_skipped(self):
        response = json.dumps(
            {"facts": ["skip me", {"text": "a valid fact here", "importance": 8}]}
        )
        _, facts = parse_merged_response_v1(response)
        assert len(facts) == 1

    def test_parse_facts_response_discards_topics(self):
        response = json.dumps(
            {"topics": ["x"], "facts": [{"text": "a valid fact here", "importance": 8}]}
        )
        facts = parse_facts_response(response)
        assert isinstance(facts, list) and len(facts) == 1


# ---------------------------------------------------------------------------
# default_volatility
# ---------------------------------------------------------------------------


def _fact(**kw) -> ExtractedFact:
    base = dict(text="some fact text", type="claim", importance=8, action="ADD")
    base.update(kw)
    return ExtractedFact(**base)


class TestDefaultVolatility:
    def test_commitment_updatable(self):
        assert default_volatility(_fact(type="commitment")) == "updatable"

    def test_episode_and_directive_stable(self):
        assert default_volatility(_fact(type="episode")) == "stable"
        assert default_volatility(_fact(type="directive")) == "stable"

    def test_health_family_scope_stable(self):
        assert default_volatility(_fact(type="claim", scope="health")) == "stable"
        assert default_volatility(_fact(type="claim", scope="family")) == "stable"

    def test_default_updatable(self):
        assert default_volatility(_fact(type="claim", scope="work")) == "updatable"


# ---------------------------------------------------------------------------
# compute_lexical_importance_bump
# ---------------------------------------------------------------------------


class TestLexicalImportanceBump:
    def test_no_signal_zero(self):
        assert compute_lexical_importance_bump("likes coffee", "user talks about coffee") == 0

    def test_strong_intent_bumps(self):
        bump = compute_lexical_importance_bump(
            "the API key is secret", "remember this: the API key is secret"
        )
        assert bump >= 1

    def test_emphasis_double_excl_bumps(self):
        bump = compute_lexical_importance_bump(
            "deadline is friday", "the deadline is friday!!"
        )
        assert bump >= 1

    def test_repetition_bumps(self):
        # 'kubernetes' (>=5 chars, non-stopword) appears twice in conversation.
        bump = compute_lexical_importance_bump(
            "migrating to kubernetes",
            "we should use kubernetes. yes, kubernetes is the plan.",
        )
        assert bump >= 1

    def test_bump_capped_at_two(self):
        conv = "REMEMBER THIS NEVER FORGET!! kubernetes kubernetes kubernetes"
        bump = compute_lexical_importance_bump("migrating to kubernetes now", conv)
        assert bump == 2

    def test_stopwords_do_not_trigger_repetition(self):
        # 'their'/'these' are stopwords — repetition of them shouldn't bump.
        bump = compute_lexical_importance_bump(
            "these their these their", "these their these their these"
        )
        assert bump == 0


# ---------------------------------------------------------------------------
# is_product_meta_request
# ---------------------------------------------------------------------------


class TestIsProductMetaRequest:
    @pytest.mark.parametrize(
        "text",
        [
            "I want encrypted memory across my AI tools",
            "help me set up totalreclaw",
            "install the memory plugin",
            "configure the vault plugin",
            "set up my memory vault",
            "how do I use the hermes plugin",
        ],
    )
    def test_meta_requests_flagged(self, text):
        assert is_product_meta_request(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "I like encrypted tools",
            "I prefer Signal because it's encrypted",
            "User lives in Lisbon",
            "prefers dark mode in the editor",
        ],
    )
    def test_genuine_preferences_pass(self, text):
        assert is_product_meta_request(text) is False

    @pytest.mark.parametrize("bad", ["", None, 123])
    def test_non_string_or_empty_false(self, bad):
        assert is_product_meta_request(bad) is False

    def test_case_insensitive(self):
        assert is_product_meta_request("SET UP TOTALRECLAW") is True
