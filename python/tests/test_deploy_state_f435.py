"""F3 / internal#435 — batch writes must not die at sponsorship with -32500.

Root cause (rc13 QA, 52/78 facts lost): ``_eth_get_code`` silently read any
RPC error as ``"0x"`` (= not deployed), so the client re-attached the factory /
initCode to an ALREADY-deployed Smart Account. The paymaster then reverted the
whole ≤batch UserOp with ``-32500 "Sender does not implement validateUserOp or
factory is not deployed"`` and every fact in that batch was lost.

Fixes exercised here (all in ``totalreclaw.userop``):

* Deterministic deploy signal: ``chain_nonce > 0`` ⇒ account has executed ops ⇒
  deployed ⇒ skip ``eth_getCode`` entirely.
* A module-level ``_sender_deployed`` cache: once deployed, always deployed.
* Hardened ``_eth_get_code``: an error/absent-result response is NOT read as
  "undeployed" — it retries once then raises.
* Sponsor-stage recovery: a ``-32500`` / "already constructed" /
  "validateUserOp or factory" error marks the sender deployed and retries the
  build WITHOUT the factory.
* Receipt confirmation (internal#431) for the BATCH path: the hash is only
  returned once ``eth_getUserOperationReceipt`` reports ``success=true``; a
  false receipt or a timeout raises so the import engine counts the batch as
  FAILED, not stored.

No network: ``get_nonce`` / ``_eth_get_code`` / ``_relay_rpc`` are patched.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from totalreclaw import userop


# Deterministic EOA key (same canonical test mnemonic as test_userop_batch).
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
_SPONSOR_32500 = {
    "error": {
        "code": -32500,
        "message": (
            "UserOperation reverted during simulation with reason: Sender "
            "does not implement validateUserOp or factory is not deployed"
        ),
    }
}
# A -32500 that is NOT a redeploy signal (AA13 initCode-failed). Must NOT
# latch the account deployed / strip the factory — a fresh account's deploy
# depends on the factory.
_SPONSOR_AA13 = {
    "error": {
        "code": -32500,
        "message": (
            "UserOperation reverted during simulation with reason: AA13 "
            "initCode failed or OOG"
        ),
    }
}
_RECEIPT_OK = {"result": {"success": True, "receipt": {"transactionHash": "0xabc"}}}


def _make_relay_mock(sponsor_seq, receipt=_RECEIPT_OK):
    """Return (mock, record) replaying relay JSON-RPC by method.

    ``sponsor_seq`` is a list of sponsor responses consumed one per
    ``pm_sponsorUserOperation`` call. ``record`` captures every UserOp
    passed to the sponsor + send stages so tests can assert factory
    presence.
    """
    record = {"sponsor_userops": [], "send_userops": [], "calls": {}}

    async def _mock(http, relay_url, headers, method, params, rpc_id=1):
        record["calls"][method] = record["calls"].get(method, 0) + 1
        if method == "pimlico_getUserOperationGasPrice":
            return {"result": {"fast": {
                "maxFeePerGas": "0x77359400",
                "maxPriorityFeePerGas": "0x59682f00",
            }}}
        if method == "pm_sponsorUserOperation":
            record["sponsor_userops"].append(dict(params[0]))
            i = len(record["sponsor_userops"]) - 1
            return sponsor_seq[min(i, len(sponsor_seq) - 1)]
        if method == "eth_sendUserOperation":
            record["send_userops"].append(dict(params[0]))
            return {"result": "0xhash"}
        if method == "eth_getUserOperationReceipt":
            return receipt
        raise AssertionError(f"unexpected relay method {method}")

    return _mock, record


@pytest.fixture(autouse=True)
def _reset_caches():
    userop._reset_deployed_cache_for_tests()
    userop._reset_nonce_cache_for_tests()
    userop._reset_sender_locks_for_tests()
    yield
    userop._reset_deployed_cache_for_tests()
    userop._reset_nonce_cache_for_tests()
    userop._reset_sender_locks_for_tests()


@pytest.mark.asyncio
async def test_nonce_positive_skips_getcode_and_never_attaches_factory(monkeypatch):
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))

    async def _boom(*a, **k):
        raise AssertionError("eth_getCode must be skipped when chain_nonce > 0")

    monkeypatch.setattr(userop, "_eth_get_code", _boom)
    mock, record = _make_relay_mock([_VALID_SPONSOR])
    monkeypatch.setattr(userop, "_relay_rpc", mock)

    result = await userop.build_and_send_userop_batch(
        sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
        protobuf_payloads=[b"f1", b"f2"], relay_url="https://mock",
        auth_key_hex="dead", wallet_address=_SA, chain_id=100,
    )
    assert result == "0xhash"
    sent = record["send_userops"][0]
    assert "factory" not in sent
    assert "factoryData" not in sent
    # deployed cache learned the account is live from nonce>0.
    assert _SA.lower() in userop._sender_deployed


@pytest.mark.asyncio
async def test_getcode_error_response_is_not_read_as_undeployed():
    class _Resp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class _Http:
        def __init__(self, bodies):
            self._bodies = list(bodies)
            self.calls = 0

        async def post(self, *a, **k):
            b = self._bodies[min(self.calls, len(self._bodies) - 1)]
            self.calls += 1
            return _Resp(b)

    http = _Http([{"jsonrpc": "2.0", "id": 1,
                   "error": {"code": -32000, "message": "rate limited"}}])
    with pytest.raises(Exception):
        await userop._eth_get_code(http, "https://rpc", _SA)
    # Must have retried at least once rather than returning "0x" immediately.
    assert http.calls >= 2


@pytest.mark.asyncio
async def test_deployed_cache_marks_and_persists_across_calls(monkeypatch):
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=0))
    getcode_calls = {"n": 0}

    async def _getcode(http, rpc, addr):
        getcode_calls["n"] += 1
        return "0x"  # per getCode the account looks undeployed

    monkeypatch.setattr(userop, "_eth_get_code", _getcode)
    mock, record = _make_relay_mock([_VALID_SPONSOR])
    monkeypatch.setattr(userop, "_relay_rpc", mock)

    # First call: undeployed → factory attached → send OK → marks deployed.
    await userop.build_and_send_userop_batch(
        sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
        protobuf_payloads=[b"f1"], relay_url="https://mock",
        auth_key_hex="dead", wallet_address=_SA, chain_id=100,
    )
    assert "factory" in record["send_userops"][0]
    assert _SA.lower() in userop._sender_deployed

    # Second call: cached deployed → no getCode, no factory.
    await userop.build_and_send_userop_batch(
        sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
        protobuf_payloads=[b"f2"], relay_url="https://mock",
        auth_key_hex="dead", wallet_address=_SA, chain_id=100,
    )
    assert "factory" not in record["send_userops"][1]
    assert getcode_calls["n"] == 1  # only the first (uncached) call hit getCode


@pytest.mark.asyncio
async def test_sponsor_32500_marks_deployed_and_retries_without_factory(monkeypatch):
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=0))

    async def _getcode(http, rpc, addr):
        return "0x"  # getCode wrongly reports undeployed (the bug scenario)

    monkeypatch.setattr(userop, "_eth_get_code", _getcode)
    # gas ok → sponsor -32500 → gas ok → sponsor ok → send ok → receipt ok
    mock, record = _make_relay_mock([_SPONSOR_32500, _VALID_SPONSOR])
    monkeypatch.setattr(userop, "_relay_rpc", mock)

    result = await userop.build_and_send_userop_batch(
        sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
        protobuf_payloads=[b"f1", b"f2", b"f3"], relay_url="https://mock",
        auth_key_hex="dead", wallet_address=_SA, chain_id=100,
    )
    assert result == "0xhash"
    # First sponsor attempt carried the factory; the retry did NOT.
    assert "factory" in record["sponsor_userops"][0]
    assert "factory" not in record["sponsor_userops"][1]
    assert _SA.lower() in userop._sender_deployed


@pytest.mark.asyncio
async def test_fresh_account_non_redeploy_32500_keeps_factory_not_latched(monkeypatch):
    """Review Finding 1 (BLOCKER): a non-redeploy -32500 (AA13 initCode-failed)
    on a FRESH account must NOT latch _sender_deployed or strip the factory —
    the deploy depends on the factory. Every retry keeps the factory; the
    account is never bricked in-process."""
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=0))  # fresh

    async def _getcode(http, rpc, addr):
        return "0x"  # genuinely undeployed

    monkeypatch.setattr(userop, "_eth_get_code", _getcode)
    # Don't actually sleep on the transient-retry backoff.
    monkeypatch.setattr(userop.asyncio, "sleep", AsyncMock())
    # Every sponsor attempt returns AA13 → exhausts retries and raises.
    mock, record = _make_relay_mock([_SPONSOR_AA13, _SPONSOR_AA13, _SPONSOR_AA13])
    monkeypatch.setattr(userop, "_relay_rpc", mock)

    with pytest.raises(RuntimeError):
        await userop.build_and_send_userop_batch(
            sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
            protobuf_payloads=[b"f1", b"f2"], relay_url="https://mock",
            auth_key_hex="dead", wallet_address=_SA, chain_id=100,
        )

    # NOT falsely latched deployed — a re-import can still deploy this account.
    assert _SA.lower() not in userop._sender_deployed
    # The account retried (>1 sponsor attempt) and EVERY attempt kept the
    # factory — it was never stripped.
    assert len(record["sponsor_userops"]) >= 2
    for uo in record["sponsor_userops"]:
        assert "factory" in uo
        assert "factoryData" in uo


@pytest.mark.asyncio
async def test_batch_receipt_success_returns(monkeypatch):
    monkeypatch.setattr(
        userop, "_relay_rpc",
        AsyncMock(return_value={"result": {"success": True}}),
    )
    # Should not raise.
    await userop._await_batch_receipt(None, "https://mock", {}, "0xhash")


@pytest.mark.asyncio
async def test_batch_receipt_failure_raises(monkeypatch):
    monkeypatch.setattr(
        userop, "_relay_rpc",
        AsyncMock(return_value={"result": {"success": False}}),
    )
    with pytest.raises(RuntimeError):
        await userop._await_batch_receipt(None, "https://mock", {}, "0xhash")


@pytest.mark.asyncio
async def test_batch_receipt_timeout_raises(monkeypatch):
    monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
    monkeypatch.setattr(
        userop, "_relay_rpc",
        AsyncMock(return_value={"result": None}),  # never confirmed
    )
    with pytest.raises(RuntimeError):
        await userop._await_batch_receipt(None, "https://mock", {}, "0xhash")


@pytest.mark.asyncio
async def test_batch_send_awaits_receipt_before_returning(monkeypatch):
    """A batch whose receipt never confirms must RAISE, not return the hash —
    so the import engine counts it FAILED (stored must mean on-chain)."""
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
    monkeypatch.setattr(userop, "_BATCH_RECEIPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(userop, "_BATCH_RECEIPT_POLL_S", 0.01)
    mock, record = _make_relay_mock([_VALID_SPONSOR], receipt={"result": None})
    monkeypatch.setattr(userop, "_relay_rpc", mock)

    with pytest.raises(RuntimeError):
        await userop.build_and_send_userop_batch(
            sender=_SA, eoa_address=_EOA, eoa_private_key=_KEY,
            protobuf_payloads=[b"f1"], relay_url="https://mock",
            auth_key_hex="dead", wallet_address=_SA, chain_id=100,
        )
