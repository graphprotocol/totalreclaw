"""imp-15: auto_extract emits one structured telemetry log line per submission.

Per spec docs/specs/imp/281-gnosis-batching-chain-gate.md §6 T-6, the
lifecycle batch path must emit one log line carrying
``{ submission_path, fact_count, userop_count, chain_id }`` so Axiom can
verify ``userop_count / fact_count`` across a sample.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from totalreclaw.agent.extraction import ExtractedFact


def _make_state_and_client(chain_id: int = 100):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    fake_client.recall = AsyncMock(return_value=[])
    fake_client.forget = AsyncMock(return_value=True)
    fake_client.remember = AsyncMock(return_value="should-not-be-called")

    async def _batch_side_effect(facts, source="python-client"):
        return [f"id-{i}" for i in range(len(facts))]

    fake_client.remember_batch = AsyncMock(side_effect=_batch_side_effect)
    # MagicMock would auto-vivify `chain_id` to a MagicMock; pin it to a real int
    # so the telemetry line interpolates a stable, parseable value.
    fake_client.chain_id = chain_id
    state._client = fake_client
    return state, fake_client


def _make_facts(n: int) -> list[ExtractedFact]:
    return [
        ExtractedFact(
            text=f"Fact {i}: user detail",
            type="claim",
            importance=7,
            action="ADD",
            confidence=0.9,
            source="user",
            scope="unspecified",
        )
        for i in range(n)
    ]


def _run_auto_extract(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*_a, **_kw):
        return facts

    async def passthrough(fs, *_a, **_kw):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):

        state.add_message("user", "tell me facts")
        state.add_message("assistant", "ok")
        with patch.object(state, "get_max_facts_per_extraction", return_value=max(len(facts), 1)):
            return auto_extract(state)


def _telemetry_lines(records) -> list[str]:
    return [
        r.getMessage()
        for r in records
        if "submission_telemetry" in r.getMessage()
    ]


def test_one_telemetry_line_per_chunk_for_single_batch(caplog) -> None:
    state, _client = _make_state_and_client(chain_id=100)
    facts = _make_facts(5)

    with caplog.at_level(logging.INFO, logger="totalreclaw.agent.lifecycle"):
        stored = _run_auto_extract(state, facts)

    assert len(stored) == 5
    lines = _telemetry_lines(caplog.records)
    assert len(lines) == 1, f"expected 1 telemetry line, got {lines}"
    line = lines[0]
    assert "submission_path=batch" in line
    assert "fact_count=5" in line
    assert "userop_count=1" in line
    assert "chain_id=100" in line


def test_one_telemetry_line_per_chunk_for_multi_chunk(caplog) -> None:
    """20 facts → 2 chunks (15 + 5) → 2 telemetry lines."""
    state, _client = _make_state_and_client(chain_id=100)
    facts = _make_facts(20)

    with caplog.at_level(logging.INFO, logger="totalreclaw.agent.lifecycle"):
        _run_auto_extract(state, facts)

    lines = _telemetry_lines(caplog.records)
    assert len(lines) == 2, f"expected 2 telemetry lines for 20-fact extraction, got {lines}"
    assert any("fact_count=15" in line for line in lines)
    assert any("fact_count=5" in line for line in lines)
    for line in lines:
        assert "submission_path=batch" in line
        assert "userop_count=1" in line
        assert "chain_id=100" in line


def test_telemetry_records_free_tier_chain_id(caplog) -> None:
    state, _client = _make_state_and_client(chain_id=84532)
    facts = _make_facts(3)

    with caplog.at_level(logging.INFO, logger="totalreclaw.agent.lifecycle"):
        _run_auto_extract(state, facts)

    lines = _telemetry_lines(caplog.records)
    assert len(lines) == 1
    assert "chain_id=84532" in lines[0]


def test_no_telemetry_line_on_batch_failure(caplog) -> None:
    """If remember_batch raises, no submission_telemetry line is emitted
    (the batch never landed). The existing failure warning still fires."""
    state, fake_client = _make_state_and_client(chain_id=100)
    fake_client.remember_batch = AsyncMock(side_effect=RuntimeError("simulated relay 503"))
    facts = _make_facts(3)

    with caplog.at_level(logging.INFO, logger="totalreclaw.agent.lifecycle"):
        _run_auto_extract(state, facts)

    lines = _telemetry_lines(caplog.records)
    assert lines == [], f"expected no telemetry line on batch failure, got {lines}"
    # the pre-existing failure warning should still appear
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("remember_batch failed" in w for w in warnings)
