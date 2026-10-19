"""PRD-04 F1 / DEP-5 -- every skip path of the LLM UPDATE/DELETE pin guard."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.agent.loop_runner import InterpreterShutdownError
from totalreclaw.agent.pin_guard import (
    REASON_EXISTING_PINNED,
    REASON_PIN_STATE_UNKNOWN,
    apply_pin_guard,
    fail_closed_actions,
)

SECRET_TEXT = "Pedro's home city is Porto"
PINNED_ID = "11111111-1111-4111-8111-111111111111"
PLAIN_ID = "22222222-2222-4222-8222-222222222222"
BROKEN_ID = "33333333-3333-4333-8333-333333333333"
LOGGER = "totalreclaw.agent.pin_guard"


def _fact(action: str, target: str | None = None) -> ExtractedFact:
    return ExtractedFact(
        text=SECRET_TEXT, type="claim", importance=8, action=action,
        existing_fact_id=target, source="user",
    )


def _client() -> AsyncMock:
    def lookup(fact_id: str) -> bool:
        if fact_id == BROKEN_ID:
            raise RuntimeError("relay 503")
        return fact_id == PINNED_ID

    client = AsyncMock()
    client.get_fact_pin_status = AsyncMock(side_effect=lookup)
    return client


async def test_update_on_pinned_target_is_dropped(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = await apply_pin_guard([_fact("UPDATE", PINNED_ID)], _client())
    assert out == []
    assert f"reason={REASON_EXISTING_PINNED}" in caplog.text


async def test_delete_on_pinned_target_is_dropped() -> None:
    assert await apply_pin_guard([_fact("DELETE", PINNED_ID)], _client()) == []


async def test_update_on_unpinned_target_passes_unchanged() -> None:
    fact = _fact("UPDATE", PLAIN_ID)
    out = await apply_pin_guard([fact], _client())
    assert out == [fact] and out[0] is fact


async def test_delete_on_unpinned_target_passes_unchanged() -> None:
    fact = _fact("DELETE", PLAIN_ID)
    assert await apply_pin_guard([fact], _client()) == [fact]


async def test_update_with_unknown_pin_state_becomes_add(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = await apply_pin_guard([_fact("UPDATE", BROKEN_ID)], _client())
    assert len(out) == 1
    assert out[0].action == "ADD"
    assert out[0].existing_fact_id is None
    assert out[0].text == SECRET_TEXT
    assert f"reason={REASON_PIN_STATE_UNKNOWN}" in caplog.text


async def test_delete_with_unknown_pin_state_is_dropped() -> None:
    assert await apply_pin_guard([_fact("DELETE", BROKEN_ID)], _client()) == []


async def test_add_and_noop_never_trigger_a_lookup() -> None:
    client = _client()
    facts = [_fact("ADD"), _fact("NOOP"), _fact("UPDATE"), _fact("DELETE")]
    assert await apply_pin_guard(facts, client) == facts
    client.get_fact_pin_status.assert_not_awaited()


async def test_one_lookup_per_distinct_target() -> None:
    client = _client()
    await apply_pin_guard([_fact("UPDATE", PLAIN_ID), _fact("DELETE", PLAIN_ID), _fact("UPDATE", PINNED_ID)], client)
    assert [c.args[0] for c in client.get_fact_pin_status.await_args_list] == [PLAIN_ID, PINNED_ID]


async def test_interpreter_shutdown_propagates() -> None:
    client = AsyncMock()
    client.get_fact_pin_status = AsyncMock(side_effect=InterpreterShutdownError("shutting down"))
    with pytest.raises(InterpreterShutdownError):
        await apply_pin_guard([_fact("DELETE", PLAIN_ID)], client)


async def test_logs_carry_category_only(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await apply_pin_guard(
            [_fact("UPDATE", PINNED_ID), _fact("DELETE", BROKEN_ID), _fact("UPDATE", BROKEN_ID)],
            _client(),
        )
    assert caplog.records, "expected skip log lines"
    assert SECRET_TEXT not in caplog.text
    for fact_id in (PINNED_ID, BROKEN_ID):
        assert fact_id not in caplog.text


def test_fail_closed_actions_rule() -> None:
    add, update, delete = _fact("ADD"), _fact("UPDATE", PLAIN_ID), _fact("DELETE", PLAIN_ID)
    out = fail_closed_actions([add, update, delete])
    assert [f.action for f in out] == ["ADD", "ADD"]
    assert out[0] is add
    assert out[1].existing_fact_id is None
