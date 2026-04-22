"""pair.http_server — embedded HTTP server for the Hermes QR-pair flow.

Python parity of ``skill/plugin/pair-http.ts``. Spawns a stdlib
``http.server`` on ``127.0.0.1:<ephemeral>`` (fixed port configurable
for tests) that serves:

- ``GET /pair/<token>`` — the browser pair page (HTML + inline crypto).
  Browser reads the gateway's ephemeral pubkey from the URL fragment
  (``#pk=<b64url>``) — the fragment NEVER hits the server, which keeps
  the pubkey out of server logs.

- ``POST /pair/<token>`` — accepts ``{v, sid, pk_d, pin, nonce, ct}``,
  verifies the PIN, decrypts the phrase via x25519 ECDH + HKDF-SHA256
  + ChaCha20-Poly1305, writes ``~/.totalreclaw/credentials.json``, and
  returns ``204``. Errors: ``403`` for PIN / attempt-exhausted, ``410``
  for expired, ``400`` for bad body / decrypt fail, ``404`` for unknown
  token.

The server is bound to ``127.0.0.1`` only — no LAN exposure. For remote
phones, the CLI instructions tell the user to SSH-port-forward the
ephemeral port, or (recommended) run the pair flow from the same phone's
browser on the gateway host directly.

Scanner / surface hygiene:

- No ``urllib.request``, no outbound HTTP, no env-var reads. All config
  flows in via ``PairHttpConfig``.
- No phrase material in logs. Session ids are logged with a prefix-only
  redaction.
- No ``os.environ`` reads; the caller supplies paths + ports.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from cryptography.exceptions import InvalidTag

from .crypto import decrypt_pairing_payload
from .pair_page import render_pair_page
from .session_store import (
    MAX_SECONDARY_CODE_ATTEMPTS,
    PairSession,
    consume_pair_session,
    default_now_ms,
    get_pair_session,
    register_failed_secondary_code,
    reject_pair_session,
    transition_pair_session,
)
from ..pair.crypto import compare_secondary_codes_ct


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class CompletePairingResult:
    """Returned by the caller-injected completion handler."""

    state: str  # "active" or "error"
    account_id: Optional[str] = None
    error: Optional[str] = None


CompletePairingHandler = Callable[[str, PairSession], CompletePairingResult]


@dataclass
class PairHttpConfig:
    """Config bundle for :func:`build_pair_http_server`.

    ``sessions_path`` and ``complete_pairing`` are required. The rest have
    reasonable defaults (ephemeral port bind, 8 KiB body cap, default BIP-
    39 word-count validator).
    """

    sessions_path: Path
    complete_pairing: CompletePairingHandler
    bind_host: str = "127.0.0.1"
    bind_port: int = 0  # 0 = ephemeral
    max_body_bytes: int = 8 * 1024
    validate_mnemonic: Optional[Callable[[str], bool]] = None
    now: Optional[Callable[[], int]] = None
    logger: logging.Logger = field(default=logger)


# ---------------------------------------------------------------------------
# Default BIP-39 word-count validator (same policy as the TS default)
# ---------------------------------------------------------------------------


def _default_mnemonic_validator(phrase: str) -> bool:
    words = phrase.strip().split(" ")
    if len(words) not in (12, 24):
        return False
    return all(w.isascii() and w.isalpha() and w.islower() for w in words)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class PairHttpServer:
    """Thin wrapper around an ``HTTPServer`` with start / stop / url helpers."""

    def __init__(self, http_server: HTTPServer, config: PairHttpConfig):
        self._server = http_server
        self._config = config
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    def url_for(self, token: str) -> str:
        return f"http://{self.host}:{self.port}/pair/{token}"

    def start(self) -> None:
        """Run the server in a background daemon thread."""
        if self._thread is not None:
            return
        t = threading.Thread(
            target=self._server.serve_forever,
            name="totalreclaw-pair-http",
            daemon=True,
        )
        t.start()
        self._thread = t

    def stop(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
            self._thread = None

    def handle_one_request(self) -> None:
        """Test helper — serve a single request synchronously."""
        self._server.handle_request()


def _parse_pair_token(path: str) -> Optional[str]:
    """Extract the pair token from ``/pair/<token>``.

    Rejects paths with extra segments or query params containing the token.
    Query params on ``/pair/<token>?x=y`` are tolerated (urlparse.path
    strips them), but ``/pair/<token>/more`` is rejected.
    """
    parsed = urlparse(path)
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "pair":
        return None
    tok = parts[1]
    if not tok or not tok.replace("-", "").replace("_", "").isalnum():
        return None
    return tok


def _redact_sid(sid: str) -> str:
    if len(sid) <= 10:
        return "[redacted-sid]"
    return f"{sid[:6]}…{sid[-2:]}"


def build_pair_http_server(config: PairHttpConfig) -> PairHttpServer:
    """Construct a ``PairHttpServer`` wired to ``config``.

    Returns the server without starting it; caller calls ``.start()``.
    """
    cfg = config
    now_fn = cfg.now or default_now_ms
    validate = cfg.validate_mnemonic or _default_mnemonic_validator
    log = cfg.logger

    class Handler(BaseHTTPRequestHandler):
        # Quiet default access-logging; we emit our own redacted lines via logger.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            log.debug("pair-http: %s", format % args)

        # ---- Responses -------------------------------------------------

        def _send_html(self, code: int, body: str) -> None:
            body_bytes = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            # Tight CSP — mirror the TS side. Inline scripts + styles are
            # required for the self-contained page; no external resources
            # are loaded.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body_bytes)

        def _send_plain(self, code: int, body: str) -> None:
            body_bytes = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body_bytes)

        def _send_json(self, code: int, body: Dict[str, Any]) -> None:
            body_bytes = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body_bytes)

        def _send_empty(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()

        # ---- Routing ---------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            token = _parse_pair_token(self.path)
            if token is None:
                self._send_plain(404, "not found")
                return
            session = get_pair_session(cfg.sessions_path, token, now_fn)
            if session is None:
                self._send_plain(404, "session not found or already expired")
                return
            if session.status in ("completed", "consumed", "expired", "rejected"):
                self._send_plain(410, "session is no longer available")
                return
            api_base = f"/pair/{token}"
            html = render_pair_page(
                sid=session.sid,
                mode=session.mode,
                expires_at_ms=session.expires_at_ms,
                api_base=api_base,
                now_ms=now_fn(),
            )
            self._send_html(200, html)

        def do_POST(self) -> None:  # noqa: N802
            token = _parse_pair_token(self.path)
            if token is None:
                self._send_json(404, {"error": "not_found"})
                return

            # Body read with size cap
            length_hdr = self.headers.get("Content-Length", "0")
            try:
                length = int(length_hdr)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "bad_content_length"})
                return
            if length > cfg.max_body_bytes:
                self._send_json(413, {"error": "body_too_large"})
                return
            ct_type = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ct_type:
                self._send_json(400, {"error": "content_type_must_be_json"})
                return

            try:
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid_json"})
                return

            parsed = _validate_respond_body(body)
            if parsed is None:
                self._send_json(400, {"error": "bad_body"})
                return
            sid, pk_d, nonce, ct, pin = parsed

            # Enforce sid-matches-token: the URL-path token IS the session
            # id. The body MAY omit sid; if present, it must match.
            if sid != token:
                self._send_json(400, {"error": "sid_mismatch"})
                return

            # Look up session for PIN check FIRST — before consuming.
            session = get_pair_session(cfg.sessions_path, token, now_fn)
            if session is None:
                self._send_json(404, {"error": "not_found"})
                return
            if now_fn() > session.expires_at_ms:
                self._send_json(410, {"error": "expired"})
                return
            if session.status in ("completed", "consumed", "expired", "rejected"):
                status_map = {
                    "rejected": 403,
                    "expired": 410,
                    "completed": 409,
                    "consumed": 409,
                }
                self._send_json(status_map[session.status], {"error": session.status})
                return

            # Constant-time PIN compare + strike counter.
            if not compare_secondary_codes_ct(pin, session.secondary_code):
                after = register_failed_secondary_code(cfg.sessions_path, token, now_fn)
                if after and after.status == "rejected":
                    log.warning(
                        "pair-http: session %s locked out after %d wrong PINs",
                        _redact_sid(token),
                        MAX_SECONDARY_CODE_ATTEMPTS,
                    )
                    self._send_json(403, {"error": "attempts_exhausted"})
                    return
                self._send_json(403, {"error": "wrong_pin"})
                return

            # Consume atomically — flips to 'consumed' before crypto work.
            consumed = consume_pair_session(cfg.sessions_path, token, now_fn)
            if not consumed.ok or consumed.session is None:
                code_map = {
                    "not_found": 404,
                    "expired": 410,
                    "rejected": 403,
                    "already_consumed": 409,
                }
                err = consumed.error or "not_found"
                self._send_json(code_map.get(err, 409), {"error": err})
                return

            session = consumed.session

            # Decrypt.
            try:
                plaintext = decrypt_pairing_payload(
                    sk_gateway_b64=session.sk_gateway_b64,
                    pk_device_b64=pk_d,
                    sid=token,
                    nonce_b64=nonce,
                    ciphertext_b64=ct,
                )
            except (InvalidTag, ValueError) as err:
                reject_pair_session(cfg.sessions_path, token, now_fn)
                log.warning(
                    "pair-http: session %s decrypt failed: %s",
                    _redact_sid(token),
                    type(err).__name__,
                )
                self._send_json(400, {"error": "decrypt_failed"})
                return

            try:
                phrase = plaintext.decode("utf-8")
                # NFKC + lowercase + collapse whitespace (BIP-39 norm).
                import unicodedata

                phrase = unicodedata.normalize("NFKC", phrase).strip().lower()
                phrase = " ".join(phrase.split())
            except UnicodeDecodeError:
                reject_pair_session(cfg.sessions_path, token, now_fn)
                self._send_json(400, {"error": "bad_utf8"})
                return
            finally:
                # Best-effort zero (bytes is immutable in Python; rebinding
                # drops our reference but other copies may linger).
                plaintext = b"\x00" * len(plaintext)  # noqa: F841

            if not validate(phrase):
                reject_pair_session(cfg.sessions_path, token, now_fn)
                log.warning(
                    "pair-http: session %s invalid phrase payload",
                    _redact_sid(token),
                )
                # The JSON error code stays ``invalid_mnemonic`` for API
                # parity with the TS side; the log line above uses the
                # canonical user-facing terminology ("phrase") to stay
                # aligned with the ``test_onboarding.py`` terminology
                # parity check.
                self._send_json(400, {"error": "invalid_mnemonic"})
                return

            # Hand off to the caller's completion handler — writes
            # credentials.json + flips onboarding state.
            try:
                result = cfg.complete_pairing(phrase, session)
            except Exception as err:  # pragma: no cover — defensive
                log.error("pair-http: complete_pairing raised: %r", err)
                self._send_json(500, {"error": "completion_failed"})
                return
            finally:
                # Drop the phrase reference. Python can't guarantee GC but
                # rebinding at least releases our handle.
                phrase = ""  # noqa: F841

            if result.state == "active":
                transition_pair_session(cfg.sessions_path, token, "completed", now_fn)
                log.info("pair-http: session %s completed", _redact_sid(token))
                # 204 means "processed, nothing to say" — the browser
                # treats any 2xx as success.
                self._send_empty(204)
            else:
                log.warning(
                    "pair-http: session %s completion error: %s",
                    _redact_sid(token),
                    result.error or "unknown",
                )
                self._send_json(500, {"error": result.error or "completion_state_unknown"})

        # Silence the stdlib's verbose "code 200, message OK" default log.
        def log_request(self, code="-", size="-"):  # noqa: D401
            log.debug("pair-http: %s %s -> %s", self.command, self.path, code)

    http = HTTPServer((cfg.bind_host, cfg.bind_port), Handler)
    return PairHttpServer(http, cfg)


# ---------------------------------------------------------------------------
# Body validation (stays out of the handler class for testability)
# ---------------------------------------------------------------------------


def _validate_respond_body(
    body: Any,
) -> Optional[Tuple[str, str, str, str, str]]:
    """Return ``(sid, pk_d, nonce, ct, pin)`` if valid, else ``None``."""
    if not isinstance(body, dict):
        return None
    if body.get("v") != 1:
        return None
    sid = body.get("sid")
    pk_d = body.get("pk_d")
    nonce = body.get("nonce")
    ct = body.get("ct")
    pin = body.get("pin")
    if not all(isinstance(x, str) for x in (sid, pk_d, nonce, ct, pin)):
        return None

    # Basic shape checks — avoid expensive crypto on obviously-bad input.
    if not (isinstance(sid, str) and 16 <= len(sid) <= 64):
        return None
    if not (isinstance(pk_d, str) and 40 <= len(pk_d) <= 48):  # 32 bytes b64url ~= 43 chars
        return None
    if not (isinstance(nonce, str) and 14 <= len(nonce) <= 18):  # 12 bytes b64url = 16 chars
        return None
    if not (isinstance(ct, str) and 20 <= len(ct) <= 4096):
        return None
    if not (isinstance(pin, str) and len(pin) == 6 and pin.isdigit()):
        return None

    return (sid, pk_d, nonce, ct, pin)
