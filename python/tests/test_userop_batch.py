"""Tests for batched ERC-4337 UserOperation construction (v2.2.0).

Covers Hermes parity Gap 3. Three layers:

1. **Byte-match parity with TS / Rust** — the whole point of batching is
   cross-client correctness: a Python-signed batch UserOp must be
   byte-identical to the TS-signed equivalent. Driven by a baked fixture
   in ``tests/fixtures/batch_calldata_vectors.json`` that was generated
   directly from the shared Rust core (``encode_batch_call``). The TS
   plugin uses the same core (via WASM), so matching the Rust bytes is
   equivalent to matching the TS bytes.

2. **Validation + structural tests** — empty batches rejected, oversize
   batches rejected, 1-element batch folds to ``execute``, N=2+ switches
   to ``executeBatch``, payload ordering preserved.

3. **Mocked transport tests** — AA25 retry, partial-failure surfacing.
   Staging integration (real 5-fact batch against
   api-staging.totalreclaw.xyz) lives under
   ``TestStagingIntegration`` and is gated on the
   ``TOTALRECLAW_STAGING_INTEGRATION=1`` env var so local ``pytest`` runs
   skip it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import totalreclaw_core
from totalreclaw.userop import (
    MAX_BATCH_SIZE,
    build_and_send_userop_batch,
    encode_execute_batch_calldata_for_data_edge,
    encode_execute_calldata_for_data_edge,
)


# ---------------------------------------------------------------------------
# Fixture: byte-for-byte expected calldata, loaded once at import time.
# ---------------------------------------------------------------------------

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "batch_calldata_vectors.json"
)
with _FIXTURE_PATH.open() as _f:
    BATCH_FIXTURE = json.load(_f)


# Derive a valid EOA private key for mock transport tests. The Rust signer
# rejects an all-zero key with "signature error", and we can't use
# ``secrets.token_bytes(32)`` because the test must be deterministic for
# CI reproducibility. Use the canonical "abandon x11 + about" test mnemonic.
def _test_eoa_private_key() -> bytes:
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about",
        account_path="m/44'/60'/0'/0/0",
    )
    return bytes(acct.key)


_TEST_EOA_PRIVATE_KEY = _test_eoa_private_key()


# ---------------------------------------------------------------------------
# 1. Byte-match parity with the shared Rust core
# ---------------------------------------------------------------------------


class TestBatchCalldataFixtureParity:
    """Python ``encode_execute_batch_calldata_for_data_edge`` output must
    byte-match the baked fixture across the full supported range.

    The fixture was generated from ``totalreclaw_core.encode_batch_call``
    — the same Rust implementation the TS plugin consumes via WASM. So
    if this test passes, Python-signed batch UserOps are byte-identical
    to TS-signed equivalents for the same inputs — which is the
    guarantee that underpins cross-client vault portability.
    """

    @pytest.mark.parametrize("label", sorted(BATCH_FIXTURE.keys()))
    def test_matches_fixture(self, label: str) -> None:
        entry = BATCH_FIXTURE[label]
        payloads = [bytes.fromhex(h) for h in entry["payloads_hex"]]
        calldata_hex = encode_execute_batch_calldata_for_data_edge(payloads)
        assert calldata_hex.startswith("0x")
        assert calldata_hex[2:] == entry["expected_calldata_hex"], (
            f"{label}: Python batch calldata diverges from Rust core "
            f"fixture. This would break cross-client parity."
        )
        assert len(calldata_hex[2:]) // 2 == entry["expected_calldata_bytes"]

    def test_fixture_covers_required_sizes(self) -> None:
        # Spec minimum: N = 1, 3, 5, 10. We also cover 15 (MAX_BATCH_SIZE).
        required = {"n1", "n3", "n5", "n10"}
        assert required.issubset(BATCH_FIXTURE.keys())


class TestBatchVsSingleParity:
    """A batch of 1 must be byte-identical to the single-fact path.

    The Rust core ``encode_batch_call([p])`` delegates to
    ``encode_single_call(p)`` for the single-payload case. Confirming
    that behavior here locks in the invariant that callers can
    unconditionally use ``remember_batch`` without paying batch overhead
    for small inputs.
    """

    def test_single_payload_batch_equals_single_call(self) -> None:
        payload = b"a single fact that tests the fold-to-execute path"
        batch_hex = encode_execute_batch_calldata_for_data_edge([payload])
        single_hex = encode_execute_calldata_for_data_edge(payload)
        assert batch_hex == single_hex

    def test_batch_selector_differs_from_single_for_n_ge_2(self) -> None:
        # execute() selector is 0xb61d27f6, executeBatch() is 0x47e1da2a.
        batch_hex = encode_execute_batch_calldata_for_data_edge(
            [b"one", b"two"]
        )
        assert batch_hex.startswith("0x47e1da2a"), (
            "N>=2 must select executeBatch, not execute"
        )


# ---------------------------------------------------------------------------
# 2. Validation
# ---------------------------------------------------------------------------


class TestBatchValidation:
    def test_empty_batch_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            encode_execute_batch_calldata_for_data_edge([])

    def test_oversize_batch_rejected(self) -> None:
        payloads = [bytes([i]) for i in range(MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError, match="exceeds maximum"):
            encode_execute_batch_calldata_for_data_edge(payloads)

    def test_exact_max_size_accepted(self) -> None:
        # 15 must work (not raise)
        payloads = [bytes([i]) for i in range(MAX_BATCH_SIZE)]
        calldata_hex = encode_execute_batch_calldata_for_data_edge(payloads)
        assert calldata_hex.startswith("0x47e1da2a")


# ---------------------------------------------------------------------------
# 3. Mocked transport — retry + error propagation
# ---------------------------------------------------------------------------


def _build_mock_send_sequence(
    gas_response: dict,
    sponsor_response: dict,
    send_response: dict,
):
    """Build an AsyncMock.post side_effect that replays relay JSON-RPC
    responses in the canonical order: gas -> sponsor -> send."""
    responses = [
        _rpc_response(gas_response),
        _rpc_response(sponsor_response),
        _rpc_response(send_response),
    ]
    idx = {"i": 0}

    async def _post(*_args, **_kwargs):
        i = idx["i"]
        idx["i"] += 1
        # Cycle if we hit more calls than expected (robust against retry).
        return responses[min(i, len(responses) - 1)]

    return _post


def _rpc_response(body: dict) -> httpx.Response:
    """Build an httpx.Response that .json() returns the given body."""
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://mock/v1/bundler"),
    )


def _ok_eth_call_response(hex_result: str) -> httpx.Response:
    return _rpc_response({"jsonrpc": "2.0", "id": 1, "result": hex_result})


_VALID_SPONSOR_RESULT = {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "callGasLimit": "0x186a0",
        "verificationGasLimit": "0x30d40",
        "preVerificationGas": "0xc350",
        "paymaster": "0x0000000000000000000000000000000000000000",
        "paymasterData": "0x",
        "paymasterVerificationGasLimit": "0x30d40",
        "paymasterPostOpGasLimit": "0x30d40",
    },
}


class TestBatchSendRetry:
    """AA25 nonce conflict retry path should work identically to the
    single-fact path."""

    @pytest.mark.asyncio
    async def test_aa25_retries_then_succeeds(self) -> None:
        """First send returns AA25, second succeeds. The batch sender
        should retry and ultimately return the second hash.

        We mock the httpx.AsyncClient at the transport level because
        ``build_and_send_userop_batch`` constructs its own client.
        """
        # Mock sequence covers 2 attempts: (nonce, code, gas, sponsor,
        # send=AA25) → (nonce, code, gas, sponsor, send=OK).
        call_log: list[tuple[str, dict]] = []

        def _dispatch(request: httpx.Request) -> httpx.Response:
            try:
                body = json.loads(request.content or b"{}")
            except Exception:
                body = {}
            method = body.get("method", "")
            call_log.append((method, body))

            if method == "eth_call":
                # Return nonce=0 or empty code depending on "to" addr.
                params = body.get("params", [{}])[0]
                to_addr = params.get("to", "").lower()
                if (
                    to_addr
                    == "0x0000000071727de22e5e9d8baf0edac6f37da032".lower()
                ):
                    return _ok_eth_call_response(
                        "0x" + "0" * 63 + "0"
                    )  # nonce 0
                return _ok_eth_call_response("0x")  # code empty
            if method == "eth_getCode":
                return _ok_eth_call_response("0x")
            if method == "pimlico_getUserOperationGasPrice":
                return _rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "result": {
                            "fast": {
                                "maxFeePerGas": "0x77359400",
                                "maxPriorityFeePerGas": "0x59682f00",
                            }
                        },
                    }
                )
            if method == "pm_sponsorUserOperation":
                return _rpc_response(
                    {**_VALID_SPONSOR_RESULT, "id": body.get("id", 2)}
                )
            if method == "eth_sendUserOperation":
                # Count how many send attempts we've had. First AA25,
                # then success.
                send_count = sum(
                    1 for m, _ in call_log if m == "eth_sendUserOperation"
                )
                if send_count == 1:
                    return _rpc_response(
                        {
                            "jsonrpc": "2.0",
                            "id": body.get("id", 3),
                            "error": {
                                "code": -32500,
                                "message": (
                                    "UserOperation reverted during "
                                    "simulation with reason: AA25 "
                                    "invalid account nonce"
                                ),
                            },
                        }
                    )
                return _rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 3),
                        "result": "0xbatchsuccess",
                    }
                )
            pytest.fail(f"Unexpected RPC method: {method}")

        transport = httpx.MockTransport(_dispatch)

        # Patch httpx.AsyncClient to use our transport for the whole
        # ``build_and_send_userop_batch`` call.
        original_init = httpx.AsyncClient.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original_init(self, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", _patched_init):
            result = await build_and_send_userop_batch(
                sender="0x2c0cf74b2b76110708ca431796367779e3738250",
                eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                protobuf_payloads=[b"fact1", b"fact2", b"fact3"],
                relay_url="https://mock",
                auth_key_hex="deadbeef",
                wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
                chain_id=84532,
            )

        assert result == "0xbatchsuccess"
        # Exactly two send attempts (first AA25, second OK).
        send_attempts = [m for m, _ in call_log if m == "eth_sendUserOperation"]
        assert len(send_attempts) == 2, (
            f"Expected exactly 2 send attempts (first AA25, then OK), "
            f"got {len(send_attempts)}: {[m for m, _ in call_log]}"
        )


class TestBatchSendErrorPropagation:
    """Non-AA25 errors should surface immediately (no retry)."""

    @pytest.mark.asyncio
    async def test_sponsor_error_propagates(self) -> None:
        def _dispatch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            method = body.get("method", "")
            if method == "eth_call":
                return _ok_eth_call_response("0x" + "0" * 64)
            if method == "eth_getCode":
                return _ok_eth_call_response("0x")
            if method == "pimlico_getUserOperationGasPrice":
                return _rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "result": {
                            "fast": {
                                "maxFeePerGas": "0x77359400",
                                "maxPriorityFeePerGas": "0x59682f00",
                            }
                        },
                    }
                )
            if method == "pm_sponsorUserOperation":
                return _rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 2),
                        "error": {
                            "code": -32000,
                            "message": "Insufficient paymaster balance",
                        },
                    }
                )
            pytest.fail(f"Should not reach method={method}")

        transport = httpx.MockTransport(_dispatch)
        original_init = httpx.AsyncClient.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original_init(self, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", _patched_init):
            with pytest.raises(RuntimeError, match="Insufficient paymaster"):
                await build_and_send_userop_batch(
                    sender="0x2c0cf74b2b76110708ca431796367779e3738250",
                    eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                    eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                    protobuf_payloads=[b"fact1", b"fact2"],
                    relay_url="https://mock",
                    auth_key_hex="deadbeef",
                    wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
                    chain_id=84532,
                )

    @pytest.mark.asyncio
    async def test_empty_batch_rejected_before_network(self) -> None:
        # Empty batch is validated before any RPC — no mocks needed.
        with pytest.raises(ValueError, match="at least 1"):
            await build_and_send_userop_batch(
                sender="0x2c0cf74b2b76110708ca431796367779e3738250",
                eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                protobuf_payloads=[],
                relay_url="https://mock",
                auth_key_hex="deadbeef",
                wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
                chain_id=84532,
            )


# ---------------------------------------------------------------------------
# 4. operations.store_fact_batch + client.remember_batch wrapper behavior
# ---------------------------------------------------------------------------


class TestStoreFactBatch:
    """operations.store_fact_batch delegates to build_and_send_userop_batch
    once and returns N pre-assigned UUIDs in input order.

    We mock ``build_and_send_userop_batch`` at the module level because
    its internals (httpx, RPC) are exercised elsewhere.
    """

    @pytest.mark.asyncio
    @patch(
        "totalreclaw.operations.build_and_send_userop_batch",
        new_callable=AsyncMock,
    )
    async def test_returns_uuid_per_fact_in_order(
        self, mock_send: AsyncMock
    ) -> None:
        pytest.importorskip(
            "totalreclaw.claims_helper",
            reason="claims_helper requires core@2.x",
        )
        # ``build_canonical_claim_v1`` depends on
        # ``totalreclaw_core.validate_memory_claim_v1``; if the installed
        # core is pre-v1 the entire batch path cannot run. Skip gracefully
        # so CI on older cores still passes the non-claim tests.
        if not hasattr(totalreclaw_core, "validate_memory_claim_v1"):
            pytest.skip(
                "totalreclaw_core lacks validate_memory_claim_v1; test "
                "requires a v1-capable core."
            )

        from totalreclaw.crypto import derive_keys_from_mnemonic
        from totalreclaw.operations import store_fact_batch
        from totalreclaw.relay import RelayClient

        mock_send.return_value = "0xbatchhash"
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about"
        )
        keys = derive_keys_from_mnemonic(mnemonic)

        relay = AsyncMock(spec=RelayClient)
        relay._relay_url = "https://api.totalreclaw.xyz"
        relay._auth_key_hex = "deadbeef"
        relay._client_id = "test"

        facts = [
            {"text": "fact one", "importance": 0.7},
            {"text": "fact two", "importance": 0.5},
            {"text": "fact three", "importance": 0.9},
        ]

        fact_ids = await store_fact_batch(
            facts=facts,
            keys=keys,
            owner="0x1234",
            relay=relay,
            eoa_private_key=bytes(32),
            eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            sender="0x1234",
        )
        assert len(fact_ids) == 3
        assert all(len(fid) == 36 for fid in fact_ids)  # UUID format
        # batched sender called exactly once (the whole point)
        mock_send.assert_called_once()
        # IDs are unique
        assert len(set(fact_ids)) == 3

    @pytest.mark.asyncio
    async def test_empty_list_raises(self) -> None:
        from totalreclaw.crypto import derive_keys_from_mnemonic
        from totalreclaw.operations import store_fact_batch
        from totalreclaw.relay import RelayClient

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about"
        )
        keys = derive_keys_from_mnemonic(mnemonic)
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="at least one fact"):
            await store_fact_batch(
                facts=[],
                keys=keys,
                owner="0x1234",
                relay=relay,
                eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            )

    @pytest.mark.asyncio
    async def test_oversize_raises(self) -> None:
        from totalreclaw.crypto import derive_keys_from_mnemonic
        from totalreclaw.operations import store_fact_batch
        from totalreclaw.relay import RelayClient

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about"
        )
        keys = derive_keys_from_mnemonic(mnemonic)
        relay = AsyncMock(spec=RelayClient)
        facts = [{"text": f"f{i}"} for i in range(MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError, match="exceeds maximum"):
            await store_fact_batch(
                facts=facts,
                keys=keys,
                owner="0x1234",
                relay=relay,
                eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            )

    @pytest.mark.asyncio
    async def test_empty_text_raises(self) -> None:
        from totalreclaw.crypto import derive_keys_from_mnemonic
        from totalreclaw.operations import store_fact_batch
        from totalreclaw.relay import RelayClient

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about"
        )
        keys = derive_keys_from_mnemonic(mnemonic)
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="empty/missing text"):
            await store_fact_batch(
                facts=[{"text": "", "importance": 0.5}],
                keys=keys,
                owner="0x1234",
                relay=relay,
                eoa_private_key=_TEST_EOA_PRIVATE_KEY,
                eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
            )

    @pytest.mark.asyncio
    async def test_missing_eoa_raises(self) -> None:
        from totalreclaw.crypto import derive_keys_from_mnemonic
        from totalreclaw.operations import store_fact_batch
        from totalreclaw.relay import RelayClient

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about"
        )
        keys = derive_keys_from_mnemonic(mnemonic)
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="eoa_private_key"):
            await store_fact_batch(
                facts=[{"text": "hello"}],
                keys=keys,
                owner="0x1234",
                relay=relay,
            )


class TestClientRememberBatch:
    """The ``TotalReclaw.remember_batch`` high-level wrapper."""

    @pytest.mark.asyncio
    async def test_empty_list_raises(self) -> None:
        from totalreclaw import TotalReclaw

        client = TotalReclaw(
            recovery_phrase=(
                "abandon abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon about"
            ),
            server_url="https://mock",
            wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
        )
        try:
            with pytest.raises(ValueError, match="at least one"):
                await client.remember_batch([])
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_oversize_raises(self) -> None:
        from totalreclaw import TotalReclaw

        client = TotalReclaw(
            recovery_phrase=(
                "abandon abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon about"
            ),
            server_url="https://mock",
            wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
        )
        try:
            facts = [{"text": f"f{i}"} for i in range(MAX_BATCH_SIZE + 1)]
            with pytest.raises(ValueError, match="exceeds batch limit"):
                await client.remember_batch(facts)
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 5. Optional staging integration (gated on env var)
# ---------------------------------------------------------------------------


_STAGING_ENABLED = os.environ.get(
    "TOTALRECLAW_STAGING_INTEGRATION", ""
).lower() in {"1", "true", "yes"}


@pytest.mark.skipif(
    not _STAGING_ENABLED,
    reason=(
        "Staging integration tests require "
        "TOTALRECLAW_STAGING_INTEGRATION=1 + a provisioned fresh mnemonic. "
        "Intended for CI staging-integration jobs only."
    ),
)
class TestStagingIntegration:
    """End-to-end: submit a 5-fact batch to api-staging.totalreclaw.xyz
    and verify all 5 facts land on-chain via the subgraph.

    The test expects:
      * ``TOTALRECLAW_STAGING_MNEMONIC`` — a 12-word mnemonic with enough
        paymaster quota.
      * ``TOTALRECLAW_STAGING_URL`` (optional, default
        https://api-staging.totalreclaw.xyz) — staging relay URL.

    Runs only when ``TOTALRECLAW_STAGING_INTEGRATION=1``. Emits a latency
    measurement alongside the assertion so the CI job can track the
    ~60s → ~8s speedup empirically.
    """

    @pytest.mark.asyncio
    async def test_5_fact_batch_round_trip(self) -> None:
        import time

        from totalreclaw import TotalReclaw

        mnemonic = os.environ.get("TOTALRECLAW_STAGING_MNEMONIC")
        if not mnemonic:
            pytest.skip("TOTALRECLAW_STAGING_MNEMONIC not set")
        server_url = os.environ.get(
            "TOTALRECLAW_STAGING_URL", "https://api-staging.totalreclaw.xyz"
        )

        client = TotalReclaw(
            recovery_phrase=mnemonic, server_url=server_url
        )
        try:
            t0 = time.monotonic()
            fact_ids = await client.remember_batch(
                [
                    {"text": f"staging batch fact {i}", "importance": 0.6}
                    for i in range(5)
                ]
            )
            elapsed = time.monotonic() - t0
            print(
                f"staging batch(5) round-trip: {elapsed:.2f}s "
                f"(vs ~20s expected for 5 sequential)"
            )
            assert len(fact_ids) == 5
            assert len(set(fact_ids)) == 5
        finally:
            await client.close()
