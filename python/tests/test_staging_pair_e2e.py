"""Staging end-to-end: pairing a brand-new account, then using it.

Opt-in, never in CI: set ``TOTALRECLAW_STAGING_E2E=1``. Refuses to run
unless ``TOTALRECLAW_SERVER_URL`` points at the staging relay.

    TOTALRECLAW_STAGING_E2E=1 \\
    TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz \\
        python -m pytest tests/test_staging_pair_e2e.py -v

``test_fresh_generate_then_write_and_read`` is the regression for a fresh
"Generate new" pairing that the next plugin load migrated to a derived
bundle before the vault was ever registered, so every relay call returned
401 for good. It drives the real flow against the staging relay: the
completion sidecar opens a pair session, this test plays the browser (a
freshly generated phrase, encrypted to the gateway key from the pair URL,
POSTed with the PIN), then the migration runs and the bundle-configured
client writes a fact and reads it back.

``test_replace_needs_the_users_confirmation`` checks the replace-mode
contract with the staging relay: an upload without the page's confirmation
is refused and changes nothing; a confirmed one replaces the account and
keeps the previous credentials.

Phrase safety: the phrases are generated in-process, held in a wrapper
whose repr is redacted, and never printed or logged.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

STAGING_URL = "https://api-staging.totalreclaw.xyz"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("TOTALRECLAW_STAGING_E2E") != "1",
        reason="staging E2E is opt-in: set TOTALRECLAW_STAGING_E2E=1",
    ),
    pytest.mark.skipif(
        os.environ.get("TOTALRECLAW_SERVER_URL", "").rstrip("/") != STAGING_URL,
        reason=f"refusing to run unless TOTALRECLAW_SERVER_URL={STAGING_URL}",
    ),
]


class _Secret:
    """Holds a phrase without ever rendering it (repr/str are redacted)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def _new_phrase() -> _Secret:
    from mnemonic import Mnemonic

    return _Secret(Mnemonic("english").generate(strength=128))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", STAGING_URL)
    # Mark every relay call as test traffic (also exempts it from staging's
    # per-IP registration limit, which back-to-back runs exhaust).
    monkeypatch.setenv("TOTALRECLAW_TEST", "true")
    for var in (
        "TOTALRECLAW_PAIR_RELAY_URL",
        "TOTALRECLAW_RELAY_URL",
        "TOTALRECLAW_CREDENTIALS_PATH",
        "TOTALRECLAW_RECOVERY_PHRASE",
        "TOTALRECLAW_NO_AUTO_MIGRATE",
    ):
        monkeypatch.delenv(var, raising=False)
    return h


class _Sidecar:
    """Runs the real completion sidecar in-process on a thread."""

    def __init__(self, *, replace: bool = False) -> None:
        self.handshake_id = f"e2e-{uuid.uuid4().hex[:12]}"
        self.replace = replace
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        from totalreclaw.pair.completion_sidecar import run_sidecar_inline

        run_sidecar_inline(
            handshake_id=self.handshake_id,
            mode="generate",
            relay_url=None,
            replace=self.replace,
        )

    def start(self) -> dict:
        from totalreclaw.pair.completion_sidecar import _handshake_path_for

        self._thread.start()
        path = _handshake_path_for(self.handshake_id)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.1)
        raise AssertionError("sidecar never opened a relay session")

    def join(self, timeout: float = 60) -> None:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "sidecar did not finish"


def _respond(handshake: dict, phrase: _Secret, *, replace_confirmed=None):
    """Do what the pairing page does: encrypt and POST to /respond."""
    import httpx

    from totalreclaw.pair.crypto import encrypt_pairing_payload, generate_gateway_keypair

    url = handshake["url"]
    gateway_pk = urlparse(url).fragment.split("pk=", 1)[1]
    device = generate_gateway_keypair()
    nonce_b64, ct_b64 = encrypt_pairing_payload(
        sk_local_b64=device.sk_b64,
        pk_remote_b64=gateway_pk,
        sid=handshake["token"],
        plaintext=phrase.reveal().encode("utf-8"),
    )
    body = {
        "v": 1,
        "client_pubkey": device.pk_b64,
        "pin": handshake["pin"],
        "nonce": nonce_b64,
        "ciphertext": ct_b64,
        "mode": "generate",
    }
    if replace_confirmed is not None:
        body["replace_confirmed"] = replace_confirmed
    return httpx.post(
        f"{STAGING_URL}/pair/p/{handshake['token']}/respond", json=body, timeout=30
    )


def _pair_fresh_account(home: Path) -> None:
    sidecar = _Sidecar()
    handshake = sidecar.start()
    assert handshake["status"] == "opened"
    res = _respond(handshake, _new_phrase())
    assert res.status_code == 204, res.text
    sidecar.join()


async def _write_then_read(client) -> None:
    marker = f"e2e-{uuid.uuid4().hex[:10]}"
    await client.remember(
        f"The staging pairing check {marker} prefers green tea", importance=0.8
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        await asyncio.sleep(10)
        results = await client.recall(f"staging pairing check {marker}")
        if any(marker in r.text for r in results):
            return
    raise AssertionError("the written fact never came back from recall")


def test_fresh_generate_then_write_and_read(home: Path) -> None:
    from totalreclaw.agent.state import AgentState
    from totalreclaw.hermes import auto_migrate

    _pair_fresh_account(home)

    creds_path = home / ".totalreclaw" / "credentials.json"
    assert creds_path.exists()
    assert "version" not in json.loads(creds_path.read_text())  # phrase-mode file

    # The next plugin load: migrate to a derived bundle.
    assert auto_migrate.maybe_migrate() is True
    assert json.loads(creds_path.read_text())["version"] == 2

    state = AgentState()
    client = state.get_client()
    assert client is not None and client._mnemonic is None  # bundle mode

    async def _use() -> None:
        try:
            status = await client.status()  # 401 here was the bug
            assert status.tier in ("free", "pro")
            await _write_then_read(client)
        finally:
            await client.close()

    asyncio.run(_use())


def test_replace_needs_the_users_confirmation(home: Path) -> None:
    _pair_fresh_account(home)
    creds_path = home / ".totalreclaw" / "credentials.json"
    first_account = creds_path.read_bytes()

    # Upload without the page's confirmation: refused, nothing changes.
    sidecar = _Sidecar(replace=True)
    handshake = sidecar.start()
    assert handshake["status"] == "opened", handshake.get("error_message")
    res = _respond(handshake, _new_phrase())
    assert res.status_code == 400
    assert res.json().get("error") == "replace_not_confirmed"
    assert creds_path.read_bytes() == first_account

    # The same session, now confirmed on the page: replaced, old kept.
    res = _respond(handshake, _new_phrase(), replace_confirmed=True)
    assert res.status_code == 204, res.text
    sidecar.join()
    assert creds_path.read_bytes() != first_account
    kept = list(creds_path.parent.glob("credentials.json.replaced-*"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == first_account
