"""F8 (#428) + #466 Finding-2 — pair-tool existing-credentials guard + token.

rc12 QA: "Set up my TotalReclaw account" on an ALREADY-configured install
started a fresh pair flow that would have orphaned the existing vault. The
guard refuses unless the user explicitly chose to replace.

rc7 QA (#466 Finding-2): the agent SELF-ASSERTED replace_confirmed=true on a
"generate a new phrase" request without ever surfacing the replace warning.
The guard now applies the disclosure-token pattern — replacement requires BOTH
replace_confirmed=true AND a one-time replace_token minted by the
already_configured response.

Message-level guard only — no key material, no crypto-path changes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist


@pytest.fixture(autouse=True)
def _tmp_state_dir(tmp_path, monkeypatch):
    # consent_tokens writes replace-token sidecars under IMPORT_STATE_DIR.
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")
    yield


def _state(configured: bool, monkeypatch):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    monkeypatch.setattr(state, "is_configured", lambda: configured)
    return state


def _patch_pair_session(monkeypatch):
    """Record whether a pair session was actually started."""
    from totalreclaw.hermes import pair_tool
    started = []

    async def fake_relay(state, mode):
        started.append(mode)
        return "https://pair.example/x", "123456", "2026-07-05T23:59:59Z"

    monkeypatch.setattr(pair_tool, "_pair_relay", fake_relay)
    monkeypatch.setattr(pair_tool, "_pair_mode", lambda: "relay")
    return started


@pytest.mark.asyncio
async def test_configured_install_blocks_fresh_pair(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({}, state))
    assert res.get("already_configured") is True
    assert "replace_confirmed" in res["message"]
    # The response mints a one-time replace_token to gate replacement.
    assert res.get("replace_token")
    assert started == [], "no pair session may start while credentials exist"


@pytest.mark.asyncio
async def test_self_asserted_replace_without_token_rejected(monkeypatch):
    """#466 Finding-2: replace_confirmed=true WITHOUT the token (the agent
    self-asserting) must be REJECTED — no pair session starts."""
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"replace_confirmed": True}, state))
    assert res.get("already_configured") is True
    assert res.get("replace_token")  # a fresh token is offered
    assert started == [], "self-asserted replacement must not start a session"


@pytest.mark.asyncio
async def test_replace_with_token_proceeds(monkeypatch):
    """The token flow: the already_configured response mints a token; a
    follow-up with replace_confirmed=true AND that token proceeds."""
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    first = json.loads(await pair_tool.pair({}, state))
    token = first["replace_token"]
    res = json.loads(await pair_tool.pair(
        {"replace_confirmed": True, "replace_token": token}, state))
    assert res.get("already_configured") is not True
    assert res.get("url")
    assert started, "confirmed replacement with a valid token must start the session"


@pytest.mark.asyncio
async def test_replace_token_is_one_time(monkeypatch):
    """A redeemed replace_token cannot be reused for a second replacement."""
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    first = json.loads(await pair_tool.pair({}, state))
    token = first["replace_token"]
    # Consume it once.
    await pair_tool.pair({"replace_confirmed": True, "replace_token": token}, state)
    started.clear()
    # Reuse rejected.
    res = json.loads(await pair_tool.pair(
        {"replace_confirmed": True, "replace_token": token}, state))
    assert res.get("already_configured") is True
    assert started == []


def test_consent_tokens_are_kind_isolated(monkeypatch):
    """A token minted for one kind cannot be redeemed as another (a disclosure
    token must not satisfy the pair-replace guard, and vice-versa)."""
    from totalreclaw.hermes import consent_tokens
    tok = consent_tokens.mint("pair-replace", "pair")
    assert consent_tokens.redeem("disclosure", "pair", tok) is False
    assert consent_tokens.redeem("pair-replace", "other", tok) is False
    assert consent_tokens.redeem("pair-replace", "pair", tok) is True  # correct kind+subject


@pytest.mark.asyncio
async def test_unconfigured_install_pairs_without_friction(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=False, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({}, state))
    assert res.get("already_configured") is not True
    assert res.get("url")
    assert started
