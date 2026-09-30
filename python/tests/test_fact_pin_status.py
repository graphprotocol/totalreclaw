"""PRD-04 DEP-5 -- reading a fact's pin state from the subgraph (the pin
guard's source of truth)."""
from __future__ import annotations

import base64
import json
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from totalreclaw import operations
from totalreclaw.crypto import encrypt

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
FACT_ID = "44444444-4444-4444-8444-444444444444"

_V1_BASE = {
    "id": FACT_ID, "text": "Pedro lives in Lisbon", "type": "claim", "source": "user",
    "created_at": "2026-09-01T00:00:00Z", "schema_version": "1.0", "importance": 8,
}
V1_PINNED = json.dumps({**_V1_BASE, "pin_status": "pinned"})
V1_PLAIN = json.dumps(_V1_BASE)
V0_PINNED = json.dumps({"t": "Pedro lives in Lisbon", "c": "fact", "cf": 0.9, "i": 8, "sa": "x", "st": "p"})
# A pinned blob core cannot parse (unknown ``type`` token, e.g. written by a
# future client): core says "not pinned", the guard must still see the pin.
V1_UNPARSEABLE_PINNED = json.dumps({**_V1_BASE, "type": "memo", "pin_status": "pinned"})


def _keys(key: bytes = KEY):
    keys = mock.Mock()
    keys.encryption_key = key
    return keys


def _hex(plaintext: str, key: bytes = KEY) -> str:
    return "0x" + base64.b64decode(encrypt(plaintext, key)).hex()


def _fact(blob_hex: str, active: bool = True) -> dict:
    return {"id": FACT_ID, "owner": "0xabc", "encryptedBlob": blob_hex, "isActive": active}


class _Relay:
    def __init__(self, fact=None, exc: Exception | None = None) -> None:
        self.fact = fact
        self.exc = exc
        self.calls: list[dict] = []

    async def query_subgraph(self, gql: str, variables: dict) -> dict:
        self.calls.append(variables)
        if self.exc is not None:
            raise self.exc
        return {"data": {"fact": self.fact}}


@pytest.mark.parametrize(
    "plaintext, expected",
    [(V1_PINNED, True), (V1_PLAIN, False), (V0_PINNED, True)],
)
async def test_pin_state_from_decrypted_blob(plaintext: str, expected: bool) -> None:
    relay = _Relay(_fact(_hex(plaintext)))
    assert await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", relay) is expected
    assert relay.calls == [{"id": FACT_ID}]


async def test_unparseable_blob_with_pin_sentinel_counts_as_pinned() -> None:
    import totalreclaw_core

    assert totalreclaw_core.is_pinned_claim(V1_UNPARSEABLE_PINNED) is False
    relay = _Relay(_fact(_hex(V1_UNPARSEABLE_PINNED)))
    assert await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", relay) is True


async def test_missing_fact_is_not_pinned() -> None:
    assert await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", _Relay(None)) is False


async def test_inactive_fact_is_not_pinned() -> None:
    relay = _Relay(_fact(_hex(V1_PINNED), active=False))
    assert await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", relay) is False


async def test_tombstone_stub_is_not_pinned() -> None:
    relay = _Relay(_fact("0x00"))
    assert await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", relay) is False


async def test_relay_error_propagates() -> None:
    with pytest.raises(RuntimeError):
        await operations.get_fact_pin_status(FACT_ID, _keys(), "0xabc", _Relay(exc=RuntimeError("503")))


async def test_wrong_key_propagates() -> None:
    relay = _Relay(_fact(_hex(V1_PINNED)))
    with pytest.raises(Exception):
        await operations.get_fact_pin_status(FACT_ID, _keys(OTHER_KEY), "0xabc", relay)


async def test_empty_fact_id_rejected() -> None:
    with pytest.raises(ValueError):
        await operations.get_fact_pin_status("   ", _keys(), "0xabc", _Relay(None))


async def test_client_get_fact_pin_status_delegates(monkeypatch) -> None:
    from totalreclaw import client as client_mod
    from totalreclaw.client import TotalReclaw

    c = TotalReclaw.__new__(TotalReclaw)
    c._ensure_address = AsyncMock()
    c._ensure_registered = AsyncMock()
    c._keys = object()
    c._wallet_address = "0xabc"
    c._relay = object()
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(client_mod, "get_fact_pin_status", spy)
    assert await c.get_fact_pin_status("f1") is True
    spy.assert_awaited_once_with(fact_id="f1", keys=c._keys, owner="0xabc", relay=c._relay)
