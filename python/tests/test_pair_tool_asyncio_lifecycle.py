"""rc.13 regression test for the tool-invocation-loop asyncio lifecycle fix.

Prior RCs (rc.10–rc.12) opened the relay WebSocket on Hermes's
tool-invocation loop, then used ``loop.create_task(...)`` to keep
awaiting on it after the tool returned. That loop is torn down as soon
as the tool returns, so the background task was destroyed mid-
``ws.recv()`` — logged as::

    Task was destroyed but it is pending!
    RuntimeError: no running event loop

during ``WebSocketCommonProtocol.close_connection``. The relay saw no
ack, timed out after 15s, and returned 502 to the browser.

rc.13 runs the ENTIRE relay session on a dedicated OS thread with its
own ``asyncio.new_event_loop()``. The WebSocket is created INSIDE the
thread, so it's never bound to a loop that closes mid-session.

This test proves the fix end-to-end:

1. Call ``_run_relay_pair_on_thread`` (the rc.13 helper used by the
   tool body via ``asyncio.to_thread``).
2. The relay stub delays the ``forward`` frame by 400ms — long enough
   that rc.12's tool-loop-destroyed bug would have fired.
3. Assert the ack frame is received + ``state.configure(phrase)`` is
   called with the decrypted phrase.

If the fix regresses (WS gets bound to the caller loop again, or the
waiter runs off the tool loop), the ack never arrives because the loop
closes before ``recv()`` wakes.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import List, Optional
from unittest.mock import MagicMock

import pytest
import websockets

from totalreclaw.pair.crypto import (
    encrypt_pairing_payload,
    generate_gateway_keypair,
)


TEST_PHRASE = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


class _DelayedRelayStub:
    """WebSocket server that impersonates the relay with a delay between
    ``opened`` and ``forward`` — mirroring real usage where the browser
    takes human time to type the PIN + paste the phrase.

    The delay proves the fix: without the rc.13 thread-loop isolation,
    the tool loop closes before the delay elapses and the waiter dies.
    """

    def __init__(self, token: str, phrase: str, forward_delay_s: float) -> None:
        self.token = token
        self.phrase = phrase
        self.forward_delay_s = forward_delay_s
        self.open_received: Optional[dict] = None
        self.ack_received: Optional[dict] = None
        self._server: Optional[websockets.Server] = None
        self._port: int = 0
        self._ack_event = threading.Event()

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
                        "expires_at": "2026-04-30T00:00:00Z",
                    }
                )
            )

            # Hold before pushing forward. In rc.10–rc.12 the gateway's
            # tool loop would have closed by this point; waiter destroyed
            # mid-recv and ack never goes out.
            await asyncio.sleep(self.forward_delay_s)

            kp_device = generate_gateway_keypair()
            gateway_pubkey = self.open_received["gateway_pubkey"]
            nonce_b64, ct_b64 = encrypt_pairing_payload(
                sk_local_b64=kp_device.sk_b64,
                pk_remote_b64=gateway_pubkey,
                sid=self.token,
                plaintext=self.phrase.encode("utf-8"),
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

            try:
                raw2 = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw2)
                if msg.get("type") == "ack":
                    self.ack_received = msg
                    self._ack_event.set()
            except asyncio.TimeoutError:
                pass

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def wait_for_ack_async(self, timeout_s: float) -> bool:
        """Async wait — keeps the pytest loop running so the server
        handler coroutine can progress.

        ``_ack_event`` is threading.Event (set from the server handler
        coroutine), so we poll it with ``asyncio.sleep`` yields. A
        purely sync ``wait()`` call would block the pytest loop and
        the handler couldn't read the ack frame at all.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._ack_event.is_set():
                return True
            await asyncio.sleep(0.02)
        return False

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.mark.asyncio
async def test_relay_pair_survives_tool_loop_teardown(monkeypatch):
    """End-to-end: the tool returns {url, pin} fast, the browser
    uploads the phrase after a delay, and the worker thread still acks
    the relay + writes credentials.

    Regression shield for the rc.10–rc.12 bug where Hermes's tool-
    invocation loop closed mid-``ws.recv`` and the ack was never sent.
    """
    # Real relay-stub with a 400ms delay between opened + forward — long
    # enough to exercise the lifecycle bug if it regressed.
    relay = _DelayedRelayStub(
        token="rc13-lifecycle-tok",
        phrase=TEST_PHRASE,
        forward_delay_s=0.4,
    )
    await relay.start()

    # Point the remote_client at the stub.
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)

    # Side-effect bucket so we can inspect what the worker thread did.
    captured_phrases: List[str] = []
    configure_called = threading.Event()

    class FakeState:
        """Plugin-state double — ``_spawn_relay_completion_task`` closes
        over this and invokes ``.configure()`` when the relay forwards a
        valid ciphertext."""

        def configure(self, phrase: str) -> None:
            captured_phrases.append(phrase)
            configure_called.set()

        def get_client(self):
            mock = MagicMock()
            mock._eoa_address = "0xdeadbeef0123"
            return mock

    state = FakeState()

    # Exercise the actual code path used by the tool body
    # (``_pair_relay`` → ``asyncio.to_thread(_run_relay_pair_on_thread,
    # ...)``). Using the exact helper covers both the handshake path
    # AND the background-completion path in one go.
    from totalreclaw.hermes.pair_tool import _run_relay_pair_on_thread

    t_start = time.monotonic()
    opened = await asyncio.to_thread(
        _run_relay_pair_on_thread, state, "either"
    )
    t_handshake = time.monotonic() - t_start

    # Handshake should complete in well under 1s (local loopback).
    # The forward delay is 400ms AFTER opened — if we accidentally wait
    # for forward here, t_handshake would be >400ms.
    assert t_handshake < 1.0, (
        f"handshake took {t_handshake:.2f}s — did we accidentally "
        "block on the forward frame instead of returning fast?"
    )

    # Sanity on the opened metadata.
    assert opened.url.startswith("http://")
    assert relay.token in opened.url
    assert opened.pin and len(opened.pin) == 6
    assert opened.expires_at

    # Wait for the worker thread to drive the whole pairing.
    # 5s budget — the forward delay is 400ms, decrypt + ack is <50ms
    # in practice, so 5s is slack for slow CI machines.
    ack_arrived = await relay.wait_for_ack_async(timeout_s=5.0)

    try:
        await relay.stop()
    except Exception:
        pass

    # Hard assertions on the rc.13 fix.
    assert ack_arrived, (
        "relay never received ack — the worker thread failed to "
        "complete the pair session (lifecycle regression)"
    )
    assert relay.ack_received is not None
    assert relay.ack_received.get("type") == "ack"

    # The worker thread invoked the plugin-state configure callback with
    # the decrypted phrase — proves decrypt succeeded end-to-end.
    assert configure_called.wait(timeout=2.0), (
        "state.configure was never called — decrypt or credential-write "
        "path broken"
    )
    assert captured_phrases == [TEST_PHRASE]


