"""Tests for ``totalreclaw.pair.remote_client`` (rc.10 relay-brokered pair).

Spins up a local WebSocket server that impersonates the relay, drives the
gateway-side ``open_remote_pair_session`` + ``await_phrase_upload`` flow
end-to-end, and asserts:

  - The open frame carries the exact gateway pubkey + PIN + client_id.
  - The opened-ack frame's token + URL are propagated to the caller.
  - The phrase round-trip decrypts correctly (ECDH + HKDF + ChaCha20-Poly1305).
  - ``complete_pairing`` is invoked with the canonical lowercase phrase.
  - The relay gets an ``ack`` frame after successful decrypt.

No real network traffic. No real ``credentials.json`` is touched — the
completion handler is a pure ``dict`` stub.
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

import pytest
import websockets

from totalreclaw.pair.crypto import (
    encrypt_pairing_payload,
    generate_gateway_keypair,
)
from totalreclaw.pair.remote_client import (
    await_phrase_upload,
    open_remote_pair_session,
    resolve_paired_smart_account,
)
# Bound at import time — conftest's autouse fixture stubs the module attribute
# so no test reaches the real RPC; the strict-helper tests below need the
# original function (with httpx patched instead).
from totalreclaw.client import derive_smart_account_address_strict as _strict_original


TEST_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class RelayStub:
    """Asyncio WS server that impersonates the relay for one session."""

    def __init__(self, token: str = "test-token-xyz123"):
        self.token = token
        self.open_received: Optional[dict] = None
        self.ack_received: Optional[dict] = None
        self.nack_received: Optional[dict] = None
        self._phrase_to_upload: Optional[str] = None
        self._server: Optional[websockets.Server] = None
        self._port: int = 0

    def set_phrase(self, phrase: str) -> None:
        self._phrase_to_upload = phrase

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def start(self) -> None:
        async def handler(ws):
            # Step 1: receive open frame
            raw = await ws.recv()
            self.open_received = json.loads(raw)
            await ws.send(
                json.dumps(
                    {
                        "type": "opened",
                        "token": self.token,
                        "short_url": f"/pair/p/{self.token}",
                        "expires_at": "2026-04-23T12:00:00Z",
                    }
                )
            )

            # Step 2: push forward frame (simulated browser respond)
            if self._phrase_to_upload is not None:
                kp_device = generate_gateway_keypair()
                gateway_pubkey = self.open_received["gateway_pubkey"]
                nonce_b64, ct_b64 = encrypt_pairing_payload(
                    sk_local_b64=kp_device.sk_b64,
                    pk_remote_b64=gateway_pubkey,
                    sid=self.token,
                    plaintext=self._phrase_to_upload.encode("utf-8"),
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "forward",
                            "client_pubkey": kp_device.pk_b64,
                            "nonce": nonce_b64,
                            "ciphertext": ct_b64,
                        }
                    )
                )

                # Step 3: await ack / nack
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw2)
                    if msg.get("type") == "ack":
                        self.ack_received = msg
                    elif msg.get("type") == "nack":
                        self.nack_received = msg
                except asyncio.TimeoutError:
                    pass

        # websockets.serve expects a handler(websocket)
        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.mark.asyncio
async def test_open_remote_pair_session_round_trip():
    """open_remote_pair_session sends {type:open} and returns token + URL."""
    relay = RelayStub(token="happy-token-abc")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="123456",
            client_id="gw-xyz",
        )

        # Gateway sent the right open frame
        assert relay.open_received == {
            "type": "open",
            "gateway_pubkey": session.keypair.pk_b64,
            "pin": "123456",
            "client_id": "gw-xyz",
        }

        # Returned URL carries the token + pubkey fragment
        assert session.token == "happy-token-abc"
        assert session.pin == "123456"
        assert "happy-token-abc" in session.url
        assert f"#pk={session.keypair.pk_b64}" in session.url
        # ws:// → http:// for user-facing URL
        assert session.url.startswith("http://")

        try:
            await session._ws.close()
        except Exception:
            pass
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_await_phrase_upload_decrypt_and_ack():
    """End-to-end: relay pushes ciphertext → gateway decrypts → acks."""
    relay = RelayStub(token="tok-e2e-1")
    relay.set_phrase(TEST_PHRASE)
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="654321",
            client_id="gw-e2e",
        )

        captured_phrase: List[str] = []

        async def complete(phrase: str) -> dict:
            captured_phrase.append(phrase)
            return {"state": "active", "account_id": "0xdeadbeef"}

        result = await await_phrase_upload(session, complete_pairing=complete)

        assert captured_phrase == [TEST_PHRASE]
        assert result == {"state": "active", "account_id": "0xdeadbeef"}
        # Relay received an ack frame
        await asyncio.sleep(0.1)
        assert relay.ack_received is not None
        assert relay.ack_received.get("type") == "ack"
        assert relay.nack_received is None
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_await_phrase_upload_invalid_phrase_sends_nack():
    """If BIP-39 validation fails, gateway sends nack instead of ack."""
    relay = RelayStub(token="tok-bad")
    # Not a 12-word phrase
    relay.set_phrase("foo bar baz")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="111111",
            client_id="gw-bad",
        )

        async def complete(phrase: str) -> dict:
            raise AssertionError("should not reach complete_pairing on invalid phrase")

        with pytest.raises(RuntimeError, match="BIP-39"):
            await await_phrase_upload(session, complete_pairing=complete)

        await asyncio.sleep(0.1)
        assert relay.ack_received is None
        assert relay.nack_received is not None
        assert relay.nack_received.get("error") == "invalid_mnemonic"
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_url_scheme_conversion_wss_to_https():
    """wss:// relay base → https:// user URL (production shape)."""
    from totalreclaw.pair.remote_client import _build_user_url

    url = _build_user_url("wss://api-staging.totalreclaw.xyz", "abc", "pubkeyvalue")
    assert url == "https://api-staging.totalreclaw.xyz/pair/p/abc#pk=pubkeyvalue"

    url2 = _build_user_url("ws://127.0.0.1:9000", "xyz", "pk2")
    assert url2 == "http://127.0.0.1:9000/pair/p/xyz#pk=pk2"


