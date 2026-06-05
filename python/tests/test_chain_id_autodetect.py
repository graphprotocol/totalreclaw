"""Bug #11 regression tests — chain_id auto-detect from billing tier.

QA-V1CLEAN-VPS-20260418 called this out as KNOWN: the Python client
hardcoded chain_id = 84532 (Base Sepolia) for every write, but the relay
routes Pro-tier users to chain 100 (Gnosis mainnet). A Pro user on the
Python client therefore had all writes signed for Base Sepolia while the
relay expected Gnosis — producing silent AA23 signature failures.

The MCP server implements auto-detect at ``mcp/src/index.ts`` ~line 346:
read ``/v1/billing/status?wallet_address=<sa>`` once, and if
``tier === 'pro'`` use chain 100; otherwise default to 84532. Best-effort:
any error falls back to free-tier chain so offline / misconfigured users
still work.

Cross-implementation contract (keep these parity invariants green):

* Free tier / no tier field / error -> chain 84532.
* ``tier == "pro"`` -> chain 100.
* The auto-detect is triggered lazily on first write.
* The user-facing ``TOTALRECLAW_CHAIN_ID`` override was removed in the v1
  env cleanup and must not be respected by this client.
"""
from __future__ import annotations

import json as _json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest


TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SA = "0x2c0cf74b2b76110708ca431796367779e3738250"


def _make_client(**kwargs):
    """Factory for a TotalReclaw with a preset SA so no RPC call is needed."""
    from totalreclaw.client import TotalReclaw

    c = TotalReclaw(
        recovery_phrase=TEST_MNEMONIC,
        wallet_address=TEST_SA,
        **kwargs,
    )
    c._registered = True
    return c


def _patch_billing(response_body: dict | None, status_code: int = 200, raise_exc: Exception | None = None):
    """Patch httpx.AsyncClient globally for the billing endpoint.

    Returns the captured request list so tests can assert the URL + headers.
    """

    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if raise_exc is not None:
            raise raise_exc
        if response_body is None:
            return httpx.Response(status_code, text="")
        return httpx.Response(status_code, json=response_body)

    transport = httpx.MockTransport(_handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kw):
            kw.pop("transport", None)
            super().__init__(*args, transport=transport, **kw)

    return patch("totalreclaw.client._httpx.AsyncClient", _PatchedAsyncClient), captured


# ---------------------------------------------------------------------------
# Basic resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_chain_id_free_tier_defaults_to_base_sepolia() -> None:
    client = _make_client()
    patch_ctx, captured = _patch_billing({"tier": "free", "free_writes_used": 2, "free_writes_limit": 500})
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 84532
    assert client._chain_id_resolved is True
    # Confirm the URL was the billing endpoint with wallet_address param.
    assert len(captured) == 1
    req = captured[0]
    assert "/v1/billing/status" in str(req.url)
    assert TEST_SA in str(req.url)


@pytest.mark.asyncio
async def test_resolve_chain_id_pro_tier_returns_gnosis() -> None:
    """``tier == 'pro'`` switches the client to chain 100 (Gnosis)."""
    client = _make_client()
    patch_ctx, _ = _patch_billing({"tier": "pro", "free_writes_used": 0, "free_writes_limit": 0})
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 100


@pytest.mark.asyncio
async def test_resolve_chain_id_falls_back_to_free_on_network_error() -> None:
    """Any httpx error -> default to 84532 (matches MCP 'best-effort')."""
    client = _make_client()
    patch_ctx, _ = _patch_billing(None, raise_exc=httpx.ConnectError("DNS down"))
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 84532


@pytest.mark.asyncio
async def test_resolve_chain_id_falls_back_on_non_200_response() -> None:
    """HTTP 500 from billing -> default to free-tier chain."""
    client = _make_client()
    patch_ctx, _ = _patch_billing({"error": "internal"}, status_code=500)
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 84532


@pytest.mark.asyncio
async def test_resolve_chain_id_is_cached_after_first_call() -> None:
    """Second ``resolve_chain_id`` must not re-hit the network."""
    client = _make_client()
    patch_ctx, captured = _patch_billing({"tier": "pro"})
    with patch_ctx:
        await client.resolve_chain_id()
        await client.resolve_chain_id()
        await client.resolve_chain_id()
    assert len(captured) == 1, (
        "resolve_chain_id should cache — second calls must not re-hit billing."
    )


@pytest.mark.asyncio
async def test_chain_id_property_raises_before_resolve() -> None:
    """``.chain_id`` must fail loud pre-resolve so callers can't silently
    use the uninitialized default."""
    client = _make_client()
    with pytest.raises(RuntimeError, match="not yet resolved"):
        _ = client.chain_id


