"""Pair-tool existing-credentials guard.

Completing a fresh pair on an already-configured install switches the agent
to a different account and orphans the current vault. The guard refuses by
default, and replacement is confirmed only by the USER, on the pairing page.

History: an earlier version returned a one-time ``replace_token`` with the
refusal and accepted ``replace_confirmed=true`` plus that token. QA showed
agents echoing the token back and confirming on the user's behalf, so the
agent no longer receives anything that authorises a replacement:
``replace=true`` only opens a replace-mode session, whose page asks the
user to confirm (see ``test_pair_replace_confirmation.py`` for the wire
side).
"""
from __future__ import annotations

import json

import pytest


def _state(configured: bool, monkeypatch):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    monkeypatch.setattr(state, "is_configured", lambda: configured)
    return state


def _patch_pair_session(monkeypatch):
    """Record every relay pair session the tool starts, with its replace flag."""
    from totalreclaw.hermes import pair_tool
    started = []

    async def fake_relay(state, mode, *, replace=False):
        started.append({"mode": mode, "replace": replace})
        return "https://pair.example/x", "123456", "2026-07-05T23:59:59Z"

    monkeypatch.setattr(pair_tool, "_pair_relay", fake_relay)
    monkeypatch.setattr(pair_tool, "_pair_mode", lambda: "relay")
    return started


def _assert_no_authorising_value(res: dict) -> None:
    blob = json.dumps(res)
    assert "replace_token" not in blob
    assert "token" not in res


@pytest.mark.asyncio
async def test_configured_install_blocks_fresh_pair(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({}, state))
    assert res.get("already_configured") is True
    assert "replace=true" in res["message"]
    _assert_no_authorising_value(res)
    assert started == [], "no pair session may start while credentials exist"


@pytest.mark.asyncio
async def test_legacy_self_confirmation_args_are_refused(monkeypatch):
    """The old confirmation arguments no longer do anything: an agent that
    sends them (with or without a token) gets the refusal, not a session."""
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    for args in (
        {"replace_confirmed": True},
        {"replace_confirmed": True, "replace_token": "anything"},
        {"mode": "generate", "replace_confirmed": True, "replace_token": "x"},
    ):
        res = json.loads(await pair_tool.pair(args, state))
        assert res.get("already_configured") is True, args
        _assert_no_authorising_value(res)
    assert started == []


@pytest.mark.asyncio
async def test_replace_opens_a_replace_mode_session(monkeypatch):
    """replace=true on a configured install opens a session in replace mode
    (the page asks the user to confirm); it hands the agent nothing that
    could confirm on the user's behalf."""
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"mode": "generate", "replace": True}, state))
    assert res.get("url")
    assert res.get("replace") is True
    assert "replaces the account currently on this agent" in res["instructions"]
    _assert_no_authorising_value(res)
    assert started == [{"mode": "generate", "replace": True}]


@pytest.mark.asyncio
async def test_replace_flag_is_ignored_on_an_unconfigured_install(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=False, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"replace": True}, state))
    assert res.get("url")
    assert res.get("replace") is False
    assert started == [{"mode": "either", "replace": False}]


@pytest.mark.asyncio
async def test_relay_without_replace_mode_fails_closed(monkeypatch):
    """An older relay cannot show the confirmation, so no session is offered
    and the current account is untouched."""
    from totalreclaw.hermes import pair_tool
    from totalreclaw.pair.remote_client import ReplaceNotSupportedError

    async def old_relay(state, mode, *, replace=False):
        raise ReplaceNotSupportedError("no replace mode")

    monkeypatch.setattr(pair_tool, "_pair_relay", old_relay)
    monkeypatch.setattr(pair_tool, "_pair_mode", lambda: "relay")
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"replace": True}, state))
    assert "url" not in res
    assert res.get("already_configured") is True
    assert "relay needs updating" in res["error"]


@pytest.mark.asyncio
async def test_local_mode_refuses_replacement(monkeypatch):
    """The local pair page has no confirmation step, so it never replaces."""
    from totalreclaw.hermes import pair_tool

    async def must_not_run(state, mode):
        raise AssertionError("local pair session must not start")

    monkeypatch.setattr(pair_tool, "_pair_local", must_not_run)
    monkeypatch.setattr(pair_tool, "_pair_mode", lambda: "local")
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"replace": True}, state))
    assert "url" not in res
    assert res.get("already_configured") is True


def test_schema_offers_no_self_confirmation():
    from totalreclaw.hermes.pair_tool import PAIR_SCHEMA

    props = PAIR_SCHEMA["parameters"]["properties"]
    assert "replace_token" not in props
    assert "replace_confirmed" not in props
    assert props["replace"]["type"] == "boolean"
    assert "cannot confirm it for them" in props["replace"]["description"]


@pytest.mark.asyncio
async def test_unconfigured_install_pairs_without_friction(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=False, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({}, state))
    assert res.get("already_configured") is not True
    assert res.get("url")
    assert started


def test_consent_tokens_are_kind_isolated(tmp_path, monkeypatch):
    """Consent tokens (still used by the import disclosure gate) cannot be
    redeemed across kinds or subjects."""
    import totalreclaw.import_state as ist
    from totalreclaw.hermes import consent_tokens

    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")
    tok = consent_tokens.mint("disclosure", "chatgpt")
    assert consent_tokens.redeem("other-kind", "chatgpt", tok) is False
    assert consent_tokens.redeem("disclosure", "gemini", tok) is False
    assert consent_tokens.redeem("disclosure", "chatgpt", tok) is True
