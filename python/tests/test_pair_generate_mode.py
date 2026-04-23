"""rc.10 generate-mode extensions — gateway-side tests.

Verifies the gateway:
  1. Forwards the caller's ``mode`` through ``open_remote_pair_session`` so
     the relay renders the correct pair-page variant.
  2. Accepts ``mode="generate"`` (and ``"import"``) in the ``forward`` frame
     from the relay without branching on it — decrypt + completion stay
     mode-agnostic (the ciphertext is the same in both flows).
  3. ``totalreclaw_pair`` tool defaults to ``mode="either"`` when no arg is
     provided + preserves the explicit pin for ``"generate"`` / ``"import"``.
  4. ``totalreclaw_pair`` tool accepts the new ``"either"`` enum value and
     echoes it in the returned payload.

These tests use a websocket stub (no network traffic) plus an in-memory
mock PluginState. No ``credentials.json`` is touched.
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional
from unittest.mock import MagicMock

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


TEST_PHRASE_12 = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)


class ModeAwareRelayStub:
    """Relay stub that captures the ``open`` frame + supports injecting a
    ``mode`` into the ``forward`` frame."""

    def __init__(self, token: str = "test-tok"):
        self.token = token
        self.open_received: Optional[dict] = None
        self.forward_frame_sent: Optional[dict] = None
        self.ack_received: Optional[dict] = None
        self.nack_received: Optional[dict] = None
        self._phrase: Optional[str] = None
        self._forward_mode: Optional[str] = None
        self._server: Optional[websockets.Server] = None
        self._port: int = 0

    def set_phrase(self, phrase: str, mode: Optional[str] = None) -> None:
        self._phrase = phrase
        self._forward_mode = mode

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def start(self) -> None:
        async def handler(ws):
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
            if self._phrase is not None:
                kp_device = generate_gateway_keypair()
                gateway_pubkey = self.open_received["gateway_pubkey"]
                nonce_b64, ct_b64 = encrypt_pairing_payload(
                    sk_local_b64=kp_device.sk_b64,
                    pk_remote_b64=gateway_pubkey,
                    sid=self.token,
                    plaintext=self._phrase.encode("utf-8"),
                )
                forward: dict = {
                    "type": "forward",
                    "client_pubkey": kp_device.pk_b64,
                    "nonce": nonce_b64,
                    "ciphertext": ct_b64,
                }
                if self._forward_mode is not None:
                    forward["mode"] = self._forward_mode
                self.forward_frame_sent = forward
                await ws.send(json.dumps(forward))
                try:
                    raw2 = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw2)
                    if msg.get("type") == "ack":
                        self.ack_received = msg
                    elif msg.get("type") == "nack":
                        self.nack_received = msg
                except asyncio.TimeoutError:
                    pass

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.mark.asyncio
async def test_open_session_forwards_mode_either():
    """Default ``mode='either'`` appears in the open frame verbatim."""
    relay = ModeAwareRelayStub(token="mode-either-1")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="123456",
            client_id="gw-mode-either",
            mode="either",
        )
        assert relay.open_received is not None
        assert relay.open_received.get("mode") == "either"
        try:
            await session._ws.close()
        except Exception:
            pass
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_open_session_forwards_mode_generate():
    """Explicit ``mode='generate'`` appears in the open frame."""
    relay = ModeAwareRelayStub(token="mode-gen-1")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="111111",
            client_id="gw-mode-gen",
            mode="generate",
        )
        assert relay.open_received.get("mode") == "generate"
        try:
            await session._ws.close()
        except Exception:
            pass
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_open_session_omits_mode_when_none():
    """``mode=None`` -> no ``mode`` key in the open frame (backwards-compat)."""
    relay = ModeAwareRelayStub(token="mode-none-1")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="222222",
            client_id="gw-mode-none",
            mode=None,
        )
        assert "mode" not in (relay.open_received or {})
        try:
            await session._ws.close()
        except Exception:
            pass
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_open_session_ignores_junk_mode():
    """Invalid enum values are silently dropped, not forwarded."""
    relay = ModeAwareRelayStub(token="mode-junk-1")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="333333",
            client_id="gw-mode-junk",
            mode="banana",
        )
        # Invalid enum -> omitted (don't corrupt the wire with unknown values).
        assert "mode" not in (relay.open_received or {})
        try:
            await session._ws.close()
        except Exception:
            pass
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_await_phrase_upload_accepts_forward_mode_generate():
    """``forward`` frame with ``mode='generate'`` decrypts + completes
    identically to a frame without it."""
    relay = ModeAwareRelayStub(token="tok-fwd-gen")
    relay.set_phrase(TEST_PHRASE_12, mode="generate")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="654321",
            client_id="gw-fwd-gen",
            mode="generate",
        )

        captured: List[str] = []

        async def complete(phrase: str) -> dict:
            captured.append(phrase)
            return {"state": "active", "account_id": "0xgen"}

        result = await await_phrase_upload(session, complete_pairing=complete)
        assert captured == [TEST_PHRASE_12]
        assert result == {"state": "active", "account_id": "0xgen"}
        await asyncio.sleep(0.1)
        assert relay.ack_received is not None
        assert relay.nack_received is None
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_await_phrase_upload_accepts_forward_mode_import():
    """``forward`` frame with ``mode='import'`` decrypts + completes."""
    relay = ModeAwareRelayStub(token="tok-fwd-imp")
    relay.set_phrase(TEST_PHRASE_12, mode="import")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="654321",
            client_id="gw-fwd-imp",
            mode="import",
        )

        captured: List[str] = []

        async def complete(phrase: str) -> dict:
            captured.append(phrase)
            return {"state": "active", "account_id": "0ximp"}

        result = await await_phrase_upload(session, complete_pairing=complete)
        assert captured == [TEST_PHRASE_12]
        await asyncio.sleep(0.1)
        assert relay.ack_received is not None
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_await_phrase_upload_forward_mode_junk_does_not_reject():
    """Unknown ``mode`` in forward frame is ignored (not a protocol error).

    The relay validates the enum before forwarding — if a garbage value
    slips through, the gateway should still decrypt correctly. No
    branching on mode means forward compatibility if we ever add a
    3rd mode ("generate-24" etc.) without bumping v=1.
    """
    relay = ModeAwareRelayStub(token="tok-fwd-junk")
    relay.set_phrase(TEST_PHRASE_12, mode="definitely-not-an-enum-value")
    await relay.start()
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay.url,
            pin="654321",
            client_id="gw-fwd-junk",
            mode="either",
        )
        captured: List[str] = []

        async def complete(phrase: str) -> dict:
            captured.append(phrase)
            return {"state": "active"}

        result = await await_phrase_upload(session, complete_pairing=complete)
        assert captured == [TEST_PHRASE_12]
        assert relay.ack_received is not None
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_pair_tool_default_mode_is_either(tmp_path, monkeypatch):
    """No ``mode`` arg -> payload reports ``mode='either'`` (rc.10 default)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_PORT", "0")
    monkeypatch.setenv("TOTALRECLAW_PAIR_MODE", "local")

    from totalreclaw.hermes import pair_tool as _pair_tool_mod

    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]

    state = MagicMock()
    # No 'mode' in args.
    result_json = await _pair_tool_mod.pair({}, state)
    payload = json.loads(result_json)
    assert "error" not in payload, payload
    # Tool echoes "either" in the returned payload so the agent can relay
    # the right instructions to the user.
    assert payload["mode"] == "either"


