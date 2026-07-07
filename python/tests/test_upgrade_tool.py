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
        # 2.3.1rc4 — error messages now point to totalreclaw_pair (the
        # phrase-safe replacement), not totalreclaw_setup (removed).
        assert "totalreclaw_pair" in result["error"]

    @pytest.mark.asyncio
    async def test_happy_path_returns_url_and_message(self) -> None:
        """Mock relay → assert tool returns the checkout URL + a user-visible message."""
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        mock_client = MagicMock()
        # The handler reaches the relay through the public ``relay`` accessor
        # and resolves the address via the public ``ensure_address``.
        mock_client.ensure_address = AsyncMock()
        mock_client.relay = MagicMock()
        mock_client.relay.create_checkout = AsyncMock(
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
        assert mock_client.relay.create_checkout.await_count == 1

    @pytest.mark.asyncio
    async def test_relay_error_surfaces_as_error_field(self) -> None:
        """Relay failure → tool returns ``{"error": "..."}`` (no exception)."""
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        mock_client = MagicMock()
        mock_client.ensure_address = AsyncMock()
        mock_client.relay = MagicMock()
        mock_client.relay.create_checkout = AsyncMock(
            side_effect=RuntimeError("relay down")
        )
        state._client = mock_client

        result = json.loads(await upgrade({}, state))
        assert "error" in result
        assert "relay down" in result["error"]


class _FakeHttpResp:
    """Minimal stand-in for an httpx.Response (status 200, JSON body)."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:  # relay always returns HTTP 200
        return None

    def json(self) -> dict:
        return self._data


def _relay_returning(data: dict):
    """Build a RelayClient whose POST returns ``data`` (no real network)."""
    from totalreclaw.relay import RelayClient

    client = RelayClient(relay_url="https://api-staging.totalreclaw.xyz")
    client._wallet_address = "0xa1db0bdbacf82b65dbd464f25b07432fd2f9c47e"
    http = MagicMock()
    http.post = AsyncMock(return_value=_FakeHttpResp(data))
    client._get_http = AsyncMock(return_value=http)
    return client


class TestCreateCheckoutContract:
    """Regression: relay POST /v1/billing/checkout returns ``{success, checkout_url}``
    with NO ``session_id``. The client used to hard-read ``data["session_id"]`` →
    ``KeyError: 'session_id'`` → "Failed to create checkout session: 'session_id'"
    on every upgrade. (errors.log 2026-06-10 00:26, pop-os prod 2.4.5rc1.)
    """

    @pytest.mark.asyncio
    async def test_success_without_session_id_does_not_raise(self) -> None:
        # Exactly what the relay returns on success (src/routes/billing.ts:52).
        client = _relay_returning(
            {"success": True, "checkout_url": "https://checkout.stripe.com/c/pay/cs_live_xyz"}
        )
        resp = await client.create_checkout()
        assert resp.checkout_url == "https://checkout.stripe.com/c/pay/cs_live_xyz"
        assert resp.session_id is None  # optional — relay never sends it

    @pytest.mark.asyncio
    async def test_success_with_session_id_is_passed_through(self) -> None:
        # Forward-compat: if a future relay starts returning session_id, keep it.
        client = _relay_returning(
            {"success": True, "checkout_url": "https://x", "session_id": "cs_test_1"}
        )
        resp = await client.create_checkout()
        assert resp.session_id == "cs_test_1"

    @pytest.mark.asyncio
    async def test_error_path_200_without_checkout_url_raises_readable(self) -> None:
        # Relay error path: HTTP 200 + {success:false, error_message} and NO
        # checkout_url (src/routes/billing.ts:56-66). Must raise a readable
        # error, not a bare KeyError.
        client = _relay_returning(
            {"success": False, "error_code": "CONFIG_ERROR", "error_message": "Stripe not configured"}
        )
        with pytest.raises(RuntimeError) as ei:
            await client.create_checkout()
        assert "Stripe not configured" in str(ei.value)

    @pytest.mark.asyncio
    async def test_upgrade_tool_end_to_end_without_session_id(self) -> None:
        """The full tool path over a relay that omits session_id: returns the URL,
        omits the session_id key, and never raises ``KeyError``."""
        from totalreclaw.hermes.tools import upgrade

        state = _make_state()
        mock_client = MagicMock()
        mock_client.ensure_address = AsyncMock()
        mock_client.relay = _relay_returning(
            {"success": True, "checkout_url": "https://checkout.stripe.com/c/pay/cs_live_q"}
        )
        state._client = mock_client

        result = json.loads(await upgrade({}, state))
        assert result.get("checkout_url") == "https://checkout.stripe.com/c/pay/cs_live_q"
        assert "session_id" not in result  # dropped when the relay omits it
        assert "https://checkout.stripe.com/c/pay/cs_live_q" in result.get("message", "")
        assert "error" not in result


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
