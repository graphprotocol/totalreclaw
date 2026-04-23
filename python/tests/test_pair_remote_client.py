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
)


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
