"""Regression tests for internal issues #620 and #662.

Auto-QA found bare clauses stored as user facts: the question fragment
"these days?" (2.5.3rc3, #620) and "to restore it, not create a new one"
(2.5.3rc8, #662). ``is_sentence_fragment`` / ``_filter_sentence_fragment_facts``
drop them before storage. The rule is narrow on purpose, so real facts that
happen to lack a subject, start with a brand name or use a non-Latin script
pass through.
"""
from __future__ import annotations

import pytest

from totalreclaw.agent.extraction import (
    ExtractedFact,
    _filter_sentence_fragment_facts,
    is_sentence_fragment,
)


@pytest.mark.parametrize(
    "text",
    [
        "these days?",  # #620
        "to restore it, not create a new one",  # #662
        "it surfaced every time",  # #471
        "“to restore it, not create a new one”",
        "Where do I live and where do I work these days?",
        "   ",
    ],
)
def test_flags_fragments(text: str) -> None:
    assert is_sentence_fragment(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "User lives in Porto and works at The Graph Foundation.",
        "Enjoys speedcubing",
        "iPhone 15 owner",
        "maria's dog is Rex",
        "zsh is the user's shell",
        "macOS is the user's main OS",
        "用户正在学习中文",  # Chinese, no case
        "User decided against PgBouncer transaction pooling.",
    ],
)
def test_keeps_facts(text: str) -> None:
    assert is_sentence_fragment(text) is False


def _fact(text: str, action: str = "ADD") -> ExtractedFact:
    return ExtractedFact(text=text, type="claim", importance=7, action=action, source="user")


def test_filter_drops_fragments_and_keeps_facts() -> None:
    facts = [
        _fact("User has a TotalReclaw account on another machine."),
        _fact("to restore it, not create a new one"),
        _fact("these days?"),
    ]
    kept = _filter_sentence_fragment_facts(facts)
    assert [f.text for f in kept] == ["User has a TotalReclaw account on another machine."]


def test_filter_keeps_delete_actions() -> None:
    kept = _filter_sentence_fragment_facts([_fact("old fragment", action="DELETE")])
    assert len(kept) == 1