@pytest.mark.asyncio
async def test_open_session_rejects_rate_limited():
    """Relay returns {type: error, error: rate_limited} → tool raises."""

    async def handler(ws):
        _ = await ws.recv()  # receive open frame
        await ws.send(json.dumps({"type": "error", "error": "rate_limited"}))
        await ws.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="rate_limited"):
            await open_remote_pair_session(
                relay_base_url=f"ws://127.0.0.1:{port}",
                pin="123456",
                client_id="gw-rate",
            )
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# totalreclaw-relay#79 — wallet_address on the ack
# ---------------------------------------------------------------------------

SA = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"


async def _run_with_resolver(token, resolver, complete=None, **kwargs):
    relay = RelayStub(token=token)
    relay.set_phrase(TEST_PHRASE)
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url, pin="246810", client_id="gw-wallet"
        )

        async def _active(phrase: str) -> dict:
            return {"state": "active"}

        result = await await_phrase_upload(
            session,
            complete_pairing=complete or _active,
            resolve_wallet_address=resolver,
            **kwargs,
        )
        await asyncio.sleep(0.1)
        return relay, result
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_ack_carries_resolved_wallet_lowercased():
    seen: List[str] = []

    async def resolver(phrase: str):
        seen.append(phrase)
        return SA

    relay, result = await _run_with_resolver("tok-w-ok", resolver)
    assert seen == [TEST_PHRASE]
    assert result == {"state": "active"}
    assert relay.ack_received == {"type": "ack", "wallet_address": SA.lower()}


@pytest.mark.asyncio
async def test_ack_without_resolver_is_unchanged():
    relay, _ = await _run_with_resolver("tok-w-none", None)
    assert relay.ack_received == {"type": "ack"}


