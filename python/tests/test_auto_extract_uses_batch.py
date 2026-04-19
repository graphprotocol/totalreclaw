"""Tests for v2.2.1 batch wiring: auto_extract calls remember_batch.

Verifies:
1. With 5 extracted facts, remember_batch is called once (not 5 remember calls).
2. With 20 extracted facts, remember_batch is called twice (chunked at 15).
3. Partial-failure path: batch returns a shorter id list; surviving facts are
   logged to stored_texts and failed ones are reported via logger.warning.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from totalreclaw.agent.extraction import ExtractedFact


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_state_and_client():
    """Build a minimal PluginState with a stubbed client.

    remember_batch is configured with a side_effect that returns one UUID
    per fact in the batch — so any call with N facts returns N IDs.
    """
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    fake_client.recall = AsyncMock(return_value=[])
    fake_client.forget = AsyncMock(return_value=True)
    fake_client.remember = AsyncMock(return_value="should-not-be-called")

    # Default remember_batch: return one UUID per fact so stored_texts is populated.
    call_count = [0]

    async def _batch_side_effect(facts, source="python-client"):
        n = len(facts)
        ids = [f"fact-id-{call_count[0]}-{i}" for i in range(n)]
        call_count[0] += 1
        return ids

    fake_client.remember_batch = AsyncMock(side_effect=_batch_side_effect)
    state._client = fake_client
    return state, fake_client


def _make_facts(n: int, action: str = "ADD") -> list[ExtractedFact]:
    """Create n distinct ExtractedFact instances."""
    return [
        ExtractedFact(
            text=f"Fact number {i}: user detail here",
            type="claim",
            importance=7,
            action=action,
            confidence=0.9,
            source="user",
            scope="unspecified",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Test 1: 5 facts → 1 remember_batch call, 0 remember calls
# ---------------------------------------------------------------------------


def test_auto_extract_5_facts_calls_remember_batch_once() -> None:
    """Given 5 extracted facts, remember_batch is called exactly once."""
    from totalreclaw.agent.lifecycle import auto_extract

    state, fake_client = _make_state_and_client()
    facts = _make_facts(5)

    async def fake_extract(*args, **kwargs):
        return facts

    async def passthrough(fs, *args, **kwargs):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):

        # Add a message so get_unprocessed_messages() returns something.
        state.add_message("user", "I like fact number 0")
        state.add_message("assistant", "Got it")
        stored = auto_extract(state)

    # remember_batch called exactly once with all 5 facts
    assert isinstance(fake_client.remember_batch, AsyncMock)
    assert fake_client.remember_batch.call_count == 1, (
        f"expected 1 remember_batch call, got {fake_client.remember_batch.call_count}"
    )
    # remember must NOT have been called (auto_extract now uses batch path)
    assert fake_client.remember.call_count == 0, (
        "client.remember should not be called — auto_extract uses remember_batch"
    )
    # The batch should contain all 5 facts
    batch_args = fake_client.remember_batch.call_args
    fact_dicts = batch_args.args[0]
    assert len(fact_dicts) == 5, f"expected 5 fact dicts, got {len(fact_dicts)}"
    # source kwarg is shared
    assert batch_args.kwargs.get("source") == "hermes-auto"
    # All 5 texts stored
    assert len(stored) == 5


# ---------------------------------------------------------------------------
# Test 2: 20 facts → 2 remember_batch calls (chunked at 15)
# ---------------------------------------------------------------------------


def test_auto_extract_20_facts_calls_remember_batch_twice() -> None:
    """Given 20 extracted facts, remember_batch is called twice (chunks of 15+5)."""
    from totalreclaw.agent.lifecycle import auto_extract

    # Track batch sizes to verify chunking
    batch_sizes: list[int] = []

    async def _tracking_side_effect(facts, source="python-client"):
        batch_sizes.append(len(facts))
        return [f"id-{i}" for i in range(len(facts))]

    state, fake_client = _make_state_and_client()
    fake_client.remember_batch = AsyncMock(side_effect=_tracking_side_effect)
    facts = _make_facts(20)

    async def fake_extract(*args, **kwargs):
        return facts

    async def passthrough(fs, *args, **kwargs):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):

        state.add_message("user", "Tell me facts")
        state.add_message("assistant", "Here they are")
        # Override max_facts so the 20-fact cap doesn't truncate before chunking.
        with patch.object(state, "get_max_facts_per_extraction", return_value=20):
            stored = auto_extract(state)

    # Must be called twice (15 + 5)
    assert len(batch_sizes) == 2, (
        f"expected 2 remember_batch calls for 20 facts, got {len(batch_sizes)}"
    )
    assert batch_sizes[0] == 15, f"first chunk should be 15, got {batch_sizes[0]}"
    assert batch_sizes[1] == 5, f"second chunk should be 5, got {batch_sizes[1]}"
    # All 20 texts stored
    assert len(stored) == 20


# ---------------------------------------------------------------------------
# Test 3: partial-failure — batch returns shorter id list
# ---------------------------------------------------------------------------


def test_auto_extract_partial_failure_logs_failed_facts(caplog) -> None:
    """When remember_batch returns fewer IDs than facts, surviving facts are
    stored and failed ones are logged via logger.warning.

    Simulates the case where 3 facts are batched but only 2 IDs come back
    (the third fact's id is missing → treat as failure).
    """
    from totalreclaw.agent.lifecycle import auto_extract

    state, fake_client = _make_state_and_client()
    # Override remember_batch to return only 2 IDs for a batch of 3.
    fake_client.remember_batch = AsyncMock(return_value=["id-0", "id-1"])
    facts = _make_facts(3)

    async def fake_extract(*args, **kwargs):
        return facts

    async def passthrough(fs, *args, **kwargs):
        return fs

    with caplog.at_level(logging.WARNING, logger="totalreclaw.agent.lifecycle"):
        with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
             patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
             patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
             patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
             patch("totalreclaw.embedding.get_embedding", return_value=None):

            state.add_message("user", "Three facts please")
            state.add_message("assistant", "OK")
            stored = auto_extract(state)

    # Only 2 facts successfully stored (the ones with returned IDs)
    assert len(stored) == 2, f"expected 2 stored facts, got {len(stored)}"
    assert facts[0].text in stored
    assert facts[1].text in stored
    assert facts[2].text not in stored

    # A warning must have been logged for the missing-id case
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no id" in r.getMessage().lower() or "not stored" in r.getMessage().lower()
               for r in warnings), (
        f"expected a warning for the unidded fact, got: {[r.getMessage() for r in warnings]}"
    )
