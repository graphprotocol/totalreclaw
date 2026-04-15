"""Phase 2.2.6: cross-language parity test for ``VALID_MEMORY_TYPES``.

Asserts the Python-side canonical type list matches the TypeScript plugin and
MCP lists byte-for-byte. Drift here produces hard-to-diagnose cross-client
divergence — a rule-typed fact stored by the plugin would deserialize as
unknown if Python's list is missing "rule".

The TypeScript-side parity check lives at
``tests/parity/memory-types-parity.test.ts``. These two tests together form a
three-way guard: plugin + MCP are compared by the TS test, Python + canonical
list is compared here. The canonical list is hard-coded in BOTH tests so a
drift in either language fails loudly.

Run with: ``pytest python/tests/test_memory_types_parity.py -v``
"""
from __future__ import annotations

import pytest


CANONICAL_TYPES = (
    "fact",
    "preference",
    "decision",
    "episodic",
    "goal",
    "context",
    "summary",
    "rule",
)


def test_extraction_valid_memory_types_matches_canonical():
    """``totalreclaw.agent.extraction.VALID_MEMORY_TYPES`` is the canonical 8-type list."""
    from totalreclaw.agent.extraction import VALID_MEMORY_TYPES
    assert VALID_MEMORY_TYPES == CANONICAL_TYPES, (
        f"VALID_MEMORY_TYPES drifted from canonical list.\n"
        f"  expected: {CANONICAL_TYPES}\n"
        f"  actual:   {VALID_MEMORY_TYPES}"
    )


def test_extraction_valid_types_legacy_alias_matches():
    """``VALID_TYPES`` (legacy frozenset alias) matches ``VALID_MEMORY_TYPES``."""
    from totalreclaw.agent.extraction import VALID_TYPES, VALID_MEMORY_TYPES
    assert VALID_TYPES == frozenset(VALID_MEMORY_TYPES)


def test_claims_helper_type_to_category_covers_all_types():
    """Every canonical type has a short-form mapping in ``claims_helper.TYPE_TO_CATEGORY``."""
    from totalreclaw.claims_helper import TYPE_TO_CATEGORY
    for t in CANONICAL_TYPES:
        assert t in TYPE_TO_CATEGORY, f"Missing TYPE_TO_CATEGORY entry for '{t}'"


def test_claims_helper_short_form_values():
    """The short-form mapping matches the Rust ``ClaimCategory`` serde_rename values."""
    from totalreclaw.claims_helper import TYPE_TO_CATEGORY
    expected = {
        "fact": "fact",
        "preference": "pref",
        "decision": "dec",
        "episodic": "epi",
        "goal": "goal",
        "context": "ctx",
        "summary": "sum",
        "rule": "rule",
    }
    for long_form, short_form in expected.items():
        assert TYPE_TO_CATEGORY[long_form] == short_form, (
            f"TYPE_TO_CATEGORY[{long_form!r}] = {TYPE_TO_CATEGORY[long_form]!r}, "
            f"expected {short_form!r}"
        )


def test_rust_core_category_round_trip():
    """``totalreclaw_core.canonicalize_claim`` accepts every type and preserves short form."""
    import json
    import totalreclaw_core as core

    for long_form in CANONICAL_TYPES:
        short_form = {
            "fact": "fact", "preference": "pref", "decision": "dec", "episodic": "epi",
            "goal": "goal", "context": "ctx", "summary": "sum", "rule": "rule",
        }[long_form]

        # Build a minimal canonical Claim JSON and round-trip it through the Rust core.
        # Short-form is what lives on-chain; this proves the Rust enum accepts it.
        claim_json = json.dumps({
            "t": f"test {long_form}",
            "c": short_form,
            "cf": 0.9,
            "i": 7,
            "sa": "parity-test",
            "ea": "2026-04-15T00:00:00Z",
        })
        out = core.canonicalize_claim(claim_json)
        parsed = json.loads(out)
        assert parsed["c"] == short_form, (
            f"Round-trip failed for {long_form!r}: expected c={short_form!r}, got c={parsed['c']!r}"
        )