@pytest.mark.parametrize("answer", [None, "", "0x1234", "not-an-address", 42, SA + "00"])
@pytest.mark.asyncio
async def test_unknown_or_malformed_wallet_sends_plain_ack(answer):
    async def resolver(phrase: str):
        return answer

    relay, _ = await _run_with_resolver(f"tok-w-bad", resolver)
    assert relay.ack_received == {"type": "ack"}


@pytest.mark.asyncio
async def test_resolver_error_sends_plain_ack():
    async def resolver(phrase: str):
        raise RuntimeError("rpc down")

    relay, result = await _run_with_resolver("tok-w-err", resolver)
    assert result == {"state": "active"}
    assert relay.ack_received == {"type": "ack"}


@pytest.mark.asyncio
async def test_slow_resolver_is_bounded_and_cancelled():
    cancelled = asyncio.Event()

    async def resolver(phrase: str):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return SA

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    relay, result = await _run_with_resolver("tok-w-slow", resolver, wallet_timeout_s=0.2)
    assert loop.time() - t0 < 2.0
    assert result == {"state": "active"}
    assert relay.ack_received == {"type": "ack"}
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_resolver_runs_in_parallel_with_completion():
    """The lookup overlaps the completion handler instead of adding to it."""

    async def resolver(phrase: str):
        await asyncio.sleep(0.4)
        return SA

    async def slow_complete(phrase: str) -> dict:
        await asyncio.sleep(0.4)
        return {"state": "active"}

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    relay, _ = await _run_with_resolver("tok-w-par", resolver, complete=slow_complete)
    # Sequential would be >= 0.8s (+0.1 settle sleep); parallel ~0.5s.
    assert loop.time() - t0 < 0.75
    assert relay.ack_received == {"type": "ack", "wallet_address": SA.lower()}


@pytest.mark.asyncio
async def test_inactive_completion_never_reports_wallet():
    async def resolver(phrase: str):
        return SA

    async def failed(phrase: str) -> dict:
        return {"state": "error", "error": "credentials_write_failed"}

    relay, _ = await _run_with_resolver("tok-w-inactive", resolver, complete=failed)
    assert relay.ack_received == {"type": "ack"}


@pytest.mark.asyncio
async def test_completion_raising_cancels_resolver_and_nacks():
    cancelled = asyncio.Event()

    async def resolver(phrase: str):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def boom(phrase: str) -> dict:
        await asyncio.sleep(0.05)  # let the in-flight lookup start
        raise RuntimeError("disk full")

    relay = RelayStub(token="tok-w-raise")
    relay.set_phrase(TEST_PHRASE)
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url, pin="246810", client_id="gw-wallet"
        )
        with pytest.raises(RuntimeError, match="disk full"):
            await await_phrase_upload(
                session, complete_pairing=boom, resolve_wallet_address=resolver
            )
        await asyncio.sleep(0.1)
        assert relay.ack_received is None
        assert relay.nack_received == {"type": "nack", "error": "completion_failed"}
        assert cancelled.is_set()
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_default_call_site_resolver_is_stubbed_in_tests():
    """Guards the conftest fixture: the shared resolver never hits the RPC."""
    assert await resolve_paired_smart_account(TEST_PHRASE) is None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
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
        return _FakeResp(self.answer)


@pytest.mark.parametrize(
    "answer, expected",
    [
        ({"result": "0x" + "0" * 24 + SA[2:]}, SA.lower()),
        ({"result": "0x"}, None),  # empty — the lenient helper would return the EOA
        ({"error": {"code": -32000}}, None),
        (ConnectionError("rpc unreachable"), None),
    ],
)
@pytest.mark.asyncio
async def test_strict_derivation_never_falls_back_to_eoa(monkeypatch, answer, expected):
    import httpx

    monkeypatch.setattr(_FakeAsyncClient, "answer", answer)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    assert await _strict_original(TEST_PHRASE) == expected