@pytest.mark.asyncio
async def test_relay_pair_returns_before_forward(monkeypatch):
    """The tool body MUST return ``{url, pin, expires_at}`` before the
    browser completes its side.

    If the fix accidentally waits for the forward frame before
    returning, the tool would block the agent chat for up to the 5-min
    TTL — the exact UX regression Option 2 was chosen to avoid.
    """
    relay = _DelayedRelayStub(
        token="rc13-return-fast-tok",
        phrase=TEST_PHRASE,
        forward_delay_s=5.0,  # deliberately long — would block if buggy
    )
    await relay.start()
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)

    class NoopState:
        def configure(self, phrase: str) -> None:  # pragma: no cover
            pass

        def get_client(self):  # pragma: no cover
            return MagicMock(_eoa_address="0x0")

    try:
        from totalreclaw.hermes.pair_tool import _run_relay_pair_on_thread

        t_start = time.monotonic()
        opened = await asyncio.to_thread(
            _run_relay_pair_on_thread, NoopState(), "either"
        )
        t_elapsed = time.monotonic() - t_start

        # The handshake must return in well under the forward delay (5s).
        # 1s is generous — in practice it's <100ms on loopback.
        assert t_elapsed < 1.0, (
            f"_run_relay_pair_on_thread blocked for {t_elapsed:.2f}s "
            "— it must return after ``opened``, not after ``forward``"
        )
        assert opened.pin
        assert opened.url
    finally:
        try:
            await relay.stop()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_relay_pair_surfaces_open_failure(monkeypatch):
    """If the relay rejects the ``open`` with ``{type:error}``, the
    handshake helper must propagate the error to the tool body so it
    can report a sane message to the agent (not a 15s timeout).
    """

    async def handler(ws):
        _ = await ws.recv()  # consume the open frame
        await ws.send(json.dumps({"type": "error", "error": "rate_limited"}))
        await ws.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", f"ws://127.0.0.1:{port}")

        class NoopState:
            def configure(self, phrase: str) -> None:  # pragma: no cover
                pass

            def get_client(self):  # pragma: no cover
                return MagicMock(_eoa_address="0x0")

        from totalreclaw.hermes.pair_tool import _run_relay_pair_on_thread

        with pytest.raises(RuntimeError, match="rate_limited"):
            await asyncio.to_thread(
                _run_relay_pair_on_thread, NoopState(), "either"
            )
    finally:
        server.close()
        await server.wait_closed()
