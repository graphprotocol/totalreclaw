"""F1 (rc.24) — sidecar-handoff regression test.

Background — rc.23 ship-stopper #157
------------------------------------
``hermes chat -q`` is one-shot: each turn runs in a fresh Python
process that exits as soon as the agent reply lands. rc.13 ran the
relay-pair WebSocket lifecycle on a daemon thread INSIDE that process,
so when the process exited the WS died with it; the relay tore the
session down and returned 404/502 for the eventual phrase POST.

rc.24 fix: spawn a fully-detached sidecar SUBPROCESS (POSIX
``setsid``) that owns the WS through completion. The sidecar survives
parent exit because it's been reparented to ``init`` / ``launchd``.

Tests in this module
--------------------
1. ``test_sidecar_handshake_reports_relay_metadata`` — exercises the
   sidecar logic in-process (``run_sidecar_inline``) against a real
   loopback websockets stub. Asserts the handshake file appears,
   carries the relay-supplied URL/PIN/expires_at, and the
   ``await_phrase_upload`` path is reachable. This proves the
   parent-side polling contract works.

2. ``test_pair_tool_returns_after_sidecar_handshake_with_short_lived_process``
   — the lifecycle test: spawns an ACTUAL detached sidecar, immediately
   simulates parent exit (the test's "parent" cleans up its handle to
   the subprocess but does not wait), and asserts the sidecar still
   completes the pair against a long-running relay stub. This is the
   exact scenario rc.13 failed at.

3. ``test_pair_tool_uses_sidecar_by_default`` — guards that
   ``TOTALRECLAW_PAIR_SIDECAR`` is on by default. Setting it to "0"
   re-exposes the rc.23 ship-stopper.

The sidecar subprocess is spawned via ``python -m
totalreclaw.pair.completion_sidecar``; tests use the live
``sys.executable`` so the same venv is in scope.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# Reusable relay stub — loopback WebSocket server that impersonates the
# real relay's open/forward/ack flow.
# ---------------------------------------------------------------------------


class _RelayStub:
    """Stand-in for the real relay. Logs every frame for assertions."""

    def __init__(self, *, token: str, phrase: str, forward_delay_s: float) -> None:
        self.token = token
        self.phrase = phrase
        self.forward_delay_s = forward_delay_s
        self.open_frame: Optional[dict] = None
        self.ack_frame: Optional[dict] = None
        self._server: Optional[websockets.Server] = None
        self._port: int = 0
        self._ack_event = asyncio.Event()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def start(self) -> None:
        async def handler(ws):
            raw = await ws.recv()
            self.open_frame = json.loads(raw)
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
            await asyncio.sleep(self.forward_delay_s)

            kp_device = generate_gateway_keypair()
            gateway_pubkey = self.open_frame["gateway_pubkey"]
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
                    self.ack_frame = msg
                    self._ack_event.set()
            except asyncio.TimeoutError:
                pass

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def wait_for_ack(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidecar_handshake_reports_relay_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process exercise of the sidecar logic — asserts the
    handshake file is produced and carries the relay-supplied
    metadata.

    Drives ``run_sidecar_inline`` (no fork) against a real loopback
    relay stub so we can synchronously prove the contract:

    1. After the relay returns ``opened``, the sidecar writes a JSON
       file at ``~/.totalreclaw/.pair_handshake_<id>.json``.
    2. The file carries ``{url, pin, expires_at, token, status:"opened"}``.
    3. ``await_phrase_upload`` then runs to completion and the relay
       receives ack.

    The credentials-write path is NOT exercised here — that requires
    ``state.configure`` which talks to the on-chain RPC. A separate
    test (``test_credentials_path``) covers that surface.
    """
    relay = _RelayStub(token="rc24-sidecar-tok", phrase=TEST_PHRASE, forward_delay_s=0.1)
    await relay.start()

    # Redirect ``~/.totalreclaw`` to a temp dir so we don't pollute the
    # developer's actual home dir, and so the handshake file shows up
    # at a path the test can read.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)

    # Stub the credentials-writing path. ``AgentState.configure`` is
    # what writes ``credentials.json`` + creates a ``TotalReclaw``
    # client; we don't want that here. Patch it inline.
    import totalreclaw.agent.state as state_module

    captured: dict[str, str] = {}

    def fake_configure(self, mnemonic: str) -> None:
        captured["mnemonic"] = mnemonic

    def fake_get_client(self):
        class _C:
            _eoa_address = "0xdeadbeef0123"

        return _C()

    monkeypatch.setattr(state_module.AgentState, "configure", fake_configure)
    monkeypatch.setattr(state_module.AgentState, "get_client", fake_get_client)

    # Run sidecar logic in-process (no fork) so we can inspect outcomes
    # synchronously.
    from totalreclaw.pair.completion_sidecar import (
        _handshake_path_for,
        run_sidecar_inline,
    )

    handshake_id = "rc24-test-handshake"

    async def _run() -> None:
        # Run as an asyncio task so the relay handler can progress
        # concurrently. ``run_sidecar_inline`` calls asyncio.run()
        # internally — wrap in to_thread so we don't nest event loops.
        await asyncio.to_thread(
            run_sidecar_inline,
            handshake_id=handshake_id,
            mode="either",
            relay_url=relay.url,
        )

    sidecar_task = asyncio.create_task(_run())

    # Poll for the handshake file. Should appear within ~1s on loopback.
    handshake_path = _handshake_path_for(handshake_id)
    deadline = time.monotonic() + 5.0
    handshake: Optional[dict] = None
    while time.monotonic() < deadline:
        if handshake_path.exists():
            try:
                handshake = json.loads(handshake_path.read_text())
                break
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.05)

    assert handshake is not None, (
        f"sidecar never wrote the handshake file at {handshake_path} — "
        "the parent's polling contract would have timed out"
    )
    assert handshake["status"] == "opened", handshake
    assert handshake["token"] == relay.token
    assert handshake["pin"] and len(handshake["pin"]) == 6
    assert handshake["expires_at"]
    assert relay.token in handshake["url"]

    # Wait for the sidecar (with the rest of the pair flow) to finish.
    # Our relay stub sends the forward frame ~0.1s after opened.
    ack_arrived = await relay.wait_for_ack(timeout_s=10.0)
    await sidecar_task

    try:
        await relay.stop()
    except Exception:
        pass

    assert ack_arrived, (
        "relay never received ack — the sidecar failed to drive the "
        "pair to completion (lifecycle regression)"
    )
    assert relay.ack_frame == {"type": "ack"}
    # Phrase reached the credentials path.
    assert captured.get("mnemonic") == TEST_PHRASE


