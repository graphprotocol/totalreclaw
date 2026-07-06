"""F8 (#428) — pair-tool existing-credentials guard.

rc12 QA: "Set up my TotalReclaw account" on an ALREADY-configured install
started a fresh pair flow. Completing it would have minted a new phrase /
Smart Account and silently orphaned the existing vault. The guard refuses
to start a pair session while credentials exist, unless the agent passes
replace_confirmed=true after the user explicitly chose to replace.

Message-level guard only — no key material, no crypto-path changes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


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
    assert started == [], "no pair session may start while credentials exist"


@pytest.mark.asyncio
async def test_replace_confirmed_proceeds(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=True, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({"replace_confirmed": True}, state))
    assert res.get("already_configured") is not True
    assert res.get("url")
    assert started, "explicit replacement must start the pair session"


@pytest.mark.asyncio
async def test_unconfigured_install_pairs_without_friction(monkeypatch):
    from totalreclaw.hermes import pair_tool
    started = _patch_pair_session(monkeypatch)
    state = _state(configured=False, monkeypatch=monkeypatch)

    res = json.loads(await pair_tool.pair({}, state))
    assert res.get("already_configured") is not True
    assert res.get("url")
    assert started
