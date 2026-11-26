"""internal#778: changing a user-pinned fact needs the user's confirmation.

forget / unpin / retype / set_scope on a pinned fact, and a remember that
restates a pinned fact, return ``confirmation_required`` with a token and
change nothing. The change happens only on a re-call with the token after a
completed user turn.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes import tools
from totalreclaw.operations import ForgetResult
from totalreclaw.pii import note_turn_completed
from totalreclaw.write_gate import GateDecision, GateMatch

FID = "fact-pinned-1"

# name, tool fn, client method, args, ok-result from the client, done key
OPS = {
    "forget": (
        tools.forget, "forget", {"fact_id": FID},
        ForgetResult(success=True, fact_id=FID, pinned=True), "deleted",
    ),
    "unpin": (
        tools.unpin, "unpin_fact", {"fact_id": FID},
        {"success": True, "fact_id": FID, "new_fact_id": "n1", "previous_status": "pinned", "new_status": "active"},
        "unpinned",
    ),
    "retype": (
        tools.retype, "retype", {"fact_id": FID, "new_type": "preference"},
        {"success": True, "fact_id": FID, "new_fact_id": "n1", "previous_type": "claim", "new_type": "preference"},
        "retyped",
    ),
    "set_scope": (
        tools.set_scope, "set_scope", {"fact_id": FID, "new_scope": "health"},
        {"success": True, "fact_id": FID, "new_fact_id": "n1", "previous_scope": "unspecified", "new_scope": "health"},
        "scope_set",
    ),
}


def _setup(op, pinned=True):
    fn, method, args, result, done_key = OPS[op]
    client = MagicMock()
    client.get_fact_pin_status = AsyncMock(return_value=pinned)
    setattr(client, method, AsyncMock(return_value=result))
    state = MagicMock()
    state.get_client.return_value = client
    return fn, client, getattr(client, method), state, dict(args), done_key


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_pinned_change_asks_and_changes_nothing(op):
    fn, client, mutator, state, args, done_key = _setup(op)
    out = json.loads(await fn(args, state))
    assert out[done_key] is False
    assert out["confirmation_required"] == f"pinned_{op}"
    assert out["fact_id"] == FID
    assert out["confirm_token"] and out["message"] and out["next_step"]
    mutator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_same_turn_wrong_unknown_tokens_refused(op):
    fn, client, mutator, state, args, done_key = _setup(op)
    first = json.loads(await fn(args, state))
    # same turn: the user had no chance to answer
    out = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state))
    assert out["confirmation_required"] == f"pinned_{op}"
    note_turn_completed()
    for bad in ("deadbeefdeadbeef", "x"):
        out = json.loads(await fn({**args, "confirm_token": bad}, state))
        assert out["confirmation_required"] == f"pinned_{op}"
    mutator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_correct_token_after_a_turn_works_once(op):
    fn, client, mutator, state, args, done_key = _setup(op)
    first = json.loads(await fn(args, state))
    note_turn_completed()
    out = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state))
    assert "confirmation_required" not in out
    mutator.assert_awaited_once()
    # reused token: refused, nothing more is called
    again = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state))
    assert again["confirmation_required"] == f"pinned_{op}"
    mutator.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_expired_token_refused(op, monkeypatch):
    from totalreclaw import pii, pin_confirm

    fn, client, mutator, state, args, done_key = _setup(op)
    first = json.loads(await fn(args, state))
    note_turn_completed()
    real = pin_confirm.confirmations._clock
    monkeypatch.setattr(pin_confirm.confirmations, "_clock", lambda: real() + pii.CONFIRM_TOKEN_TTL_SECONDS + 1)
    out = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state))
    assert out["confirmation_required"] == f"pinned_{op}"
    mutator.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_is_bound_to_the_action_and_the_new_value():
    # a forget token cannot unpin
    fn, client, mutator, state, args, _ = _setup("forget")
    first = json.loads(await fn(args, state))
    note_turn_completed()
    ufn, uclient, umutator, ustate, uargs, _ = _setup("unpin")
    out = json.loads(await ufn({**uargs, "confirm_token": first["confirm_token"]}, ustate))
    assert out["confirmation_required"] == "pinned_unpin"
    umutator.assert_not_awaited()
    # a retype token for "preference" cannot retype to "directive"
    rfn, rclient, rmutator, rstate, rargs, _ = _setup("retype")
    tok = json.loads(await rfn(rargs, rstate))["confirm_token"]
    note_turn_completed()
    out = json.loads(await rfn({**rargs, "new_type": "directive", "confirm_token": tok}, rstate))
    assert out["confirmation_required"] == "pinned_retype"
    rmutator.assert_not_awaited()
    # a token for another fact id does not work
    out = json.loads(await rfn({**rargs, "fact_id": "other", "confirm_token": tok}, rstate))
    assert out["confirmation_required"] == "pinned_retype"
    rmutator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_unpinned_fact_is_unchanged_no_prompt(op):
    fn, client, mutator, state, args, done_key = _setup(op, pinned=False)
    out = json.loads(await fn(args, state))
    assert "confirmation_required" not in out
    mutator.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_pin_lookup_failure_fails_closed(op):
    fn, client, mutator, state, args, done_key = _setup(op)
    client.get_fact_pin_status = AsyncMock(side_effect=RuntimeError("subgraph unreadable"))
    out = json.loads(await fn(args, state))
    assert "error" in out and "confirmation_required" not in out
    mutator.assert_not_awaited()


@pytest.mark.asyncio
async def test_mirror_forget_does_not_prompt():
    fn, client, mutator, state, args, _ = _setup("forget")
    out = json.loads(await fn(args, state, _from_mirror=True))
    assert out["deleted"] is True
    client.get_fact_pin_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_binding_when_a_session_id_is_given():
    fn, client, mutator, state, args, _ = _setup("forget")
    first = json.loads(await fn(args, state, session_id="chat-a"))
    note_turn_completed("chat-a")
    other = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state, session_id="chat-b"))
    assert other["confirmation_required"] == "pinned_forget"
    mutator.assert_not_awaited()
    ok = json.loads(await fn({**args, "confirm_token": first["confirm_token"]}, state, session_id="chat-a"))
    assert "confirmation_required" not in ok
    mutator.assert_awaited_once()


# --- remember that restates a pinned fact -----------------------------------

TEXT = "Tidewater restarted the ClickHouse migration last week"


def _remember_env(pinned_match: bool):
    client = MagicMock()
    client.remember = AsyncMock(return_value="new-1")
    state = MagicMock()
    state.get_client.return_value = client
    state.manual_remember_is_dup_of_pending.return_value = False
    match = GateMatch("pin-1", "Migration shelved", "near_duplicate", 0.9, pinned_match)

    async def fake_check(client_, text, emb, mode="explicit", exclude_ids=(), **kw):
        if mode == "exact_only":
            return GateDecision(action="store", match=None)
        return GateDecision(action="confirm", match=match)

    return client, state, fake_check


async def _remember(state, fake_check, args):
    with patch("totalreclaw.write_gate.check_store", new=fake_check), \
         patch("totalreclaw.hermes.tools._embed_for_remember", return_value=[0.1] * 8), \
         patch("totalreclaw.write_gate.record_write"):
        return json.loads(await tools.remember(args, state))


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_remember_restating_a_pinned_fact_asks_first(force):
    client, state, fake_check = _remember_env(True)
    args = {"text": TEXT, **({"force": True} if force else {})}
    first = await _remember(state, fake_check, args)
    assert first["stored"] is False and first["confirmation_required"] == "pinned_remember"
    assert first["fact_id"] == "pin-1" and first["confirm_token"]
    client.remember.assert_not_awaited()
    # same turn: refused
    again = await _remember(state, fake_check, {**args, "confirm_token": first["confirm_token"]})
    assert again["confirmation_required"] == "pinned_remember"
    note_turn_completed()
    # wrong text: refused
    wrong = await _remember(state, fake_check, {**args, "text": TEXT + " again", "confirm_token": first["confirm_token"]})
    assert wrong["confirmation_required"] == "pinned_remember"
    client.remember.assert_not_awaited()
    ok = await _remember(state, fake_check, {**args, "confirm_token": again["confirm_token"]})
    assert ok["stored"] is True
    client.remember.assert_awaited_once()
    # the token is spent
    reuse = await _remember(state, fake_check, {**args, "confirm_token": again["confirm_token"]})
    assert reuse["confirmation_required"] == "pinned_remember"
    client.remember.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_near_duplicate_of_unpinned_fact_is_unchanged():
    client, state, fake_check = _remember_env(False)
    out = await _remember(state, fake_check, {"text": TEXT})
    assert out["possible_duplicate"] is True and "confirmation_required" not in out
    forced = await _remember(state, fake_check, {"text": TEXT, "force": True})
    assert forced["stored"] is True and "confirmation_required" not in forced
    client.remember.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_exact_duplicate_of_pinned_fact_just_skips():
    client = MagicMock()
    state = MagicMock()
    state.get_client.return_value = client
    state.manual_remember_is_dup_of_pending.return_value = False
    match = GateMatch("pin-1", TEXT, "exact", 1.0, True)

    async def fake_check(*a, **kw):
        return GateDecision(action="skip", match=match)

    out = await _remember(state, fake_check, {"text": TEXT})
    assert out["already_stored"] is True and "confirmation_required" not in out


@pytest.mark.asyncio
async def test_pinned_asks_do_not_evict_a_pending_sensitive_token():
    from totalreclaw import pii

    sensitive = pii.confirmations.issue("api_key", "sk-secret-value")
    fn, client, mutator, state, args, _ = _setup("forget")
    for i in range(pii.CONFIRM_TOKEN_MAX_LIVE + 5):
        await fn({"fact_id": f"pinned-{i}"}, state)
    note_turn_completed()
    assert pii.confirmations.redeem(sensitive, "api_key", "sk-secret-value") is None


@pytest.mark.asyncio
async def test_remember_token_is_bound_to_the_matched_pinned_fact():
    client, state, fake_check = _remember_env(True)
    first = await _remember(state, fake_check, {"text": TEXT})
    note_turn_completed()
    # the same text now matches a different pinned fact: the token does not carry over
    match_b = GateMatch("pin-2", "Other", "near_duplicate", 0.9, True)

    async def check_b(client_, text, emb, mode="explicit", exclude_ids=(), **kw):
        return GateDecision(action="confirm", match=match_b)

    out = await _remember(state, check_b, {"text": TEXT, "confirm_token": first["confirm_token"]})
    assert out["confirmation_required"] == "pinned_remember" and out["fact_id"] == "pin-2"
    client.remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_skip_confirm_does_not_prompt():
    client, state, fake_check = _remember_env(True)
    with patch("totalreclaw.write_gate.check_store", new=fake_check), \
         patch("totalreclaw.hermes.tools._embed_for_remember", return_value=[0.1] * 8), \
         patch("totalreclaw.write_gate.record_write"):
        out = json.loads(await tools.remember({"text": TEXT, "force": True}, state, _skip_confirm=True))
    assert out["stored"] is True