@pytest.mark.asyncio
async def test_pair_tool_accepts_explicit_either(tmp_path, monkeypatch):
    """Explicit ``mode='either'`` in args is honored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_PORT", "0")
    monkeypatch.setenv("TOTALRECLAW_PAIR_MODE", "local")

    from totalreclaw.hermes import pair_tool as _pair_tool_mod

    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]

    state = MagicMock()
    result_json = await _pair_tool_mod.pair({"mode": "either"}, state)
    payload = json.loads(result_json)
    assert payload["mode"] == "either"


@pytest.mark.asyncio
async def test_pair_tool_generate_or_import_still_honored(tmp_path, monkeypatch):
    """Existing ``'generate'`` / ``'import'`` mode pins still work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_PORT", "0")
    monkeypatch.setenv("TOTALRECLAW_PAIR_MODE", "local")

    from totalreclaw.hermes import pair_tool as _pair_tool_mod

    state = MagicMock()

    # generate
    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]
    res = await _pair_tool_mod.pair({"mode": "generate"}, state)
    assert json.loads(res)["mode"] == "generate"

    # import
    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]
    res = await _pair_tool_mod.pair({"mode": "import"}, state)
    assert json.loads(res)["mode"] == "import"


def test_pair_tool_schema_advertises_either():
    """``PAIR_SCHEMA`` must now include 'either' in the mode enum."""
    from totalreclaw.hermes.pair_tool import PAIR_SCHEMA

    mode_enum = PAIR_SCHEMA["parameters"]["properties"]["mode"]["enum"]
    assert set(mode_enum) == {"generate", "import", "either"}


@pytest.mark.asyncio
async def test_relay_mode_passes_ui_mode_to_open_session(tmp_path, monkeypatch):
    """Verify relay path forwards UI mode to ``open_remote_pair_session``.

    This test uses a spy wrapper on ``open_remote_pair_session`` to make
    sure the ``mode`` kwarg threads through ``pair_tool.pair`` -> ``_pair_relay``
    -> ``open_remote_pair_session``. No live network.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOTALRECLAW_PAIR_MODE", raising=False)  # default (relay)

    from totalreclaw.hermes import pair_tool as _pair_tool_mod
    from totalreclaw.pair import remote_client as _rc

    captured: dict = {}

    class _FakeSession:
        url = "https://example.invalid/pair/p/x#pk=y"
        pin = "123456"
        expires_at = "2026-04-23T12:00:00Z"
        token = "x"
        keypair = MagicMock(pk_b64="y")
        _ws = MagicMock()

    async def _fake_open_remote_pair_session(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(
        _rc, "open_remote_pair_session", _fake_open_remote_pair_session
    )

    # Skip the background task — we're only testing pair_tool wiring.
    monkeypatch.setattr(
        _pair_tool_mod, "_spawn_relay_completion_task", lambda s, st: None
    )

    state = MagicMock()

    for mode in ("generate", "import", "either"):
        result_json = await _pair_tool_mod.pair({"mode": mode}, state)
        payload = json.loads(result_json)
        assert "error" not in payload, payload
        assert payload["mode"] == mode
        assert captured.get("mode") == mode
