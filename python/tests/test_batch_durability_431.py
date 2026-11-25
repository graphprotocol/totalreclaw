"""internal#431 durability, restated for #646/#650.

The #431 guarantees are kept: "stored" means executed on-chain, a revert
raises (the nonce advanced; never retried in-call), a batch the bundler
drops is re-sent and a batch that never confirms raises.

What changed (#646/#650): there is no accept-time nonce cache any more, so
the old "reset the cache after a drop" self-heal has nothing to heal. Instead
every attempt of one write is PINNED to its nonce until a fate verdict says
otherwise:

* ``gone`` (the bundler dropped it AND the nonce is unused, seen twice) re-sends
  at the SAME nonce through the existing loop body;
* an op whose fate is still open at the deadline raises ``UserOpAmbiguousError``
  ("maybe stored") instead of the old "dropped" verdict, which resubmitted at
  a higher nonce and could write the batch twice;
* exhausted drops raise ``UserOpDroppedError`` after the send budget.

No network: ``get_nonce`` / ``_relay_rpc`` / ``_eth_block_number`` are patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from totalreclaw import userop
from totalreclaw.userop import (
    UserOpAmbiguousError,
    UserOpDroppedError,
    UserOpRevertedError,
    build_and_send_userop_batch,
)


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
def _reset_caches(monkeypatch):
    monkeypatch.setattr(userop, "_eth_block_number", AsyncMock(return_value=1000))
    monkeypatch.setattr(userop, "GATE_POLL_S", 0.001)
    # A mini virtual clock: sleeping advances it, so deadlines are exact and
    # never depend on how fast the machine is.
    clock = {"t": 1000.0}

    async def _vsleep(s):
        clock["t"] += max(s, 0.001)

    monkeypatch.setattr(userop, "_now", lambda: clock["t"])
    monkeypatch.setattr(userop, "_sleep", _vsleep)
    userop._reset_write_state_for_tests()
    userop._reset_sender_locks_for_tests()
    yield
    userop._reset_write_state_for_tests()
    userop._reset_sender_locks_for_tests()


def _relay(receipt_per_send):
    """``_relay_rpc`` mock. ``receipt_per_send`` maps a 1-based send index to
    ``"drop"`` (no receipt, bundler status ``rejected``), ``"dark"`` (no
    receipt, status ``submitted``) or a dict returned for the receipt call."""
    record = {"send_userops": [], "receipt_calls": 0}

    def current():
        return receipt_per_send.get(
            len(record["send_userops"]), {"result": {"success": True}}
        )

    async def _mock(http, relay_url, headers, method, params, rpc_id=1):
        if method == "pimlico_getUserOperationGasPrice":
            return {"result": {"fast": {
                "maxFeePerGas": "0x77359400", "maxPriorityFeePerGas": "0x59682f00",
            }}}
        if method == "pm_sponsorUserOperation":
            return _VALID_SPONSOR
        if method == "eth_sendUserOperation":
            record["send_userops"].append(dict(params[0]))
            return {"result": "0xhash"}
        if method == "eth_getUserOperationReceipt":
            record["receipt_calls"] += 1
            spec = current()
            return {"result": None} if spec in ("drop", "dark") else spec
        if method == "pimlico_getUserOperationStatus":
            return {"result": {"status": "rejected" if current() == "drop" else "submitted"}}
        raise AssertionError(f"unexpected relay method {method}")

    return _mock, record


async def _batch(payloads=(b"f1",), **kw):
    return await build_and_send_userop_batch(
        sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
        protobuf_payloads=list(payloads), relay_url="https://mock",
        auth_key_hex="dead", wallet_address=_SA, chain_id=100, **kw,
    )


class TestErrorTypes:
    def test_all_are_runtime_errors(self):
        for cls in (UserOpRevertedError, UserOpDroppedError, UserOpAmbiguousError):
            assert issubclass(cls, RuntimeError)

    def test_ambiguous_carries_what_callers_need(self):
        e = UserOpAmbiguousError("x", fact_ids=["a"], hashes=["0x1"], nonce=3, sent_block=9)
        assert (e.fact_ids, e.hashes, e.nonce, e.sent_block) == (["a"], ["0x1"], 3, 9)


class TestDroppedBatch:
    @pytest.mark.asyncio
    async def test_drop_then_resend_targets_the_same_pinned_nonce(self, monkeypatch):
        # A prior floor of 11 does not matter: the floor never exceeds chain
        # truth in practice, and the re-send is pinned to the first nonce.
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay({1: "drop", 2: {"result": {"success": True}}})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await _batch([b"f1", b"f2"])

        assert result.startswith("0x") and len(result) == 66
        sends = record["send_userops"]
        assert len(sends) == 2, "a confirmed drop triggers a re-send"
        assert int(sends[0]["nonce"], 16) == int(sends[1]["nonce"], 16) == 5
        assert userop._floor_of(_SA) == 6

    @pytest.mark.asyncio
    async def test_exhausted_drops_raise_after_three_sends_all_at_one_nonce(self, monkeypatch):
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay({1: "drop", 2: "drop", 3: "drop"})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(UserOpDroppedError):
            await _batch()
        sends = record["send_userops"]
        assert len(sends) == 3
        assert {int(s["nonce"], 16) for s in sends} == {5}
        assert userop.pending_records(_SA) == [], "a proven drop leaves nothing pending"

    @pytest.mark.asyncio
    async def test_first_deploy_op_dropped_reattaches_factory_on_resend(self, monkeypatch):
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=0))
        monkeypatch.setattr(userop, "_eth_get_code", AsyncMock(return_value="0x"))
        mock, record = _relay({1: "drop", 2: {"result": {"success": True}}})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        await _batch()
        sends = record["send_userops"]
        assert len(sends) == 2
        assert sends[0].get("factory") and sends[1].get("factory"), (
            "the account never deployed: the re-send must carry the factory again"
        )


class TestRevertedBatch:
    @pytest.mark.asyncio
    async def test_reverted_batch_raises_and_is_not_retried(self, monkeypatch):
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay({1: {"result": {"success": False}}})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(UserOpRevertedError):
            await _batch()
        assert len(record["send_userops"]) == 1
        # ERC-4337 consumed the nonce: the floor moves past it.
        assert userop._floor_of(_SA) == 6

    @pytest.mark.asyncio
    async def test_confirmed_batch_returns_and_advances_floor(self, monkeypatch):
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay({1: {"result": {"success": True}}})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await _batch()
        assert result.startswith("0x")
        assert len(record["send_userops"]) == 1
        assert userop._floor_of(_SA) == 6


class TestFalseDropDoesNotDoubleStore:
    """A receipt blackout must never make the client write the batch twice."""

    @pytest.mark.asyncio
    async def test_receipt_dark_but_event_on_chain_returns_without_resend(self, monkeypatch):
        calls = {"n": 0}

        async def nonce(http, sender, chain_id, block=None):
            calls["n"] += 1
            return 5 if calls["n"] == 1 else 6     # the op mined after the first read

        monkeypatch.setattr(userop, "get_nonce", nonce)

        async def events(http, rpc_url, hashes, a, b):
            return {h.lower(): True for h in hashes}

        monkeypatch.setattr(userop, "_find_userop_events", events)
        mock, record = _relay({1: "dark"})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        result = await _batch()
        assert result.startswith("0x")
        assert len(record["send_userops"]) == 1

    @pytest.mark.asyncio
    async def test_open_fate_at_the_deadline_is_ambiguous_not_dropped(self, monkeypatch):
        monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
        mock, record = _relay({1: "dark"})
        monkeypatch.setattr(userop, "_relay_rpc", mock)

        with pytest.raises(UserOpAmbiguousError) as ei:
            await _batch(deadline_s=0.05, fact_ids=["a"])
        assert len(record["send_userops"]) == 1, "never re-sent while the fate is open"
        assert ei.value.fact_ids == ["a"] and ei.value.nonce == 5
        assert len(userop.pending_records(_SA)) == 1, "tracked until a verdict"
