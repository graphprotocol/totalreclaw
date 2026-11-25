"""#646/#650 (Q1): the explicit ``totalreclaw_remember`` tool waits for its own
receipt (``REMEMBER_TOOL_CONFIRM = "receipt"``, option B) under the interactive
deadline; the memory mirror and other background writers keep the fast return.
One module constant switches the tool back to option A."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes import tools
from totalreclaw.hermes.state import PluginState


def _state():
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-1")
    state.get_client = MagicMock(return_value=client)
    return state, client


TEXT = "Pedro's favourite editor is helix"


def test_default_is_option_b():
    assert tools.REMEMBER_TOOL_CONFIRM == "receipt"


@pytest.mark.asyncio
async def test_explicit_tool_waits_for_its_receipt_interactively():
    state, client = _state()
    out = json.loads(await tools.remember({"text": TEXT, "force": True}, state))
    assert out["stored"] is True
    kw = client.remember.await_args.kwargs
    assert kw["confirm"] == "receipt"
    assert kw["profile"] == "interactive"


@pytest.mark.asyncio
async def test_one_line_switch_to_option_a(monkeypatch):
    monkeypatch.setattr(tools, "REMEMBER_TOOL_CONFIRM", "accept")
    state, client = _state()
    await tools.remember({"text": TEXT, "force": True}, state)
    assert client.remember.await_args.kwargs["confirm"] == "accept"


@pytest.mark.asyncio
async def test_mirror_path_is_a_background_write():
    state, client = _state()
    await tools.remember({"text": TEXT, "force": True}, state, _from_mirror=True)
    kw = client.remember.await_args.kwargs
    assert "confirm" not in kw and "profile" not in kw


@pytest.mark.asyncio
async def test_pin_forget_use_the_interactive_profile():
    state, client = _state()
    client.forget = AsyncMock(return_value={"success": True, "fact_id": "f"})
    client.pin_fact = AsyncMock(return_value={"success": True, "fact_id": "f2"})
    await tools.forget({"fact_id": "f"}, state)
    assert client.forget.await_args.kwargs["profile"] == "interactive"
    await tools.pin({"fact_id": "f"}, state)
    assert client.pin_fact.await_args.kwargs["profile"] == "interactive"
