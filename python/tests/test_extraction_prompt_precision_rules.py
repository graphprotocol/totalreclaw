"""The canonical extraction prompt carries the precision rules measured on the
small-model testbed (internal #638, #620, #662, #344).

These guard against a later edit silently dropping a rule. The behaviour
itself is measured in the internal repo's testbed
(``testbed/small-model-bench``, results doc section "Extraction precision"),
not here: a unit test cannot call the model.
"""
from __future__ import annotations

from totalreclaw.agent.extraction import EXTRACTION_SYSTEM_PROMPT as PROMPT


def test_rule_full_sentence_not_fragment() -> None:  # #620, #662
    assert "full\n   sentence with its subject" in PROMPT
    assert "Never output a bare clause, a question" in PROMPT


def test_rule_single_request_is_not_a_trait() -> None:
    assert "A single request is not a trait." in PROMPT
    assert "anything\n   a request reveals about the user" in PROMPT


def test_rule_assistant_words_are_not_the_users() -> None:  # #638
    assert "The assistant's words are not the user's." in PROMPT
    assert "store the USER's decision with\n   source:user" in PROMPT


def test_commitment_excludes_past_and_ongoing_activity() -> None:  # #344
    assert "Past or ongoing activity is NOT a commitment" in PROMPT
    assert "is a claim" in PROMPT and "is an episode" in PROMPT
