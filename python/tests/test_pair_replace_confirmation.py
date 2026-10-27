"""Replace-mode pairing and post-pairing relay registration.

Replace mode: replacing the account already on an agent is confirmed only by
the user, on the relay's pairing page. The gateway opens the session with
``replace: true``; the relay echoes ``replace: true`` in ``opened`` (an
older relay does not, and the gateway aborts) and forwards
``replace_confirmed: true`` only after the user ticked the page's box. The
gateway refuses any replace-mode upload without that confirmation, before
decrypting it.

Registration: a freshly paired vault is registered with the relay while its
phrase is on the host, because a later migration to a derived bundle can no
longer register it.

Loopback WebSocket relay stub only; no network. Phrases used here are the
public BIP-39 test vector and are never printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Optional

import pytest
import websockets

from totalreclaw.pair.crypto import encrypt_pairing_payload, generate_gateway_keypair
from totalreclaw.pair.remote_client import (
    ReplaceNotSupportedError,
    await_phrase_upload,
    open_remote_pair_session,
)

TEST_PHRASE = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


class _RelayStub:
    """Loopback relay: answers ``open``, optionally pushes one ``forward``."""

    def __init__(
        self,
        *,
        token: str = "tok-replace-0001",
        echo_replace: bool = True,
        forward: bool = True,
        replace_confirmed: Optional[bool] = None,
    ) -> None:
        self.token = token
        self.echo_replace = echo_replace
        self.forward = forward
        self.replace_confirmed = replace_confirmed
        self.open_frame: Optional[dict] = None
        self.reply: Optional[dict] = None
        self._server = None
        self._port = 0
        self.done = asyncio.Event()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def start(self) -> None:
        async def handler(ws):
            self.open_frame = json.loads(await ws.recv())
            opened = {
                "type": "opened",
                "token": self.token,
                "short_url": f"/pair/p/{self.token}",
                "expires_at": "2026-10-27T23:59:59Z",
            }
            if self.echo_replace and self.open_frame.get("replace") is True:
                opened["replace"] = True
            await ws.send(json.dumps(opened))
            if not self.forward:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=2)
                except Exception:
                    pass
                self.done.set()
                return
            await asyncio.sleep(0.05)
            device = generate_gateway_keypair()
            nonce_b64, ct_b64 = encrypt_pairing_payload(
                sk_local_b64=device.sk_b64,
                pk_remote_b64=self.open_frame["gateway_pubkey"],
                sid=self.token,
                plaintext=TEST_PHRASE.encode("utf-8"),
            )
            frame = {
                "type": "forward",
                "client_pubkey": device.pk_b64,
                "nonce": nonce_b64,
                "ciphertext": ct_b64,
                "mode": "generate",
            }
            if self.replace_confirmed is not None:
                frame["replace_confirmed"] = self.replace_confirmed
            await ws.send(json.dumps(frame))
            try:
                self.reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            except Exception:
                pass
            self.done.set()

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()


# ---------------------------------------------------------------------------
# remote_client wire contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_session_sends_replace_and_requires_relay_echo():
    relay = _RelayStub(forward=False)
    await relay.start()
    try:
        session = await open_remote_pair_session(relay_base_url=relay.url, replace=True)
        assert relay.open_frame["replace"] is True
        assert session.replace is True
        await session._ws.close()
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_relay_without_replace_support_aborts_the_session():
    relay = _RelayStub(echo_replace=False, forward=False)
    await relay.start()
    try:
        with pytest.raises(ReplaceNotSupportedError):
            await open_remote_pair_session(relay_base_url=relay.url, replace=True)
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_normal_session_never_sends_replace():
    relay = _RelayStub(forward=False)
    await relay.start()
    try:
        session = await open_remote_pair_session(relay_base_url=relay.url)
        assert "replace" not in relay.open_frame
        assert session.replace is False
        await session._ws.close()
    finally:
        await relay.stop()


@pytest.mark.parametrize("confirmed", [None, False])
@pytest.mark.asyncio
async def test_unconfirmed_replace_upload_is_nacked_and_never_completed(confirmed):
    relay = _RelayStub(replace_confirmed=confirmed)
    await relay.start()
    calls = []

    async def complete(phrase: str) -> dict:
        calls.append(1)
        return {"state": "active"}

    try:
        session = await open_remote_pair_session(relay_base_url=relay.url, replace=True)
        with pytest.raises(RuntimeError, match="not confirmed"):
            await await_phrase_upload(session, complete_pairing=complete)
        await asyncio.wait_for(relay.done.wait(), timeout=5)
    finally:
        await relay.stop()

    assert calls == []
    assert relay.reply == {"type": "nack", "error": "replace_not_confirmed"}


@pytest.mark.asyncio
async def test_confirmed_replace_upload_completes():
    relay = _RelayStub(replace_confirmed=True)
    await relay.start()
    received = []

    async def complete(phrase: str) -> dict:
        received.append(phrase == TEST_PHRASE)
        return {"state": "active"}

    try:
        session = await open_remote_pair_session(relay_base_url=relay.url, replace=True)
        await await_phrase_upload(session, complete_pairing=complete)
        await asyncio.wait_for(relay.done.wait(), timeout=5)
    finally:
        await relay.stop()

    assert received == [True]
    assert relay.reply == {"type": "ack"}


@pytest.mark.asyncio
async def test_normal_session_ignores_replace_confirmed():
    """A normal session neither needs nor reacts to the confirmation field."""
    relay = _RelayStub()
    await relay.start()

    async def complete(phrase: str) -> dict:
        return {"state": "active"}

    try:
        session = await open_remote_pair_session(relay_base_url=relay.url)
        await await_phrase_upload(session, complete_pairing=complete)
        await asyncio.wait_for(relay.done.wait(), timeout=5)
    finally:
        await relay.stop()
    assert relay.reply == {"type": "ack"}


# ---------------------------------------------------------------------------
# Sidecar: replace mode end to end, and post-ack registration
# ---------------------------------------------------------------------------


class _FakeClient:
    eoa_address = "0x00000000000000000000000000000000000000e0"

    def __init__(self, log: list):
        self._log = log

    async def register(self) -> str:
        self._log.append("register")
        return "user-1"

    async def close(self) -> None:
        self._log.append("close")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("TOTALRECLAW_CREDENTIALS_PATH", raising=False)
    return h


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch):
    """AgentState that records configure() and hands out a fake client."""
    import totalreclaw.agent.state as state_module

    log: list = []

    def fake_configure(self, mnemonic: str) -> None:
        log.append("configure" if mnemonic == TEST_PHRASE else "configure-wrong")
        creds = Path(os.environ["HOME"]) / ".totalreclaw" / "credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text('{"new": true}')

    monkeypatch.setattr(state_module.AgentState, "configure", fake_configure)
    monkeypatch.setattr(
        state_module.AgentState, "get_client", lambda self: _FakeClient(log)
    )
    return log


def _run_sidecar(relay_url: str, *, replace: bool, handshake_id: str) -> None:
    from totalreclaw.pair.completion_sidecar import run_sidecar_inline

    run_sidecar_inline(
        handshake_id=handshake_id, mode="generate", relay_url=relay_url, replace=replace
    )


def _write_existing_creds(home: Path) -> Path:
    d = home / ".totalreclaw"
    d.mkdir(parents=True, exist_ok=True)
    creds = d / "credentials.json"
    creds.write_text('{"old": "account"}')
    (d / "credentials.json.bak").write_text('{"old": "backup"}')
    return creds


@pytest.mark.asyncio
async def test_sidecar_registers_the_new_vault_after_the_ack(home, fake_state):
    relay = _RelayStub()
    await relay.start()
    try:
        await asyncio.to_thread(
            _run_sidecar, relay.url, replace=False, handshake_id="reg-after-ack"
        )
    finally:
        await relay.stop()

    assert relay.reply == {"type": "ack"}
    assert fake_state == ["configure", "register", "close"]


@pytest.mark.asyncio
async def test_sidecar_does_not_register_when_pairing_failed(home, monkeypatch):
    import totalreclaw.agent.state as state_module

    calls = []

    def failing_configure(self, mnemonic: str) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(state_module.AgentState, "configure", failing_configure)
    monkeypatch.setattr(
        state_module.AgentState, "get_client", lambda self: _FakeClient(calls)
    )
    relay = _RelayStub()
    await relay.start()
    try:
        await asyncio.to_thread(
            _run_sidecar, relay.url, replace=False, handshake_id="no-reg-on-fail"
        )
    finally:
        await relay.stop()
    assert calls == []


@pytest.mark.asyncio
async def test_sidecar_confirmed_replace_keeps_the_old_credentials(home, fake_state):
    creds = _write_existing_creds(home)
    relay = _RelayStub(replace_confirmed=True)
    await relay.start()
    try:
        await asyncio.to_thread(
            _run_sidecar, relay.url, replace=True, handshake_id="replace-ok"
        )
    finally:
        await relay.stop()

    assert relay.open_frame["replace"] is True
    assert relay.reply == {"type": "ack"}
    assert fake_state[:2] == ["configure", "register"]
    assert json.loads(creds.read_text()) == {"new": True}
    kept = sorted(p.name for p in creds.parent.iterdir() if ".replaced-" in p.name)
    assert len(kept) == 2
    old_creds = next(creds.parent.glob("credentials.json.replaced-*"))
    old_bak = next(creds.parent.glob("credentials.json.bak.replaced-*"))
    assert json.loads(old_creds.read_text()) == {"old": "account"}
    assert json.loads(old_bak.read_text()) == {"old": "backup"}
    assert stat.S_IMODE(old_creds.stat().st_mode) == 0o600
    assert not (creds.parent / "credentials.json.bak").exists()


@pytest.mark.asyncio
async def test_sidecar_unconfirmed_replace_changes_nothing(home, fake_state):
    creds = _write_existing_creds(home)
    before = sorted(p.name for p in creds.parent.iterdir())
    relay = _RelayStub(replace_confirmed=None)
    await relay.start()
    try:
        await asyncio.to_thread(
            _run_sidecar, relay.url, replace=True, handshake_id="replace-unconfirmed"
        )
    finally:
        await relay.stop()

    assert relay.reply == {"type": "nack", "error": "replace_not_confirmed"}
    assert fake_state == []
    assert json.loads(creds.read_text()) == {"old": "account"}
    after = sorted(
        p.name for p in creds.parent.iterdir() if not p.name.startswith(".pair_")
    )
    assert after == before


@pytest.mark.asyncio
async def test_sidecar_reports_relay_without_replace_support(home, fake_state):
    from totalreclaw.pair.completion_sidecar import _handshake_path_for

    relay = _RelayStub(echo_replace=False, forward=False)
    await relay.start()
    try:
        await asyncio.to_thread(
            _run_sidecar, relay.url, replace=True, handshake_id="replace-old-relay"
        )
    finally:
        await relay.stop()

    record = json.loads(_handshake_path_for("replace-old-relay").read_text())
    assert record["status"] == "error"
    assert record["error_kind"] == "replace_not_supported"
    assert fake_state == []


def test_parent_raises_typed_error_for_replace_not_supported(home, monkeypatch):
    """spawn_completion_sidecar turns the sidecar's error_kind back into
    ReplaceNotSupportedError, and passes the replace flag via env only."""
    import subprocess

    from totalreclaw.pair import completion_sidecar as cs

    spawned_env = {}

    def fake_popen(cmd, **kwargs):
        spawned_env.update(kwargs["env"])
        handshake_id = kwargs["env"][cs._ENV_HANDSHAKE_ID]
        cs._handshake_path_for(handshake_id).write_text(
            json.dumps(
                {
                    "url": "",
                    "pin": "",
                    "expires_at": "",
                    "token": "",
                    "status": "error",
                    "error_message": "no replace mode",
                    "error_kind": "replace_not_supported",
                }
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(ReplaceNotSupportedError):
        cs.spawn_completion_sidecar(mode="generate", replace=True, handshake_timeout_s=2)
    assert spawned_env[cs._ENV_REPLACE] == "1"

    # A stale value in the parent's environment must not turn a normal
    # session into a replace-mode one.
    monkeypatch.setenv(cs._ENV_REPLACE, "1")
    spawned_env.clear()
    with pytest.raises(RuntimeError):
        cs.spawn_completion_sidecar(mode="generate", replace=False, handshake_timeout_s=2)
    assert cs._ENV_REPLACE not in spawned_env


# ---------------------------------------------------------------------------
# preserve_credentials_before_replace
# ---------------------------------------------------------------------------


def test_preserve_is_a_noop_without_credentials(home):
    from totalreclaw.hermes.auto_migrate import preserve_credentials_before_replace

    preserve_credentials_before_replace()
    d = home / ".totalreclaw"
    assert not d.exists() or not any(".replaced-" in p.name for p in d.iterdir())


def test_preserve_failure_raises_so_pairing_can_abort(home, monkeypatch):
    from totalreclaw.hermes import auto_migrate

    _write_existing_creds(home)

    def fail_open(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(auto_migrate.os, "open", fail_open)
    with pytest.raises(OSError):
        auto_migrate.preserve_credentials_before_replace()


# ---------------------------------------------------------------------------
# pair.registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_vault_success_and_failure(caplog):
    import httpx

    from totalreclaw.pair.registration import register_vault

    class _Ok:
        async def register(self):
            return "user-1"

    class _Http503:
        async def register(self):
            request = httpx.Request("POST", "https://relay.invalid/v1/register")
            raise httpx.HTTPStatusError(
                "secret-looking message", request=request,
                response=httpx.Response(503, request=request),
            )

    class _Slow:
        async def register(self):
            await asyncio.sleep(5)

    assert await register_vault(_Ok()) is True
    with caplog.at_level("WARNING", logger="totalreclaw.pair.registration"):
        assert await register_vault(_Http503()) is False
        assert await register_vault(_Slow(), timeout_s=0.05) is False
    assert "HTTP 503" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "secret-looking message" not in caplog.text


def test_register_vault_sync_runs_without_a_loop():
    from totalreclaw.pair.registration import register_vault_sync

    class _Ok:
        async def register(self):
            return "user-1"

    assert register_vault_sync(_Ok()) is True


# ---------------------------------------------------------------------------
# Tool level, real transport: a relay without replace mode (e.g. one that
# predates it) must make the tool refuse, with no fallback to any
# agent-confirmed path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sidecar", ["0", "1"])
@pytest.mark.asyncio
async def test_tool_fails_closed_against_a_relay_without_replace_mode(
    home, monkeypatch, sidecar
):
    from totalreclaw.hermes import pair_tool
    from totalreclaw.hermes.state import PluginState

    creds = _write_existing_creds(home)
    before = creds.read_bytes()
    relay = _RelayStub(echo_replace=False, forward=False)
    await relay.start()
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)
    monkeypatch.setenv("TOTALRECLAW_PAIR_SIDECAR", sidecar)
    monkeypatch.delenv("TOTALRECLAW_PAIR_MODE", raising=False)
    state = PluginState()
    monkeypatch.setattr(state, "is_configured", lambda: True)
    try:
        for args in (
            {"mode": "generate", "replace": True},
            # Old-style self-confirmation must not open anything either.
            {"mode": "generate", "replace_confirmed": True, "replace_token": "x"},
        ):
            res = json.loads(await pair_tool.pair(args, state))
            assert "url" not in res and "pin" not in res, args
            assert res.get("already_configured") is True
            if args.get("replace"):
                assert "relay needs updating" in res["error"]
    finally:
        await relay.stop()

    assert relay.open_frame is not None and relay.open_frame["replace"] is True
    assert creds.read_bytes() == before
    assert not any(".replaced-" in p.name for p in creds.parent.iterdir())
