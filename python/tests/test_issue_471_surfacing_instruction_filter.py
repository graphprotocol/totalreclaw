"""Regression tests for issue #471 (QA Finding #6).

Extraction occasionally captured a user *instruction phrase* about the vault's
own surfacing behaviour as a durable fact. The QA repro: the pin request
"I want it surfaced every time" was mis-extracted and stored as an orphaned
``[preference]`` fragment ("it surfaced every time").

Fix: a high-precision post-filter (``is_memory_surfacing_instruction`` /
``_filter_instruction_fragment_facts``) drops vault-surfacing / pin
meta-instructions before storage, mirroring the Bug #9 product-meta filter.

These assertions FAIL on the pre-patch tree (no filter → fragment survives)
and PASS on the post-patch tree.
"""
from __future__ import annotations

from totalreclaw.agent.extraction import (
    ExtractedFact,
    is_memory_surfacing_instruction,
    _filter_instruction_fragment_facts,
)


class TestIssue471SurfacingInstructionDetection:
    """The detector flags surfacing/pin meta-instructions."""

    def test_flags_the_qa_reported_fragment(self):
        # The exact orphaned fragment that reached the vault.
        assert is_memory_surfacing_instruction("it surfaced every time") is True

    def test_flags_the_full_pin_request(self):
        assert is_memory_surfacing_instruction(
            "I want it surfaced every time"
        ) is True

    def test_flags_surfacing_variants(self):
        assert is_memory_surfacing_instruction("surface this every time") is True
        assert is_memory_surfacing_instruction("always surface that") is True
        assert is_memory_surfacing_instruction(
            "surface it each time I ask"
        ) is True

    def test_flags_pin_directives(self):
        assert is_memory_surfacing_instruction("pin this") is True
        assert is_memory_surfacing_instruction("pin it to the top") is True
        assert is_memory_surfacing_instruction(
            "keep it at the top of my memories"
        ) is True

    def test_empty_and_non_string_are_safe(self):
        assert is_memory_surfacing_instruction("") is False
        assert is_memory_surfacing_instruction(None) is False  # type: ignore[arg-type]


class TestIssue471GenuineFactsPassThrough:
    """The filter must NOT catch legitimate facts, preferences, or the v1
    ``directive`` type (rules/commands the user genuinely wants stored)."""

    def test_allows_genuine_directive(self):
        # v1 `directive` type — a real stored rule, not a surfacing instruction.
        assert is_memory_surfacing_instruction(
            "Always call me by my first name"
        ) is False

    def test_allows_self_descriptions_with_frequency_words(self):
        assert is_memory_surfacing_instruction(
            "I show up on time every day"
        ) is False
        assert is_memory_surfacing_instruction(
            "I check my email every morning"
        ) is False
        assert is_memory_surfacing_instruction(
            "I always arrive early to meetings"
        ) is False

    def test_allows_ordinary_preferences(self):
        assert is_memory_surfacing_instruction("I prefer dark mode") is False
        assert is_memory_surfacing_instruction(
            "My favorite editor is Vim"
        ) is False
        assert is_memory_surfacing_instruction(
            "I like my coffee black"
        ) is False


class TestIssue471FilterList:
    """``_filter_instruction_fragment_facts`` drops the fragment fact and keeps
    the genuine ones in a mixed batch."""

    def test_drops_only_the_surfacing_fragment(self):
        surfacing = ExtractedFact(
            text="it surfaced every time",
            type="preference", importance=6, action="ADD",
        )
        genuine_pref = ExtractedFact(
            text="User prefers dark mode",
            type="preference", importance=7, action="ADD",
        )
        genuine_directive = ExtractedFact(
            text="Always call the user by their first name",
            type="directive", importance=8, action="ADD",
        )

        kept = _filter_instruction_fragment_facts(
            [surfacing, genuine_pref, genuine_directive]
        )

        texts = [f.text for f in kept]
        assert "it surfaced every time" not in texts
        assert "User prefers dark mode" in texts
        assert "Always call the user by their first name" in texts
        assert len(kept) == 2

    def test_empty_batch_is_safe(self):
        assert _filter_instruction_fragment_facts([]) == []
