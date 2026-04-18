"""Cross-language parity test for Memory Taxonomy v1 (python 2.0.0).

Asserts the Python-side canonical v1 type list matches the TypeScript plugin
and MCP lists byte-for-byte. Drift here produces hard-to-diagnose cross-client
divergence — a v1 claim-typed fact stored by the plugin would deserialize as
unknown if Python's list is missing "claim".

The TypeScript-side parity check lives at
``tests/parity/memory-types-parity.test.ts``. These two tests together form a
three-way guard: plugin + MCP are compared by the TS test, Python + canonical
list is compared here. The canonical list is hard-coded in BOTH tests so a
drift in either language fails loudly.

Run with: ``pytest python/tests/test_memory_types_parity.py -v``
"""
from __future__ import annotations

import pytest


# v1 canonical types — the 6 memory types the plugin's `VALID_MEMORY_TYPES`
# exports. Order and content must match `skill/plugin/extractor.ts`.
CANONICAL_V1_TYPES = (
    "claim",
    "preference",
    "directive",
    "commitment",
    "episode",
    "summary",
)

# Legacy v0 types — retained on the read-side adapter only. Order and content
# must match `LEGACY_V0_MEMORY_TYPES` in the plugin.
CANONICAL_V0_TYPES = (
    "fact",
    "preference",
    "decision",
    "episodic",
    "goal",
    "context",
    "summary",
    "rule",
)


def test_extraction_valid_memory_types_is_v1():
    """``VALID_MEMORY_TYPES`` must be the 6-item v1 list."""
    from totalreclaw.agent.extraction import VALID_MEMORY_TYPES
    assert VALID_MEMORY_TYPES == CANONICAL_V1_TYPES, (
        f"VALID_MEMORY_TYPES drifted from v1 canonical.\n"
        f"  expected: {CANONICAL_V1_TYPES}\n"
        f"  actual:   {VALID_MEMORY_TYPES}"
    )


def test_extraction_legacy_v0_memory_types_preserved():
    """Legacy v0 tokens are still enumerated for read-side decoding."""
    from totalreclaw.agent.extraction import LEGACY_V0_MEMORY_TYPES
    assert LEGACY_V0_MEMORY_TYPES == CANONICAL_V0_TYPES


def test_extraction_valid_types_legacy_alias_matches():
    """``VALID_TYPES`` (legacy frozenset alias) matches v1 ``VALID_MEMORY_TYPES``."""
    from totalreclaw.agent.extraction import VALID_TYPES, VALID_MEMORY_TYPES
    assert VALID_TYPES == frozenset(VALID_MEMORY_TYPES)


def test_v0_to_v1_mapping_complete():
    """Every v0 token has a v1 destination."""
    from totalreclaw.agent.extraction import V0_TO_V1_TYPE
    for v0 in CANONICAL_V0_TYPES:
        assert v0 in V0_TO_V1_TYPE, f"Missing V0_TO_V1_TYPE for {v0!r}"
        assert V0_TO_V1_TYPE[v0] in CANONICAL_V1_TYPES


def test_claims_helper_v1_category_covers_all_v1_types():
    """Every v1 type has a display-category short key."""
    from totalreclaw.claims_helper import TYPE_TO_CATEGORY_V1
    for t in CANONICAL_V1_TYPES:
        assert t in TYPE_TO_CATEGORY_V1, f"Missing TYPE_TO_CATEGORY_V1 entry for {t!r}"


def test_claims_helper_v1_category_short_values():
    """v1 category short-form matches the plugin's mapping exactly."""
    from totalreclaw.claims_helper import TYPE_TO_CATEGORY_V1
    expected_v1 = {
        "claim": "claim",
        "preference": "pref",
        "directive": "rule",
        "commitment": "goal",
        "episode": "epi",
        "summary": "sum",
    }
    for long_form, short_form in expected_v1.items():
        assert TYPE_TO_CATEGORY_V1[long_form] == short_form


def test_claims_helper_v0_category_short_values_preserved():
    """v0 short-key category mapping preserved for read path."""
    from totalreclaw.claims_helper import TYPE_TO_CATEGORY_V0
    expected_v0 = {
        "fact": "fact",
        "preference": "pref",
        "decision": "dec",
        "episodic": "epi",
        "goal": "goal",
        "context": "ctx",
        "summary": "sum",
        "rule": "rule",
    }
    for long_form, short_form in expected_v0.items():
        assert TYPE_TO_CATEGORY_V0[long_form] == short_form


def test_rust_core_v1_types_enumerable():
    """``totalreclaw_core`` recognizes every v1 type via ``parse_memory_type_v1``."""
    import totalreclaw_core as core
    for t in CANONICAL_V1_TYPES:
        assert core.parse_memory_type_v1(t) == t, (
            f"core.parse_memory_type_v1({t!r}) returned {core.parse_memory_type_v1(t)!r}"
        )


def test_rust_core_v0_legacy_tokens_coerced():
    """core coerces legacy v0 tokens to v1 via ``parse_memory_type_v1``."""
    import totalreclaw_core as core
    # Unknown / legacy tokens should fall back to "claim" (same semantics as
    # V0_TO_V1_TYPE[fact]/[decision]/[context]). The core doesn't expose
    # V0→V1 mapping directly — unknown tokens map to claim.
    for t in ("fact", "decision", "context"):
        assert core.parse_memory_type_v1(t) == "claim"


def test_rust_core_v0_short_key_category_round_trip():
    """v0 short-key category round-trips unchanged through the core serializer.

    This ensures the on-chain storage layer can still decode pre-v1 blobs.
    """
    import json
    import totalreclaw_core as core

    v0_short_keys = ["fact", "pref", "dec", "epi", "goal", "ctx", "sum", "rule"]
    for short in v0_short_keys:
        claim_json = json.dumps({
            "t": f"test {short}",
            "c": short,
            "cf": 0.9,
            "i": 7,
            "sa": "parity-test",
            "ea": "2026-04-17T00:00:00Z",
        })
        out = core.canonicalize_claim(claim_json)
        parsed = json.loads(out)
        assert parsed["c"] == short, (
            f"v0 round-trip failed for {short!r}: got {parsed['c']!r}"
        )
