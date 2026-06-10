"""totalreclaw_status must expose the account ID (Smart Account address).

SKILL.md line 76 already promises status "Returns tier, used / limit, and
smart-account address" — but the tool only returned tier/quota, so when a user
asked "what's my account ID/address?" the agent had nothing to report. This
locks the address into the status response (it's a public on-chain address, NOT
the recovery phrase, so safe to surface).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_status_includes_account_id_and_addresses():
    from totalreclaw.hermes import tools
    from totalreclaw.hermes.state import PluginState

    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(
        tier="pro", free_writes_used=3, free_writes_limit=1500,
        expires_at="2027-06-10",
    ))
    client._wallet_address = "0xa1db0bdbacf82b65dbd464f25b07432fd2f9c47e"
    client._eoa_address = "0x05651cf715043fcae41620f158f835fa73eff917"
    state._client = client

    res = json.loads(await tools.status({}, state))
    assert res["tier"] == "pro"
    assert res["account_id"] == client._wallet_address
    assert res["wallet_address"] == client._wallet_address
    assert res["eoa_address"] == client._eoa_address


@pytest.mark.asyncio
async def test_status_resolves_address_lazily_if_unset():
    from totalreclaw.hermes import tools
    from totalreclaw.hermes.state import PluginState

    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(
        tier="free", free_writes_used=0, free_writes_limit=250, expires_at=None,
    ))
    client._wallet_address = None
    client._eoa_address = "0x05651cf715043fcae41620f158f835fa73eff917"

    async def _ensure():
        client._wallet_address = "0xa1db0bdbacf82b65dbd464f25b07432fd2f9c47e"
    client._ensure_address = AsyncMock(side_effect=_ensure)
    state._client = client

    res = json.loads(await tools.status({}, state))
    client._ensure_address.assert_awaited()  # resolved on demand
    assert res["account_id"] == "0xa1db0bdbacf82b65dbd464f25b07432fd2f9c47e"
