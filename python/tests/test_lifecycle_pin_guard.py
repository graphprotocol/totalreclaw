"""PRD-04 F1 / DEP-5 -- _auto_extract_inner applies the pin guard before any
tombstone is issued. Helpers mirror tests/test_auto_extract_uses_batch.py."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from totalreclaw.agent.extraction import ExtractedFact

PINNED_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "33333333-3333-4333-8333-333333333333"


def _make_state_and_client(pin_status):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    client = MagicMock()
    client.recall = AsyncMock(return_value=[])
    client.forget = AsyncMock(return_value=True)
    client.get_fact_pin_status = AsyncMock(side_effect=pin_status)

    async def _batch(facts, source="python-client"):
        return [f"new-{i}" for i in range(len(facts))]

    client.remember_batch = AsyncMock(side_effect=_batch)
    state._client = client
    return state, client


def _run(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*args, **kwargs):
        return facts

    async def passthrough(fs, *args, **kwargs):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        state.add_message("user", "My home city changed")
        state.add_message("assistant", "Noted")
        return auto_extract(state)


def test_update_and_delete_targeting_a_pinned_fact_are_not_applied() -> None:
    state, client = _make_state_and_client(lambda fact_id: fact_id == PINNED_ID)
    facts = [
        ExtractedFact(text="Home city is Porto", type="claim", importance=8,
                      action="UPDATE", existing_fact_id=PINNED_ID, source="user"),
        ExtractedFact(text="Home city is Lisbon", type="claim", importance=8,
                      action="DELETE", existing_fact_id=PINNED_ID, source="user"),
        ExtractedFact(text="Likes espresso", type="preference", importance=7,
                      action="ADD", source="user"),
    ]
    stored = _run(state, facts)
    client.forget.assert_not_awaited()
    assert stored == ["Likes espresso"]
    assert [d["text"] for d in client.remember_batch.call_args.args[0]] == ["Likes espresso"]


def test_update_on_unpinned_fact_still_tombstones_after_store() -> None:
    state, client = _make_state_and_client(lambda fact_id: False)
    facts = [
        ExtractedFact(text="Home city is Porto", type="claim", importance=8,
                      action="UPDATE", existing_fact_id=OTHER_ID, source="user"),
    ]
    stored = _run(state, facts)
    assert stored == ["Home city is Porto"]
    client.forget.assert_awaited_once_with(OTHER_ID)


def test_guard_crash_fails_closed() -> None:
    state, client = _make_state_and_client(lambda fact_id: False)

    async def boom(*args, **kwargs):
        raise RuntimeError("guard exploded")

    facts = [
        ExtractedFact(text="Home city is Porto", type="claim", importance=8,
                      action="UPDATE", existing_fact_id=OTHER_ID, source="user"),
        ExtractedFact(text="Home city is Lisbon", type="claim", importance=8,
                      action="DELETE", existing_fact_id=OTHER_ID, source="user"),
    ]
    with patch("totalreclaw.agent.lifecycle.apply_pin_guard", new=boom):
        stored = _run(state, facts)
    client.forget.assert_not_awaited()
    assert stored == ["Home city is Porto"]
