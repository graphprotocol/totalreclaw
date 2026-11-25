"""internal#679: the Smart Account lookup fails closed.

When the RPC errors or answers with nothing, the lookup raises a retryable
``SmartAccountLookupError``. It never returns the EOA address, and nothing is
written, registered or cached under a wrong address.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
