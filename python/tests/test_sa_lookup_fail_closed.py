"""internal#679: the Smart Account lookup fails closed.

When the RPC errors or answers with nothing, the lookup raises a retryable
``SmartAccountLookupError``. It never returns the EOA address, and nothing is
written, registered or cached under a wrong address.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from totalreclaw import client as client_mod
from totalreclaw.client import (
    SmartAccountLookupError,
    TotalReclaw,
    _derive_smart_account_address,
    _get_eoa_address,
    derive_smart_account_address_strict,
)

PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
SA = "0x2c0CF74B2B76110708CA431796367779E3738250"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    answer: object = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Resp(self.answer)


def _rpc_answers(monkeypatch, answer):
    import httpx

    monkeypatch.setattr(_FakeHttp, "answer", answer)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttp)


FAILURES = [
    {"result": "0x"},
    {"result": ""},
    {"error": {"code": -32000, "message": "rate limited"}},
    ConnectionError("rpc unreachable"),
    ValueError("not json"),
]


_STRICT = derive_smart_account_address_strict


@pytest.mark.parametrize("answer", FAILURES)
@pytest.mark.asyncio
async def test_lookup_failure_raises_and_never_returns_the_eoa(monkeypatch, answer):
    _rpc_answers(monkeypatch, answer)
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE)


@pytest.mark.asyncio
async def test_success_path_is_unchanged(monkeypatch):
    _rpc_answers(monkeypatch, {"result": "0x" + "0" * 24 + SA[2:]})
    got = await _derive_smart_account_address(PHRASE)
    assert got == SA.lower()
    assert got != _get_eoa_address(PHRASE).lower()


@pytest.mark.parametrize("answer", FAILURES)
@pytest.mark.asyncio
async def test_strict_helper_returns_none_not_the_eoa(monkeypatch, answer):
    _rpc_answers(monkeypatch, answer)
    assert await _STRICT(PHRASE) is None


@pytest.mark.parametrize("answer", FAILURES)
@pytest.mark.asyncio
async def test_resolve_address_raises_and_nothing_is_written(monkeypatch, answer):
    _rpc_answers(monkeypatch, answer)
    c = TotalReclaw(mnemonic=PHRASE, relay_url="https://relay.invalid")
    relay = MagicMock(name="relay")
    c._relay = relay

    with pytest.raises(SmartAccountLookupError):
        await c.resolve_address()
    with pytest.raises(SmartAccountLookupError):
        await c.remember("a fact that must not be written")

    assert c._address_resolved is False
    assert c._wallet_address is None
    assert relay.mock_calls == [], "no relay call (register, write, read) may happen"

    # Retryable: once the RPC answers, the same client resolves the real address.
    _rpc_answers(monkeypatch, {"result": "0x" + "0" * 24 + SA[2:]})
    assert await c.resolve_address() == SA.lower()


# ---------------------------------------------------------------------------
# Endpoint fallback order, total timeout, EOA-valued results and addresses
# ---------------------------------------------------------------------------

GNOSIS = "https://rpc.gnosischain.com"
BASE_SEPOLIA = "https://sepolia.base.org"


def _record_queries(monkeypatch, behaviour):
    """Replace the single-endpoint query; ``behaviour(url)`` returns/raises."""
    seen: list[str] = []

    async def _q(eoa_address, rpc_url, timeout=15.0):
        seen.append(rpc_url)
        return await behaviour(rpc_url)

    monkeypatch.setattr(client_mod, "_query_smart_account_for_owner", _q)
    return seen


@pytest.mark.asyncio
async def test_default_order_is_gnosis_then_base_sepolia(monkeypatch):
    monkeypatch.delenv(client_mod.SA_RPC_ENV, raising=False)

    async def _none(url):
        return None

    seen = _record_queries(monkeypatch, _none)
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE)
    assert seen == [GNOSIS, BASE_SEPOLIA]


@pytest.mark.asyncio
async def test_configured_endpoint_goes_first_and_a_failed_endpoint_is_skipped(monkeypatch):
    monkeypatch.setenv(client_mod.SA_RPC_ENV, "https://configured.invalid")

    async def _first_fails(url):
        if url == "https://configured.invalid":
            raise ConnectionError("down")
        return SA.lower()

    seen = _record_queries(monkeypatch, _first_fails)
    assert await _derive_smart_account_address(PHRASE) == SA.lower()
    # configured + gnosis queried together; only one answered, so the next is tried
    assert seen[:2] == ["https://configured.invalid", GNOSIS]


@pytest.mark.asyncio
async def test_explicit_rpc_url_wins_over_env(monkeypatch):
    monkeypatch.setenv(client_mod.SA_RPC_ENV, "https://configured.invalid")

    async def _none(url):
        return None

    seen = _record_queries(monkeypatch, _none)
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE, "https://explicit.invalid")
    assert seen[:2] == ["https://explicit.invalid", "https://configured.invalid"]


@pytest.mark.asyncio
async def test_total_time_is_bounded(monkeypatch):
    import asyncio

    monkeypatch.setattr(client_mod, "_SA_TOTAL_TIMEOUT", 0.2)

    async def _hang(url):
        await asyncio.sleep(30)

    _record_queries(monkeypatch, _hang)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE)
    assert loop.time() - t0 < 5


@pytest.mark.asyncio
async def test_an_answer_equal_to_the_eoa_is_not_accepted(monkeypatch):
    eoa = _get_eoa_address(PHRASE).lower()

    async def _eoa(url):
        return eoa

    _record_queries(monkeypatch, _eoa)
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE)


def test_client_ignores_a_constructor_wallet_address_equal_to_the_eoa():
    eoa = _get_eoa_address(PHRASE)
    c = TotalReclaw(mnemonic=PHRASE, relay_url="https://relay.invalid", wallet_address=eoa)
    assert c._wallet_address is None and c._address_resolved is False
    ok = TotalReclaw(mnemonic=PHRASE, relay_url="https://relay.invalid", wallet_address=SA)
    assert ok._wallet_address == SA.lower()


@pytest.mark.asyncio
async def test_endpoint_returning_the_eoa_is_skipped_for_a_real_answer(monkeypatch):
    eoa = _get_eoa_address(PHRASE).lower()

    async def _first_is_eoa(url):
        return eoa if url == GNOSIS else SA.lower()

    _record_queries(monkeypatch, _first_is_eoa)
    assert await _derive_smart_account_address(PHRASE) == SA.lower()


@pytest.mark.asyncio
async def test_disagreeing_endpoints_raise(monkeypatch):
    async def _differ(url):
        return SA.lower() if url == GNOSIS else "0x" + "ab" * 20

    _record_queries(monkeypatch, _differ)
    with pytest.raises(SmartAccountLookupError, match="disagree"):
        await _derive_smart_account_address(PHRASE)


@pytest.mark.asyncio
async def test_two_agreeing_endpoints_are_queried_together(monkeypatch):
    async def _same(url):
        return SA.lower()

    seen = _record_queries(monkeypatch, _same)
    assert await _derive_smart_account_address(PHRASE) == SA.lower()
    assert seen == [GNOSIS, BASE_SEPOLIA]


@pytest.mark.parametrize(
    "result",
    [
        "0x" + "0" * 64,  # zero address
        "0x" + "0" * 24 + "zz" * 20,  # non-hex tail
        "0x" + "0" * 20 + SA[2:],  # too short
        "0xnothex" + "0" * 58,
        None,
        12345,
    ],
)
@pytest.mark.asyncio
async def test_malformed_factory_results_are_rejected(monkeypatch, result):
    _rpc_answers(monkeypatch, {"result": result})
    with pytest.raises(SmartAccountLookupError):
        await _derive_smart_account_address(PHRASE)


@pytest.mark.asyncio
async def test_endpoint_failures_are_logged_without_owner_or_phrase(monkeypatch, caplog):
    import logging

    async def _boom(url):
        raise ConnectionError("down")

    _record_queries(monkeypatch, _boom)
    with caplog.at_level(logging.WARNING, logger="totalreclaw.client"):
        with pytest.raises(SmartAccountLookupError) as ei:
            await _derive_smart_account_address(PHRASE)
    text = caplog.text
    assert "rpc.gnosischain.com" in text and "ConnectionError" in text
    assert _get_eoa_address(PHRASE).lower() not in text.lower()
    assert "abandon" not in text
    assert ei.value.__cause__ is not None


def _eoa_bundle():
    """A parsed bundle whose account.smart_account equals the owner EOA."""
    from totalreclaw.bundle import derive_bundle_from_mnemonic

    eoa = _get_eoa_address(PHRASE)
    return derive_bundle_from_mnemonic(PHRASE, 100, "test", eoa)


@pytest.mark.asyncio
async def test_bundle_with_eoa_smart_account_is_re_resolved(monkeypatch):
    bundle = _eoa_bundle()
    assert bundle.account.smart_account.lower() == bundle.signing.address.lower()
    c = TotalReclaw.from_bundle(bundle, server_url="https://relay.invalid")
    assert c._wallet_address is None and c._address_resolved is False

    _rpc_answers(monkeypatch, {"result": "0x" + "0" * 24 + SA[2:]})
    assert await c.resolve_address() == SA.lower()


@pytest.mark.asyncio
async def test_bundle_with_eoa_smart_account_fails_closed_on_lookup_failure(monkeypatch):
    c = TotalReclaw.from_bundle(_eoa_bundle(), server_url="https://relay.invalid")
    relay = MagicMock(name="relay")
    c._relay = relay
    _rpc_answers(monkeypatch, ConnectionError("down"))
    with pytest.raises(SmartAccountLookupError):
        await c.remember("must not be written")
    assert relay.mock_calls == []


def test_bundle_with_real_smart_account_is_used_directly():
    from totalreclaw.bundle import derive_bundle_from_mnemonic

    b = derive_bundle_from_mnemonic(PHRASE, 100, "test", SA)
    c = TotalReclaw.from_bundle(b, server_url="https://relay.invalid")
    assert c._address_resolved is True and c._wallet_address == SA.lower()
