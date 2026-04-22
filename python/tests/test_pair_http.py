"""Tests for ``totalreclaw.pair.http_server``.

Spins up the embedded pair HTTP server on ``127.0.0.1`` with an ephemeral
port, posts well-formed encrypted payloads, verifies credentials.json
gets written via the injected completion handler. Error paths:
PIN-mismatch → 403, expired session → 410, unknown token → 404, tamper
→ 400.

No real gateway is spun up; no real credentials.json is touched. Tests
use pytest's ``tmp_path`` fixture for the pair-sessions.json + mock the
completion handler so the flow exercises only the pair module.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from totalreclaw.pair.crypto import (
    encrypt_pairing_payload,
    generate_gateway_keypair,
    _b64url_encode,
)
from totalreclaw.pair.http_server import (
    CompletePairingResult,
    PairHttpConfig,
    _validate_respond_body,
    build_pair_http_server,
)
from totalreclaw.pair.session_store import (
    create_pair_session,
    default_pair_sessions_path,
)


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    """Hermetic per-test pair-sessions directory."""
    d = tmp_path / "totalreclaw"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def sessions_path(sessions_dir: Path) -> Path:
    return default_pair_sessions_path(sessions_dir)


class FakeCompletionSink:
    """Captures the phrase for assertion + returns an ``active`` result.

    Scope: test-only. Real callers use
    ``hermes.pair_tool_completion.complete_pairing`` which wires through
    :class:`PluginState`.
    """

    def __init__(self) -> None:
        self.phrase: str | None = None
        self.called = False

    def __call__(self, phrase: str, session):
        self.phrase = phrase
        self.called = True
        return CompletePairingResult(state="active", account_id="0xabc")


def _start_server(sessions_path: Path, sink: FakeCompletionSink):
    cfg = PairHttpConfig(
        sessions_path=sessions_path,
        complete_pairing=sink,
        bind_host="127.0.0.1",
        bind_port=0,  # ephemeral
    )
    server = build_pair_http_server(cfg)
    server.start()
    return server


def _http_post(server, path: str, body: dict) -> tuple[int, dict | None]:
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8")
        conn.request(
            "POST",
            path,
            payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        resp = conn.getresponse()
        data = resp.read()
        try:
            parsed = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = None
        return resp.status, parsed
    finally:
        conn.close()


def _http_get(server, path: str) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, headers
    finally:
        conn.close()


class TestValidateBody:
    def test_accepts_well_formed(self):
        body = {
            "v": 1,
            "sid": "a" * 32,
            "pk_d": "A" * 43,
            "nonce": "N" * 16,
            "ct": "C" * 100,
            "pin": "123456",
        }
        assert _validate_respond_body(body) is not None

    def test_rejects_wrong_version(self):
        body = {"v": 2, "sid": "a" * 32, "pk_d": "A" * 43, "nonce": "N" * 16, "ct": "C" * 100, "pin": "123456"}
        assert _validate_respond_body(body) is None

    def test_rejects_bad_pin(self):
        body = {"v": 1, "sid": "a" * 32, "pk_d": "A" * 43, "nonce": "N" * 16, "ct": "C" * 100, "pin": "12345"}
        assert _validate_respond_body(body) is None
        body["pin"] = "abcdef"
        assert _validate_respond_body(body) is None

    def test_rejects_non_dict(self):
        assert _validate_respond_body("not a dict") is None  # type: ignore[arg-type]
        assert _validate_respond_body(None) is None


class TestHappyPath:
    def test_post_with_valid_payload_writes_credentials(
        self, sessions_path: Path
    ):
        kp = generate_gateway_keypair()
        session = create_pair_session(
            sessions_path,
            mode="import",
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
            secondary_code="482914",
        )
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            # Device side: generate an ephemeral keypair + encrypt the phrase.
            dev = generate_gateway_keypair()
            plaintext = (
                "abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon abandon about"
            ).encode("utf-8")
            nonce_b64, ct_b64 = encrypt_pairing_payload(
                sk_local_b64=dev.sk_b64,
                pk_remote_b64=kp.pk_b64,
                sid=session.sid,
                plaintext=plaintext,
            )
            body = {
                "v": 1,
                "sid": session.sid,
                "pk_d": dev.pk_b64,
                "nonce": nonce_b64,
                "ct": ct_b64,
                "pin": "482914",
            }
            status, _ = _http_post(server, f"/pair/{session.sid}", body)
            assert status == 204
            assert sink.called is True
            assert sink.phrase is not None
            assert sink.phrase.split() == plaintext.decode("utf-8").split()
        finally:
            server.stop()


class TestErrorPaths:
    def test_unknown_token_returns_404(self, sessions_path: Path):
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            body = {
                "v": 1,
                "sid": "x" * 32,
                "pk_d": "A" * 43,
                "nonce": "N" * 16,
                "ct": "C" * 100,
                "pin": "123456",
            }
            status, parsed = _http_post(server, f"/pair/{'x' * 32}", body)
            assert status == 404
        finally:
            server.stop()

    def test_wrong_pin_returns_403(self, sessions_path: Path):
        kp = generate_gateway_keypair()
        session = create_pair_session(
            sessions_path,
            mode="generate",
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
            secondary_code="111111",
        )
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            body = {
                "v": 1,
                "sid": session.sid,
                "pk_d": "A" * 43,
                "nonce": "N" * 16,
                "ct": "C" * 100,
                "pin": "222222",  # wrong
            }
            status, parsed = _http_post(server, f"/pair/{session.sid}", body)
            assert status == 403
            assert parsed is not None
            assert parsed.get("error") == "wrong_pin"
            assert sink.called is False
        finally:
            server.stop()

    def test_expired_session_returns_410(self, sessions_path: Path):
        kp = generate_gateway_keypair()
        # Create a session with a clock that places creation in the distant past.
        t_then = 1_000_000_000
        session = create_pair_session(
            sessions_path,
            mode="generate",
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
            secondary_code="333333",
            now=lambda: t_then,
        )
        sink = FakeCompletionSink()

        # Build the server with a ``now`` that's past expiry.
        future_now_ms = session.expires_at_ms + 60_000
        cfg = PairHttpConfig(
            sessions_path=sessions_path,
            complete_pairing=sink,
            bind_host="127.0.0.1",
            bind_port=0,
            now=lambda: future_now_ms,
        )
        server = build_pair_http_server(cfg)
        server.start()
        try:
            body = {
                "v": 1,
                "sid": session.sid,
                "pk_d": "A" * 43,
                "nonce": "N" * 16,
                "ct": "C" * 100,
                "pin": "333333",
            }
            status, _ = _http_post(server, f"/pair/{session.sid}", body)
            assert status == 410
            assert sink.called is False
        finally:
            server.stop()

    def test_tampered_ciphertext_returns_400(self, sessions_path: Path):
        kp = generate_gateway_keypair()
        session = create_pair_session(
            sessions_path,
            mode="import",
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
            secondary_code="555555",
        )
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            dev = generate_gateway_keypair()
            nonce_b64, ct_b64 = encrypt_pairing_payload(
                sk_local_b64=dev.sk_b64,
                pk_remote_b64=kp.pk_b64,
                sid=session.sid,
                plaintext=b"valid phrase doesnt matter here",
            )
            # Flip a byte in ciphertext.
            from totalreclaw.pair.crypto import _b64url_decode
            raw = bytearray(_b64url_decode(ct_b64))
            raw[3] ^= 0x01
            tampered = _b64url_encode(bytes(raw))

            body = {
                "v": 1,
                "sid": session.sid,
                "pk_d": dev.pk_b64,
                "nonce": nonce_b64,
                "ct": tampered,
                "pin": "555555",
            }
            status, parsed = _http_post(server, f"/pair/{session.sid}", body)
            assert status == 400
            assert parsed is not None
            assert parsed.get("error") == "decrypt_failed"
            assert sink.called is False
        finally:
            server.stop()


class TestGetPairPage:
    def test_get_returns_html_with_csp(self, sessions_path: Path):
        kp = generate_gateway_keypair()
        session = create_pair_session(
            sessions_path,
            mode="generate",
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
        )
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            status, body, headers = _http_get(server, f"/pair/{session.sid}")
            assert status == 200
            assert b"TotalReclaw pairing" in body
            assert "text/html" in headers.get("content-type", "")
            assert "no-store" in headers.get("cache-control", "")
            assert "default-src 'none'" in headers.get("content-security-policy", "")
        finally:
            server.stop()

    def test_get_unknown_token_returns_404(self, sessions_path: Path):
        sink = FakeCompletionSink()
        server = _start_server(sessions_path, sink)
        try:
            status, _, _ = _http_get(server, f"/pair/{'z' * 32}")
            assert status == 404
        finally:
            server.stop()
