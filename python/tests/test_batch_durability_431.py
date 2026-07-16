"""internal#431 — durability confirmation for bundler-accepted batch UserOps.

Residual after the #423 nonce cache: ``_record_submitted_nonce`` records the
next nonce at BUNDLER-ACCEPT time. If the bundler later DROPS an accepted op
(restart / mempool eviction / paymaster-window expiry), the on-chain nonce
never advances, so the cache now points past a GAP — up to ~10 later batch
writes queue silently behind the missing nonce (callers already hold
accept-time "success" hashes) until an AA25 finally resets the cache. If the
process exits first, those queued writes are lost.

Fix (#431, per the issue's fix shape + the nonce-gap self-heal note):

* ``_await_batch_receipt`` distinguishes a DROP (never confirmed → timeout)
  from a REVERT (mined, ``success=false``). ERC-4337 consumes the nonce on a
  MINED op even when it reverts, so only a DROP leaves a cache gap.
* On a DROP the batch loop resets the per-sender nonce cache (self-heal to
  chain truth) and resubmits at the chain-truth nonce — bounded by the
  existing retry budget. On a REVERT it raises without resetting (the nonce
  advanced; the cache is already correct).

The single-fact path is intentionally untouched: it returns at accept time by
design (auto-extraction latency matters more, and a single dropped fact
re-extracts next turn). Only the batch / import path can queue ~10 writes
behind a gap.

No network: ``get_nonce`` / ``_relay_rpc`` are patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from totalreclaw import userop
from totalreclaw.userop import (
    UserOpDroppedError,
    UserOpRevertedError,
    _await_batch_receipt,
    build_and_send_userop_batch,
)


# Canonical deterministic EOA key (same test mnemonic as the sibling suites;
# the Rust signer rejects an all-zero key).
def _test_eoa_private_key() -> bytes:
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about",
        account_path="m/44'/60'/0'/0/0",
    )
    return bytes(acct.key)


_KEY = _test_eoa_private_key()
_SA = "0x2c0cf74b2b76110708ca431796367779e3738250"
_EOA = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"

_VALID_SPONSOR = {
    "result": {
        "callGasLimit": "0x186a0",
        "verificationGasLimit": "0x30d40",
        "preVerificationGas": "0xc350",
        "paymaster": "0x0000000000000000000000000000000000000000",
        "paymasterData": "0x",
        "paymasterVerificationGasLimit": "0x30d40",
        "paymasterPostOpGasLimit": "0x30d40",
    }
}


@pytest.fixture(autouse=True)
def _reset_caches():
    userop._reset_deployed_cache_for_tests()
    userop._reset_nonce_cache_for_tests()
    userop._reset_sender_locks_for_tests()
    yield
    userop._reset_deployed_cache_for_tests()
    userop._reset_nonce_cache_for_tests()
    userop._reset_sender_locks_for_tests()


def _relay_dispatch_with_receipts(receipt_per_send):
    """Build an async ``_relay_rpc`` mock whose receipt answer depends on how
    many ``eth_sendUserOperation`` calls have already happened.

    ``receipt_per_send`` maps a 1-based send index to either the literal
    ``"drop"`` (receipt always ``None`` → ``_await_batch_receipt`` times out →
    ``UserOpDroppedError``) or a dict returned verbatim for the receipt call
    (e.g. ``{"result": {"success": True}}`` / ``{"result": {"success":
    False}}``). The mock records every submitted UserOp under
    ``record["send_userops"]`` so tests can assert the nonce each attempt
    targeted. Sponsor/gas/send answers are constant.
    """
    record = {"send_userops": [], "receipt_calls": 0}

    async def _mock(http, relay_url, headers, method, params, rpc_id=1):
        if method == "pimlico_getUserOperationGasPrice":
            return {
                "result": {
                    "fast": {
                        "maxFeePerGas": "0x77359400",
                        "maxPriorityFeePerGas": "0x59682f00",
                    }
                }
            }
        if method == "pm_sponsorUserOperation":
            return _VALID_SPONSOR
        if method == "eth_sendUserOperation":
            record["send_userops"].append(dict(params[0]))
            return {"result": "0xhash"}
        if method == "eth_getUserOperationReceipt":
            record["receipt_calls"] += 1
            send_count = len(record["send_userops"])
            spec = receipt_per_send.get(
                send_count, {"result": {"success": True}}
            )
            if spec == "drop":
                return {"result": None}  # pending forever → times out (drop)
            return spec
        raise AssertionError(f"unexpected relay method {method}")

    return _mock, record


# ---------------------------------------------------------------------------
# 1. _await_batch_receipt signals DROP vs REVERT with typed errors
# ---------------------------------------------------------------------------


class TestAwaitBatchReceiptSignals:
    """The receipt gate must let the batch loop tell a DROP (timeout, cache
    gap) apart from a REVERT (mined, nonce advanced). Both errors stay
    ``RuntimeError`` subclasses so existing callers/tests keep working."""

    @pytest.mark.asyncio
    async def test_revert_raises_userop_reverted_error(self, monkeypatch):
        # A mined op that reverted: the on-chain nonce DID advance.
        monkeypatch.setattr(
            userop,
            "_relay_rpc",
            AsyncMock(return_value={"result": {"success": False}}),
        )
        with pytest.raises(UserOpRevertedError):
            await _await_batch_receipt(None, "https://mock", {}, "0xhash")

    @pytest.mark.asyncio
    async def test_revert_error_is_runtime_error(self, monkeypatch):
        # Back-compat: pre-#431 callers catch the bare RuntimeError.
        monkeypatch.setattr(
            userop,
            "_relay_rpc",
            AsyncMock(return_value={"result": {"success": False}}),
        )
        with pytest.raises(RuntimeError):
            await _await_batch_receipt(None, "https://mock", {}, "0xhash")

    @pytest.mark.asyncio
    async def test_timeout_raises_userop_dropped_error(self, monkeypatch):
        # Never confirmed → the bundler dropped an accepted op: nonce unchanged.
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        monkeypatch.setattr(
            userop, "_relay_rpc", AsyncMock(return_value={"result": None})
        )
        with pytest.raises(UserOpDroppedError):
            await _await_batch_receipt(None, "https://mock", {}, "0xhash")

    @pytest.mark.asyncio
    async def test_dropped_error_is_runtime_error(self, monkeypatch):
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        monkeypatch.setattr(
            userop, "_relay_rpc", AsyncMock(return_value={"result": None})
        )
        with pytest.raises(RuntimeError):
            await _await_batch_receipt(None, "https://mock", {}, "0xhash")


# ---------------------------------------------------------------------------
# 2. Dropped batch self-heals the nonce gap and resubmits at chain truth
# ---------------------------------------------------------------------------


class TestDroppedBatchSelfHealsGap:
    """The heart of #431: a batch the bundler ACCEPTS then DROPS must not
    leave the nonce cache pointing past a gap."""

    @pytest.mark.asyncio
    async def test_drop_then_resubmit_targets_chain_truth_nonce(
        self, monkeypatch
    ):
        # Pre-poison the cache as if a PRIOR op was accepted at nonce 10. With
        # chain truth at 5, the next submission would normally target 11 — and
        # if THIS op then drops, 11 is a gap that queues later writes. The
        # self-heal must reset the cache so the resubmit targets 5.
        userop._record_submitted_nonce(_SA, 10)
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        # send #1 dropped, send #2 mined.
        mock, record = _relay_dispatch_with_receipts(
            {1: "drop", 2: {"result": {"success": True}}}
        )
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await build_and_send_userop_batch(
            sender=_SA,
            eoa_address=_EOA,
            eoa_private_key=_KEY,
            protobuf_payloads=[b"f1", b"f2"],
            relay_url="https://mock",
            auth_key_hex="dead",
            wallet_address=_SA,
            chain_id=100,
        )

        assert result == "0xhash"
        sends = record["send_userops"]
        assert len(sends) == 2, "a drop must trigger a resubmit"
        # First send used the poisoned (pipelined) nonce 11.
        assert int(sends[0]["nonce"], 16) == 11
        # After the drop the cache was RESET, so the resubmit targeted chain
        # truth (5) — not the stale 11/12 — closing the gap.
        assert int(sends[1]["nonce"], 16) == 5
        # Successful batch leaves the nonce advanced to 5+1 for the next send.
        assert userop._sender_next_nonce.get(_SA.lower()) == 6

    @pytest.mark.asyncio
    async def test_reverted_batch_keeps_cache_and_raises(self, monkeypatch):
        # On a revert the op MINED, so ERC-4337 consumed the nonce — the
        # accept-time cache record (nonce+1) is correct and must NOT be reset,
        # and the batch must not silently retry the same reverting calldata.
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay_dispatch_with_receipts(
            {1: {"result": {"success": False}}}
        )
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(UserOpRevertedError):
            await build_and_send_userop_batch(
                sender=_SA,
                eoa_address=_EOA,
                eoa_private_key=_KEY,
                protobuf_payloads=[b"f1"],
                relay_url="https://mock",
                auth_key_hex="dead",
                wallet_address=_SA,
                chain_id=100,
            )
        # Exactly one send — a revert is NOT retried in-call.
        assert len(record["send_userops"]) == 1
        # Cache still reflects the submitted nonce + 1 (NOT reset on revert).
        assert userop._sender_next_nonce.get(_SA.lower()) == 6

    @pytest.mark.asyncio
    async def test_exhausted_drops_reset_cache_and_raise(self, monkeypatch):
        # Every attempt drops. Even on final failure the cache must be reset
        # so a subsequent caller submission targets chain truth — no lingering
        # gap for the next process/turn to inherit.
        userop._record_submitted_nonce(_SA, 10)  # poison: would target 11
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        mock, record = _relay_dispatch_with_receipts(
            {1: "drop", 2: "drop", 3: "drop"}
        )
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(UserOpDroppedError):
            await build_and_send_userop_batch(
                sender=_SA,
                eoa_address=_EOA,
                eoa_private_key=_KEY,
                protobuf_payloads=[b"f1"],
                relay_url="https://mock",
                auth_key_hex="dead",
                wallet_address=_SA,
                chain_id=100,
            )
        # The retry budget (3) was exhausted...
        assert len(record["send_userops"]) == 3
        # ...and the cache was STILL reset despite the raise — the gap is
        # healed for whoever calls next.
        assert _SA.lower() not in userop._sender_next_nonce

    @pytest.mark.asyncio
    async def test_confirmed_batch_unaffected(self, monkeypatch):
        # Regression guard: a batch that confirms first time returns its hash
        # and advances the cache normally — no spurious reset.
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay_dispatch_with_receipts(
            {1: {"result": {"success": True}}}
        )
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await build_and_send_userop_batch(
            sender=_SA,
            eoa_address=_EOA,
            eoa_private_key=_KEY,
            protobuf_payloads=[b"f1", b"f2"],
            relay_url="https://mock",
            auth_key_hex="dead",
            wallet_address=_SA,
            chain_id=100,
        )
        assert result == "0xhash"
        assert len(record["send_userops"]) == 1
        assert userop._sender_next_nonce.get(_SA.lower()) == 6


# ---------------------------------------------------------------------------
# 4. Review findings (#431 follow-up): false-drop must NOT double-store, and a
#    first-deploy drop must re-attach the factory on resubmit
# ---------------------------------------------------------------------------


def _relay_dispatch_receipt_blackout(shared):
    """Receipt returns None while ``shared['blackout']`` is True, else
    success. Sponsor/gas/send are the standard answers; sends recorded."""
    record = {"send_userops": []}

    async def _mock(http, relay_url, headers, method, params, rpc_id=1):
        if method == "pimlico_getUserOperationGasPrice":
            return {
                "result": {
                    "fast": {
                        "maxFeePerGas": "0x77359400",
                        "maxPriorityFeePerGas": "0x59682f00",
                    }
                }
            }
        if method == "pm_sponsorUserOperation":
            return _VALID_SPONSOR
        if method == "eth_sendUserOperation":
            record["send_userops"].append(dict(params[0]))
            return {"result": "0xhash"}
        if method == "eth_getUserOperationReceipt":
            if shared.get("blackout", True):
                return {"result": None}
            return {"result": {"success": True}}
        raise AssertionError(f"unexpected relay method {method}")

    return _mock, record


class TestFalseDropDoesNotDoubleStore:
    """Review finding 1: a MINED op whose receipt is blacked out for the whole
    window must NOT be resubmitted — that writes byte-identical payloads
    on-chain twice. The drop handler re-reads chain truth first."""

    @pytest.mark.asyncio
    async def test_false_drop_with_late_receipt_returns_without_resubmit(
        self, monkeypatch
    ):
        # Chain nonce: 5 at build time; 6 when the drop handler re-reads it
        # (the op mined during the blackout). The receipt becomes readable
        # exactly when the handler does its one-shot final check.
        shared = {"blackout": True}
        nonce_reads = {"n": 0}

        async def _get_nonce(http, sender, chain_id):
            nonce_reads["n"] += 1
            if nonce_reads["n"] >= 2:
                shared["blackout"] = False  # receipt visible from now on
                return 6
            return 5

        monkeypatch.setattr(userop, "get_nonce", _get_nonce)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        mock, record = _relay_dispatch_receipt_blackout(shared)
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await build_and_send_userop_batch(
            sender=_SA,
            eoa_address=_EOA,
            eoa_private_key=_KEY,
            protobuf_payloads=[b"f1", b"f2"],
            relay_url="https://mock",
            auth_key_hex="dead",
            wallet_address=_SA,
            chain_id=100,
        )

        assert result == "0xhash"
        # THE invariant: exactly one send — no resubmit of a mined op.
        assert len(record["send_userops"]) == 1
        # Cache untouched (accept-time record 5+1 == chain truth 6).
        assert userop._sender_next_nonce.get(_SA.lower()) == 6

    @pytest.mark.asyncio
    async def test_false_drop_with_receipt_still_dark_raises_ambiguous(
        self, monkeypatch
    ):
        # Nonce advanced but the receipt NEVER becomes readable: surface
        # UserOpAmbiguousError, no resubmit, cache not reset.
        shared = {"blackout": True}  # stays dark forever
        nonce_reads = {"n": 0}

        async def _get_nonce(http, sender, chain_id):
            nonce_reads["n"] += 1
            return 5 if nonce_reads["n"] < 2 else 6

        monkeypatch.setattr(userop, "get_nonce", _get_nonce)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        mock, record = _relay_dispatch_receipt_blackout(shared)
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(userop.UserOpAmbiguousError):
            await build_and_send_userop_batch(
                sender=_SA,
                eoa_address=_EOA,
                eoa_private_key=_KEY,
                protobuf_payloads=[b"f1"],
                relay_url="https://mock",
                auth_key_hex="dead",
                wallet_address=_SA,
                chain_id=100,
            )

        assert len(record["send_userops"]) == 1, "ambiguous must not resubmit"
        assert userop._sender_next_nonce.get(_SA.lower()) == 6, (
            "cache must not be reset — accept-time record equals chain truth"
        )

    @pytest.mark.asyncio
    async def test_ambiguous_error_is_runtime_error(self):
        # Back-compat: the import engine catches bare RuntimeError.
        assert issubclass(userop.UserOpAmbiguousError, RuntimeError)


class TestFirstDeployDropReattachesFactory:
    """Review finding 2: the accept-time deploy latch (#435) is premature for
    a fresh account whose first (factory-carrying) op truly drops — the
    resubmit must re-derive deploy state and re-attach the factory."""

    @pytest.mark.asyncio
    async def test_true_drop_of_first_deploy_op_keeps_factory_on_resubmit(
        self, monkeypatch
    ):
        # Fresh account: chain nonce 0 forever (the op never mines), no code.
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=0))
        monkeypatch.setattr(
            userop, "_eth_get_code", AsyncMock(return_value="0x")
        )
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
        # send #1 dropped (true drop: nonce stays 0), send #2 mined.
        mock, record = _relay_dispatch_with_receipts(
            {1: "drop", 2: {"result": {"success": True}}}
        )
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await build_and_send_userop_batch(
            sender=_SA,
            eoa_address=_EOA,
            eoa_private_key=_KEY,
            protobuf_payloads=[b"f1"],
            relay_url="https://mock",
            auth_key_hex="dead",
            wallet_address=_SA,
            chain_id=100,
        )

        assert result == "0xhash"
        sends = record["send_userops"]
        assert len(sends) == 2
        # BOTH sends must carry the factory: the account never deployed, so
        # the accept-time latch from send #1 must have been cleared before
        # the resubmit (without the fix, send #2 omits it and AA20s forever).
        assert sends[0].get("factory"), "first send must attach the factory"
        assert sends[1].get("factory"), (
            "resubmit must RE-attach the factory — stale accept-time deploy "
            "latch not cleared"
        )