@pytest.mark.asyncio
async def test_pair_tool_returns_after_sidecar_handshake_with_short_lived_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: launch a REAL detached subprocess sidecar, wait
    only for the handshake, then simulate parent exit (drop our
    subprocess handle without waiting). The sidecar must still
    complete the pair flow.

    This is the rc.23 NO-GO scenario — it would have failed under
    rc.13 because the daemon thread died with the parent. With the
    rc.24 sidecar fix, ``setsid`` keeps the subprocess alive past
    parent exit, so the relay still gets the ack.
    """
    relay = _RelayStub(token="rc24-detach-tok", phrase=TEST_PHRASE, forward_delay_s=0.5)
    await relay.start()

    # Fake HOME so the sidecar's handshake file lands somewhere
    # readable + cleanable by this test.
    fake_home = tmp_path
    fake_home.mkdir(parents=True, exist_ok=True)

    handshake_id = "rc24-detach-handshake"

    # Build the env the sidecar will run under. We can't monkeypatch
    # process env across a real subprocess fork — pass everything via
    # ``Popen(env=...)``.
    # NB: env-var names match completion_sidecar's _ENV_* contract.
    child_env = os.environ.copy()
    child_env["HOME"] = str(fake_home)
    child_env["TR_PAIR_SIDECAR_HANDSHAKE_ID"] = handshake_id
    child_env["TR_PAIR_SIDECAR_RELAY_URL"] = relay.url
    child_env["TR_PAIR_SIDECAR_MODE"] = "either"
    # Block the on-chain client construction in the sidecar's
    # ``configure`` path. The sidecar imports ``AgentState`` from
    # ``totalreclaw.agent.state``; the easiest stub is to point its
    # server URL at a non-routable local port so credentials-write
    # would fail soft. The flow we care about (ws ack) does NOT
    # depend on configure success.
    child_env["TOTALRECLAW_SERVER_URL"] = "http://127.0.0.1:1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "totalreclaw.pair.completion_sidecar", "--run"],
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )

    # Poll for the handshake file like the real parent does.
    handshake_path = fake_home / ".totalreclaw" / f".pair_handshake_{handshake_id}.json"
    deadline = time.monotonic() + 10.0
    handshake: Optional[dict] = None
    while time.monotonic() < deadline:
        if handshake_path.exists():
            try:
                handshake = json.loads(handshake_path.read_text())
                break
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.05)

    assert handshake is not None, (
        "subprocess sidecar never produced the handshake file — "
        "the rc.24 contract is broken"
    )
    assert handshake["status"] == "opened"
    assert handshake["token"] == relay.token

    # Simulate parent exit by dropping the proc handle. Don't wait()
    # — the sidecar must NOT depend on the parent reaping it. POSIX
    # ``setsid`` reparents the orphan to init, so this is fine.
    del proc

    # The relay stub still expects the forward to land + an ack to
    # come back. If the sidecar died at parent-exit, the relay will
    # never get the ack and ``wait_for_ack`` times out.
    ack_arrived = await relay.wait_for_ack(timeout_s=15.0)
    try:
        await relay.stop()
    except Exception:
        pass

    assert ack_arrived, (
        "relay never received ack after parent process handle dropped — "
        "this is the EXACT rc.23 NO-GO scenario; the sidecar must "
        "outlive the parent for one-shot ``hermes chat -q`` to work"
    )


def test_pair_tool_uses_sidecar_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rc.24 default MUST be sidecar=on. Setting the env var to
    "0" disables it (escape hatch for sandboxed envs); anything else
    enables it.

    Regression shield: a future PR that flips the default off would
    silently re-introduce the rc.23 bug for one-shot processes.
    """
    from totalreclaw.hermes.pair_tool import _sidecar_enabled

    monkeypatch.delenv("TOTALRECLAW_PAIR_SIDECAR", raising=False)
    assert _sidecar_enabled() is True

    monkeypatch.setenv("TOTALRECLAW_PAIR_SIDECAR", "1")
    assert _sidecar_enabled() is True

    monkeypatch.setenv("TOTALRECLAW_PAIR_SIDECAR", "true")
    assert _sidecar_enabled() is True

    monkeypatch.setenv("TOTALRECLAW_PAIR_SIDECAR", "0")
    assert _sidecar_enabled() is False

    monkeypatch.setenv("TOTALRECLAW_PAIR_SIDECAR", "false")
    assert _sidecar_enabled() is False


def test_handshake_path_is_under_totalreclaw_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Belt-and-suspenders: handshake files live in
    ``~/.totalreclaw/`` (mode 0700), not ``/tmp`` (world-readable).

    The handshake JSON does NOT contain key material — only the
    public-facing URL/PIN — but keeping it in the credentials dir
    matches the rest of the pair flow's filesystem contract and
    avoids any accidental race with another user on a multi-tenant
    host.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    from totalreclaw.pair.completion_sidecar import _handshake_path_for

    p = _handshake_path_for("test-id")
    assert str(p).startswith(str(tmp_path / ".totalreclaw"))
    # Trigger directory creation by calling the helper.
    from totalreclaw.pair.completion_sidecar import _totalreclaw_dir

    d = _totalreclaw_dir()
    # Mode 0o700 enforced.
    mode = d.stat().st_mode & 0o777
    assert mode == 0o700, f"~/.totalreclaw must be 0700, got {oct(mode)}"
