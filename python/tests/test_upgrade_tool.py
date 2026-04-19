"""Tests for the ``totalreclaw_upgrade`` Hermes tool (Phase A).

The tool wraps :meth:`RelayClient.create_checkout` and returns a Stripe
checkout URL plus a user-visible message. No on-chain writes, no LLM
calls — it is a pure relay round-trip.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes import schemas
from totalreclaw.hermes.state import PluginState
from totalreclaw.relay import CheckoutResponse


def _make_state():
    """Return an unconfigured PluginState with isolated env."""
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            return PluginState()


class TestUpgradeSchema:
    def test_schema_exists(self) -> None:
        assert hasattr(schemas, "UPGRADE")
        assert schemas.UPGRADE["name"] == "totalreclaw_upgrade"

    def test_schema_description_is_user_utterance_mapped(self) -> None:
        """Description should follow the Phase 2 style: explicit utterance hints
        telling the LLM when to invoke.
        """
        desc = schemas.UPGRADE["description"]
        # Must mention the payment/checkout outcome.
        assert "Pro" in desc
        assert "checkout" in desc.lower()
        # Must include the "when user says X" pattern (Phase 2 style).
        lowered = desc.lower()
        assert "upgrade" in lowered
        # Either upgrade-mention or Pro-mention is the utterance hook.
        assert any(
            phrase in lowered
            for phrase in ("upgrade to pro", "unlimited", "limit", "pay")
        )

    def test_schema_parameters_empty(self) -> None:
        """Python client already knows its wallet — no args required from the LLM."""
        params = schemas.UPGRADE["parameters"]
        assert params["type"] == "object"
        # No required args — the checkout pulls wallet_address from the client.
        assert params.get("required", []) == []


class TestUpgradeTool:
    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self) -> None:
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        result = json.loads(await upgrade({}, state))
        assert "error" in result
        assert "totalreclaw_setup" in result["error"]

    @pytest.mark.asyncio
    async def test_happy_path_returns_url_and_message(self) -> None:
        """Mock relay → assert tool returns the checkout URL + a user-visible message."""
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        mock_client = MagicMock()
        # The handler reaches into the client to call the relay.
        mock_client._relay = MagicMock()
        mock_client._relay.create_checkout = AsyncMock(
            return_value=CheckoutResponse(
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_abc123",
                session_id="cs_test_abc123",
            )
        )
        state._client = mock_client

        result = json.loads(await upgrade({}, state))
        assert result.get("checkout_url") == "https://checkout.stripe.com/c/pay/cs_test_abc123"
        assert "session_id" in result
        # User-visible message includes the URL (so the LLM reads it back).
        assert "https://checkout.stripe.com/c/pay/cs_test_abc123" in result.get("message", "")
        assert mock_client._relay.create_checkout.await_count == 1

    @pytest.mark.asyncio
    async def test_relay_error_surfaces_as_error_field(self) -> None:
        """Relay failure → tool returns ``{"error": "..."}`` (no exception)."""
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        mock_client = MagicMock()
        mock_client._relay = MagicMock()
        mock_client._relay.create_checkout = AsyncMock(
            side_effect=RuntimeError("relay down")
        )
        state._client = mock_client

        result = json.loads(await upgrade({}, state))
        assert "error" in result
        assert "relay down" in result["error"]


class TestUpgradeRegistration:
    def test_register_wires_upgrade_tool(self) -> None:
        """``register()`` must include ``totalreclaw_upgrade`` in the tool set."""
        from totalreclaw.hermes import register

        ctx = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                register(ctx)

        tool_names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        assert "totalreclaw_upgrade" in tool_names