# ---------------------------------------------------------------------------
# Lazy resolution on write paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_resolves_chain_id_then_signs_for_pro() -> None:
    """A Pro user's ``remember`` must sign for Gnosis (chain 100).

    Before Bug #11 was fixed, the Python client silently signed for 84532
    regardless of tier and the relay routed to Gnosis, producing AA23 on
    every write. This test pins the contract: after auto-detect fires,
    ``build_and_send_userop`` sees chain_id=100.
    """
    from totalreclaw.client import TotalReclaw

    client = _make_client()

    # Patch the internal httpx call for billing detection.
    billing_patch, _ = _patch_billing({"tier": "pro"})
    # Capture the chain_id that flows into the userop builder.
    mock_userop = AsyncMock(return_value="0xabc123")

    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.remember("Pro user fact")

    # build_and_send_userop should have been invoked with chain_id=100.
    kwargs = mock_userop.await_args.kwargs
    assert kwargs["chain_id"] == 100, (
        f"Expected chain_id=100 for Pro tier; got {kwargs['chain_id']}"
    )


@pytest.mark.asyncio
async def test_remember_defaults_to_base_sepolia_for_free_tier() -> None:
    client = _make_client()
    billing_patch, _ = _patch_billing({"tier": "free"})
    mock_userop = AsyncMock(return_value="0xabc123")

    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.remember("Free user fact")

    assert mock_userop.await_args.kwargs["chain_id"] == 84532


@pytest.mark.asyncio
async def test_forget_uses_detected_chain_id() -> None:
    client = _make_client()
    billing_patch, _ = _patch_billing({"tier": "pro"})
    mock_userop = AsyncMock(return_value="0xabc123")

    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.forget("fact-uuid")

    assert mock_userop.await_args.kwargs["chain_id"] == 100


# ---------------------------------------------------------------------------
# v1 env cleanup: TOTALRECLAW_CHAIN_ID override is NOT respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_chain_id_env_is_ignored() -> None:
    """The removed ``TOTALRECLAW_CHAIN_ID`` env var must not influence
    chain selection — auto-detect is authoritative, matching the MCP
    behavior where the env var was also removed."""
    with patch.dict(os.environ, {"TOTALRECLAW_CHAIN_ID": "9999"}):
        client = _make_client()
        billing_patch, _ = _patch_billing({"tier": "free"})
        with billing_patch:
            chain_id = await client.resolve_chain_id()
    assert chain_id == 84532  # free-tier default, NOT 9999


# ---------------------------------------------------------------------------
# Root fix (#364 / relay #21): consume the relay's AUTHORITATIVE chain_id.
#
# After ops-1 (single-chain Gnosis), the relay's GET /v1/billing/status
# returns an explicit ``chain_id`` sourced from getChainConfigForTier(tier).
# The client MUST consume it verbatim and stop deriving the chain from the
# hardcoded tier->chain map. This lets a free user be flipped to Gnosis with
# ZERO client release — the relay env change propagates automatically.
#
# The local tier->chain map is retained ONLY as a graceful fallback for an
# old relay that doesn't yet emit the field (additive/non-breaking rollout).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authoritative_chain_id_overrides_free_tier_map() -> None:
    """THE migration scenario: free tier but relay says chain_id=100.

    Post ops-1 the relay reports a free user's authoritative chain as Gnosis.
    The client must sign for 100 even though the legacy tier-map would say
    84532 for ``tier == 'free'``.
    """
    client = _make_client()
    patch_ctx, _ = _patch_billing({"tier": "free", "chain_id": 100})
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 100


@pytest.mark.asyncio
async def test_authoritative_chain_id_overrides_pro_tier_map() -> None:
    """Authoritative field wins in BOTH directions — pro reported on 84532."""
    client = _make_client()
    patch_ctx, _ = _patch_billing({"tier": "pro", "chain_id": 84532})
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 84532


@pytest.mark.asyncio
async def test_falls_back_to_tier_map_when_chain_id_field_absent() -> None:
    """Old relay (no chain_id field) -> graceful fallback to the tier-map."""
    client = _make_client()
    patch_ctx, _ = _patch_billing({"tier": "pro"})  # no chain_id key
    with patch_ctx:
        chain_id = await client.resolve_chain_id()
    assert chain_id == 100  # tier-map fallback still works


