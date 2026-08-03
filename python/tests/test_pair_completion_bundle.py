"""Tests for the ``payload_type == "derived-bundle-v1"`` pair-completion
branch (Option E Phase 2, P2-10, #581 — the cred-12 leaf).

As of this writing the relay does not forward a ``payload_type`` field on
the pair envelope at all (P2-0 pre-flight; plumbing it is tracked
separately as P2-11). Per the implementation spec's explicit instruction,
this branch is therefore built and tested against a **fixture**
provisioning half — a local loopback WebSocket server standing in for the
relay, exactly like the existing legacy-mnemonic pair tests
(``test_pair_remote_client.py``, ``test_pair_sidecar_lifecycle.py``) — not
against live relay/SPA traffic.

Two levels of coverage:

  1. ``remote_client.await_phrase_upload`` directly, with a
     ``complete_pairing_bundle`` stub — fast, isolates the decrypt +
     ``parse_bundle_v1`` dispatch + malformed-payload rejection paths from
     the persistence side-effects.
  2. The full ``completion_sidecar.run_sidecar_inline`` flow — proves the
     happy path actually lands a v2 credential store on disk (keychain-
     wrapped), and that the wrong-chain rejection writes nothing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional

import pytest
import websockets

totalreclaw_core = pytest.importorskip("totalreclaw_core")

pytestmark = pytest.mark.skipif(
    not hasattr(totalreclaw_core, "derive_bundle_from_mnemonic"),
    reason="installed totalreclaw_core predates the derived-bundle-v1 bindings (#581)",
)

from totalreclaw import credentials_wrap as cw  # noqa: E402
from totalreclaw.bundle import DerivedBundle, derive_bundle_from_mnemonic  # noqa: E402
from totalreclaw.pair.crypto import (  # noqa: E402
    encrypt_pairing_payload,
    generate_gateway_keypair,
)
from totalreclaw.pair.remote_client import (  # noqa: E402
    await_phrase_upload,
    open_remote_pair_session,
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SMART_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"


def _canonical_bundle_json(*, chain_id: int = 100) -> str:
    return totalreclaw_core.derive_bundle_from_mnemonic(
        TEST_MNEMONIC, chain_id, "spa", TEST_SMART_ACCOUNT
    )


# ---------------------------------------------------------------------------
# Fixture relay — pushes a payload_type-tagged forward frame.
# ---------------------------------------------------------------------------


class _BundleRelayStub:
    """Loopback WS server impersonating the relay for one bundle-payload
    session. Mirrors ``test_pair_remote_client.py``'s ``RelayStub`` but
    tags the forward frame with ``payload_type`` and encrypts arbitrary
    plaintext (a bundle JSON string) instead of a phrase."""

    def __init__(self, token: str = "bundle-test-token"):
        self.token = token
        self.ack_received: Optional[dict] = None
        self.nack_received: Optional[dict] = None
        self._payload_type: Optional[str] = None
        self._plaintext: Optional[bytes] = None
        self._server: Optional[websockets.Server] = None
        self._port = 0

    def set_bundle_payload(self, payload_type: str, plaintext: bytes) -> None:
        self._payload_type = payload_type
        self._plaintext = plaintext

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def start(self) -> None:
        async def handler(ws):
            raw = await ws.recv()
            open_frame = json.loads(raw)
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

            if self._plaintext is not None:
                kp_device = generate_gateway_keypair()
                gateway_pubkey = open_frame["gateway_pubkey"]
                nonce_b64, ct_b64 = encrypt_pairing_payload(
                    sk_local_b64=kp_device.sk_b64,
                    pk_remote_b64=gateway_pubkey,
                    sid=self.token,
                    plaintext=self._plaintext,
                )
                frame = {
                    "type": "forward",
                    "client_pubkey": kp_device.pk_b64,
                    "nonce": nonce_b64,
                    "ciphertext": ct_b64,
                }
                if self._payload_type is not None:
                    frame["payload_type"] = self._payload_type
                await ws.send(json.dumps(frame))

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


async def _open_session(relay: _BundleRelayStub):
    return await open_remote_pair_session(
        relay_base_url=relay.url, pin="123456", client_id="gw-bundle-test"
    )


# ---------------------------------------------------------------------------
# await_phrase_upload level — decrypt + parse_bundle_v1 dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_bundle_invokes_complete_pairing_bundle_and_acks():
    relay = _BundleRelayStub(token="tok-happy")
    relay.set_bundle_payload("derived-bundle-v1", _canonical_bundle_json().encode("utf-8"))
    await relay.start()
    try:
        session = await _open_session(relay)
        captured: List[DerivedBundle] = []

        async def complete_bundle(bundle: DerivedBundle) -> dict:
            captured.append(bundle)
            return {"state": "active", "account_id": bundle.signing.address}

        async def complete_phrase(phrase: str) -> dict:
            raise AssertionError("should not reach the phrase handler for a bundle payload")

        result = await await_phrase_upload(
            session,
            complete_pairing=complete_phrase,
            complete_pairing_bundle=complete_bundle,
        )

        assert len(captured) == 1
        assert captured[0].account.smart_account.lower() == TEST_SMART_ACCOUNT.lower()
        assert result["state"] == "active"
        await asyncio.sleep(0.1)
        assert relay.ack_received is not None
        assert relay.nack_received is None
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_legacy_mnemonic_payload_type_still_completes():
    """S5.5 — the legacy payload_type still completes successfully
    alongside the new bundle branch."""
    relay = _BundleRelayStub(token="tok-legacy")
    relay.set_bundle_payload("legacy-mnemonic", TEST_MNEMONIC.encode("utf-8"))
    await relay.start()
    try:
        session = await _open_session(relay)
        captured: List[str] = []

        async def complete_phrase(phrase: str) -> dict:
            captured.append(phrase)
            return {"state": "active", "account_id": "0xdeadbeef"}

        result = await await_phrase_upload(session, complete_pairing=complete_phrase)

        assert captured == [TEST_MNEMONIC]
        assert result["state"] == "active"
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_bundle_payload_without_a_bundle_handler_nacks_unsupported():
    relay = _BundleRelayStub(token="tok-no-handler")
    relay.set_bundle_payload("derived-bundle-v1", _canonical_bundle_json().encode("utf-8"))
    await relay.start()
    try:
        session = await _open_session(relay)

        async def complete_phrase(phrase: str) -> dict:
            raise AssertionError("unreachable")

        with pytest.raises(RuntimeError, match="complete_pairing_bundle"):
            await await_phrase_upload(session, complete_pairing=complete_phrase)

        await asyncio.sleep(0.1)
        assert relay.nack_received is not None
        assert relay.nack_received.get("error") == "unsupported_payload_type"
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_unknown_payload_type_nacks_and_raises():
    relay = _BundleRelayStub(token="tok-unknown-pt")
    relay.set_bundle_payload("some-future-payload-shape-v9", b"opaque")
    await relay.start()
    try:
        session = await _open_session(relay)

        async def complete_phrase(phrase: str) -> dict:
            raise AssertionError("unreachable")

        with pytest.raises(RuntimeError, match="unrecognised payload_type"):
            await await_phrase_upload(session, complete_pairing=complete_phrase)

        await asyncio.sleep(0.1)
        assert relay.nack_received is not None
        assert relay.nack_received.get("error") == "unknown_payload_type"
    finally:
        await relay.stop()


@pytest.mark.parametrize(
    "mutate,expected_match",
    [
        pytest.param(
            lambda j: j.replace('"encryption_key":"', '"encryption_key":"zz', 1),
            None,
            id="malformed_hex",
        ),
        pytest.param(
            lambda j: j.replace(
                _canonical_bundle_json().split('"address":"')[1].split('"')[0],
                "9999999999999999999999999999999999999999",
                1,
            ),
            None,
            id="address_mismatch",
        ),
        pytest.param(
            lambda j: j.replace('"kind":"owner-eoa"', '"kind":"root-eoa"', 1),
            None,
            id="unknown_signing_kind",
        ),
    ],
)
@pytest.mark.asyncio
async def test_malformed_bundle_payload_rejects_cleanly(mutate, expected_match):
    """Each malformed-payload class nacks 'invalid_bundle' and never
    reaches complete_pairing_bundle — nothing is persisted."""
    bad_json = mutate(_canonical_bundle_json())
    relay = _BundleRelayStub(token="tok-malformed")
    relay.set_bundle_payload("derived-bundle-v1", bad_json.encode("utf-8"))
    await relay.start()
    try:
        session = await _open_session(relay)

        async def complete_bundle(bundle) -> dict:
            raise AssertionError("must never reach complete_pairing_bundle on a malformed bundle")

        async def complete_phrase(phrase: str) -> dict:
            raise AssertionError("unreachable")

        with pytest.raises(Exception):
            await await_phrase_upload(
                session,
                complete_pairing=complete_phrase,
                complete_pairing_bundle=complete_bundle,
            )

        await asyncio.sleep(0.1)
        assert relay.nack_received is not None
        assert relay.nack_received.get("error") == "invalid_bundle"
    finally:
        await relay.stop()


# ---------------------------------------------------------------------------
# Full sidecar integration — lands a real v2 credential store on disk.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def fake_keychain(monkeypatch):
    monkeypatch.delenv(cw.ENV_NO_KEYCHAIN, raising=False)
    store: dict[str, str] = {}

    def _store(account: str, secret: str) -> None:
        store[account] = secret

    def _load(account: str) -> str:
        if account not in store:
            raise cw.KeychainEntryMissing(cw.MISSING_MESSAGE)
        return store[account]

    monkeypatch.setattr(cw, "detect_backend", lambda: "test-fake")
    monkeypatch.setattr(cw, "store_secret", _store)
    monkeypatch.setattr(cw, "load_secret", _load)
    return store


@pytest.mark.asyncio
async def test_happy_path_lands_a_v2_credential_store(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from totalreclaw.pair.completion_sidecar import run_sidecar_inline

    relay = _BundleRelayStub(token="tok-sidecar-happy")
    relay.set_bundle_payload("derived-bundle-v1", _canonical_bundle_json().encode("utf-8"))
    await relay.start()
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)
    try:
        await asyncio.to_thread(
            run_sidecar_inline, handshake_id="sidecar-bundle-happy", mode="either", relay_url=None
        )
    finally:
        await relay.stop()

    creds_path = isolated_home / ".totalreclaw" / "credentials.json"
    assert creds_path.exists()
    v2 = json.loads(creds_path.read_text())
    assert v2["version"] == 2
    assert v2["schema"] == "derived-bundle-v1"
    assert v2.get("keychain_wrapped") is True
    assert v2["account"]["smart_account"].lower() == TEST_SMART_ACCOUNT.lower()
    assert "vault" not in v2  # secret subtree lives in the keychain
    assert "private_key" not in json.dumps(v2)


@pytest.mark.asyncio
async def test_wrong_chain_rejects_without_persisting_anything(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from totalreclaw.pair.completion_sidecar import run_sidecar_inline

    wrong_chain_json = _canonical_bundle_json(chain_id=1)  # not 100
    relay = _BundleRelayStub(token="tok-wrong-chain")
    relay.set_bundle_payload("derived-bundle-v1", wrong_chain_json.encode("utf-8"))
    await relay.start()
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)
    try:
        await asyncio.to_thread(
            run_sidecar_inline, handshake_id="sidecar-bundle-wrong-chain", mode="either", relay_url=None
        )
    finally:
        await relay.stop()

    creds_path = isolated_home / ".totalreclaw" / "credentials.json"
    assert not creds_path.exists()
    assert len(fake_keychain) == 0  # wrap_bundle never called


@pytest.mark.asyncio
async def test_sidecar_completed_client_never_eagerly_resolves_data_edge(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for major-1 (coordinator review, 2026-08-03): #592's
    loopback-fixture suite originally only asserted on the on-disk
    credential shape, which could not catch a bug in
    TotalReclaw.from_bundle's chain/data-edge resolution timing (that bug
    lived entirely in in-memory client state the other sidecar tests never
    inspected). This test drives the SAME sidecar completion path and then
    asserts on the resulting client's data-edge source directly.

    A client configured via ANY code path that terminates in
    TotalReclaw.from_bundle — sidecar pair-completion (P2-10) included —
    must NOT have _chain_id_resolved eagerly set True with
    _data_edge_address left None, because that combination causes the
    write path to fall through to core's hardcoded PRODUCTION
    DATA_EDGE_ADDRESS regardless of which relay environment the client is
    actually configured against (confirmed live on staging, 2026-08-03:
    tx 0xa70c3402b7e2f338a17212d2acfc5a9bf843689aaea237df5572f4ef637ae447
    landed on the production subgraph from what was meant to be an
    isolated staging E2E run).
    """
    from totalreclaw.pair.completion_sidecar import run_sidecar_inline

    relay = _BundleRelayStub(token="tok-data-edge-source")
    relay.set_bundle_payload("derived-bundle-v1", _canonical_bundle_json().encode("utf-8"))
    await relay.start()
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", relay.url)
    try:
        await asyncio.to_thread(
            run_sidecar_inline,
            handshake_id="sidecar-data-edge-source",
            mode="either",
            relay_url=None,
        )
    finally:
        await relay.stop()

    # A fresh AgentState() in the SAME process, auto-configuring from the
    # v2 credentials the sidecar just wrote — exactly what the NEXT
    # session (a real subsequent Hermes boot) does. This is the client
    # whose data-edge resolution behaviour matters, not the sidecar's own
    # short-lived internal one.
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    assert state.is_configured()
    client = state.get_client()

    assert client._mnemonic is None  # genuinely bundle-configured
    assert client._data_edge_address is None  # not yet resolved — correct so far
    assert client._chain_id_resolved is False, (
        "a bundle-configured client must NOT start with chain_id eagerly "
        "marked resolved — that skips the billing lookup that populates "
        "_data_edge_address, and the write path silently falls through to "
        "core's hardcoded PRODUCTION DataEdge default regardless of which "
        "relay the client is actually configured against"
    )