@pytest.mark.asyncio
async def test_invalid_chain_id_field_falls_back_to_tier_map() -> None:
    """A malformed chain_id (non-int / null / zero) is ignored — fall back
    to the tier-map rather than signing for a garbage chain."""
    for bad in ("not-a-number", None, 0, -1, 1.5):
        client = _make_client()
        patch_ctx, _ = _patch_billing({"tier": "free", "chain_id": bad})
        with patch_ctx:
            chain_id = await client.resolve_chain_id()
        assert chain_id == 84532, f"bad chain_id {bad!r} should fall back to tier-map"


@pytest.mark.asyncio
async def test_remember_signs_for_authoritative_chain_id() -> None:
    """End-to-end: a free user whose relay reports chain_id=100 must have
    ``remember`` sign the UserOp for Gnosis (100), not Base Sepolia."""
    client = _make_client()
    billing_patch, _ = _patch_billing({"tier": "free", "chain_id": 100})
    mock_userop = AsyncMock(return_value="0xabc123")

    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.remember("Free user migrated to Gnosis")

    assert mock_userop.await_args.kwargs["chain_id"] == 100


# ---------------------------------------------------------------------------
# Relay-authoritative DataEdge (#366): consume data_edge_address from billing
# and thread it to the write path. Pairs with the chain_id root fix above —
# rc5 signed for the right chain but wrote to the wrong (hardcoded) DataEdge,
# stranding facts on the isolated staging Gnosis DataEdge.
# ---------------------------------------------------------------------------

STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91"


@pytest.mark.asyncio
async def test_resolve_captures_authoritative_data_edge_address() -> None:
    client = _make_client()
    patch_ctx, _ = _patch_billing(
        {"tier": "free", "chain_id": 100, "data_edge_address": STAGING_DATA_EDGE}
    )
    with patch_ctx:
        await client.resolve_chain_id()
    assert client._data_edge_address == STAGING_DATA_EDGE


@pytest.mark.asyncio
async def test_remember_threads_data_edge_address_to_write() -> None:
    """THE ops-9 fix: a staging free user must write to the relay-reported
    DataEdge, not the hardcoded default."""
    client = _make_client()
    billing_patch, _ = _patch_billing(
        {"tier": "free", "chain_id": 100, "data_edge_address": STAGING_DATA_EDGE}
    )
    mock_userop = AsyncMock(return_value="0xabc123")
    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.remember("Free user on isolated staging Gnosis")
    assert mock_userop.await_args.kwargs["data_edge_address"] == STAGING_DATA_EDGE


@pytest.mark.asyncio
async def test_data_edge_absent_threads_none() -> None:
    """Old relay (no data_edge_address) → None → core default (prod-correct)."""
    client = _make_client()
    billing_patch, _ = _patch_billing({"tier": "free", "chain_id": 100})
    mock_userop = AsyncMock(return_value="0xabc123")
    with billing_patch, patch(
        "totalreclaw.operations.build_and_send_userop", new=mock_userop,
    ):
        await client.remember("No data edge field")
    assert mock_userop.await_args.kwargs["data_edge_address"] is None


@pytest.mark.asyncio
async def test_malformed_data_edge_address_ignored() -> None:
    """A non-address value is ignored rather than risking a wrong target."""
    for bad in ("not-an-address", "0x1234", "0xZZZ", 100, None, ""):
        client = _make_client()
        patch_ctx, _ = _patch_billing(
            {"tier": "free", "chain_id": 100, "data_edge_address": bad}
        )
        with patch_ctx:
            await client.resolve_chain_id()
        assert client._data_edge_address is None, f"bad data_edge {bad!r} should be ignored"


def test_encode_wrapper_passes_data_edge_to_core() -> None:
    """userop.py wrapper forwards an explicit DataEdge to the core encoder."""
    from unittest.mock import MagicMock
    import totalreclaw.userop as uop

    fake_core = MagicMock()
    fake_core.encode_single_call.return_value = b"\xde\xad"
    with patch.object(uop, "totalreclaw_core", fake_core):
        uop.encode_execute_calldata_for_data_edge(b"payload", STAGING_DATA_EDGE)
    fake_core.encode_single_call.assert_called_once_with(b"payload", STAGING_DATA_EDGE)

    fake_core.reset_mock()
    fake_core.encode_single_call.return_value = b"\xbe\xef"
    with patch.object(uop, "totalreclaw_core", fake_core):
        uop.encode_execute_calldata_for_data_edge(b"payload")  # default path
    # Default path must NOT pass a 2nd arg (back-compat with pre-2.5.0 core).
    fake_core.encode_single_call.assert_called_once_with(b"payload")
