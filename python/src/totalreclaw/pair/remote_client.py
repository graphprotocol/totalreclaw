"""pair.remote_client — gateway-side WebSocket client for the relay-brokered
pair flow (rc.10).

Design blueprint:
  docs/plans/2026-04-23-rc.10-relay-brokered-pair.md (internal repo).

Flow (this file implements the gateway half):

  1. Generate an ephemeral x25519 keypair (``pair.crypto.generate_gateway_keypair``).
  2. Open a short-lived WebSocket to ``wss://<relay>/pair/session/open``.
  3. Send ``{type: "open", gateway_pubkey, pin, client_id}``.
  4. Receive ``{type: "opened", token, short_url, expires_at}`` — use these
     to build the user-facing pair URL (token + ``#pk=<gateway_pubkey>``).
  5. Block on the WebSocket until the relay pushes
     ``{type: "forward", client_pubkey, nonce, ciphertext}``.
  6. Decrypt locally via ``pair.crypto.decrypt_pairing_payload`` using the
     gateway's private key. If decrypt succeeds + phrase is valid, call the
     ``complete_pairing`` handler (writes credentials.json).
  7. Send ``{type: "ack"}`` back. Close the WebSocket.

Phrase-safety invariants preserved:
  - Relay sees only ciphertext; it cannot derive the symmetric key without
    the gateway's private key.
  - The gateway pubkey transits the relay as a label in the open frame so
    the relay can display the session, but is ALSO bound into the URL
    fragment the user opens — the fragment never hits the relay.
  - Phrase NEVER enters any logs. PIN is never logged.
  - No ``TOTALRECLAW_PAIR_RELAY_URL`` credentials are required — auth is
    the single-use PIN + 5-minute TTL + gateway ECDH private key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from cryptography.exceptions import InvalidTag

from .crypto import decrypt_pairing_payload, generate_gateway_keypair, GatewayKeypair


logger = logging.getLogger(__name__)


# Default relay endpoint. Configurable via TOTALRECLAW_PAIR_RELAY_URL so
# self-hosters can point at their own relay.
DEFAULT_RELAY_URL = "wss://api-staging.totalreclaw.xyz"


@dataclass
class RemotePairSession:
    """Handle returned by :func:`open_remote_pair_session`. Carries the
    user-facing URL + PIN + keypair + a live WebSocket handle."""

    url: str
    pin: str
    token: str
    expires_at: str
    keypair: GatewayKeypair
    _ws: Any  # websockets.WebSocketClientProtocol — typed Any to avoid hard import


def _default_pin() -> str:
    """Uniform 6-digit PIN."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _default_client_id() -> str:
    """Fresh per-session client id — opaque to the relay."""
    return f"gw-{secrets.token_hex(8)}"


def _resolve_relay_url() -> str:
    """Read ``TOTALRECLAW_PAIR_RELAY_URL`` with a fallback to the staging default."""
    v = os.environ.get("TOTALRECLAW_PAIR_RELAY_URL")
    if v:
        return v.rstrip("/")
    return DEFAULT_RELAY_URL.rstrip("/")


def _build_user_url(relay_base: str, token: str, pk_b64: str) -> str:
    """Assemble the user-facing pair URL.

    Converts ``wss://`` -> ``https://`` and ``ws://`` -> ``http://`` for the
    URL the user opens in a browser. The gateway pubkey lives in the URL
    fragment so it never hits relay logs.
    """
    http_base = relay_base
    if http_base.startswith("wss://"):
        http_base = "https://" + http_base[len("wss://") :]
    elif http_base.startswith("ws://"):
        http_base = "http://" + http_base[len("ws://") :]
    return f"{http_base}/pair/p/{token}#pk={pk_b64}"


async def open_remote_pair_session(
    *,
    relay_base_url: Optional[str] = None,
    pin: Optional[str] = None,
    client_id: Optional[str] = None,
    mode: Optional[str] = None,
) -> RemotePairSession:
    """Open a pair session on the relay. Returns handle with URL + PIN.

    ``mode`` — optional UI hint forwarded to the relay. One of:
      - ``"generate"`` — pair page shows only the "generate new phrase" panel.
      - ``"import"``   — pair page shows only the "paste existing phrase" panel.
      - ``"either"``   — pair page shows both with a tab switcher (user picks).
      - ``None``       — omit from the open frame; relay defaults to ``either``.

    The caller is expected to:
      - Relay ``session.url`` + ``session.pin`` to the user via chat.
      - Then call :func:`await_phrase_upload` on the returned handle to
        block until the user completes the browser flow (or the TTL lapses).
    """
    import websockets

    base = (relay_base_url or _resolve_relay_url()).rstrip("/")
    ws_url = f"{base}/pair/session/open"
    actual_pin = pin or _default_pin()
    actual_client_id = client_id or _default_client_id()
    keypair = generate_gateway_keypair()

    ws = await websockets.connect(ws_url, open_timeout=10, close_timeout=5)
    try:
        open_frame: dict[str, Any] = {
            "type": "open",
            "gateway_pubkey": keypair.pk_b64,
            "pin": actual_pin,
            "client_id": actual_client_id,
        }
        if mode in ("generate", "import", "either"):
            open_frame["mode"] = mode
        await ws.send(json.dumps(open_frame))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
        raise

    if msg.get("type") == "error":
        err = str(msg.get("error") or "relay_error")
        try:
            await ws.close()
        except Exception:
            pass
        raise RuntimeError(f"pair.relay: session/open failed: {err}")

    if msg.get("type") != "opened":
        try:
            await ws.close()
        except Exception:
            pass
        raise RuntimeError(
            f"pair.relay: unexpected response type '{msg.get('type')}'"
        )

    token = str(msg["token"])
    expires_at = str(msg["expires_at"])
    user_url = _build_user_url(base, token, keypair.pk_b64)

    logger.info(
        "pair.remote_client: session opened token=%s… client_id=%s",
        token[:8],
        actual_client_id,
    )

    return RemotePairSession(
        url=user_url,
        pin=actual_pin,
        token=token,
        expires_at=expires_at,
        keypair=keypair,
        _ws=ws,
    )


# Complete-pairing handler signature (mirrors the local-server form).
CompleteHandler = Callable[[str], Awaitable[dict]]


async def await_phrase_upload(
    session: RemotePairSession,
    *,
    complete_pairing: CompleteHandler,
    phrase_validator: Optional[Callable[[str], bool]] = None,
    timeout_s: float = 300.0,
) -> dict:
    """Block until the relay pushes the encrypted phrase, then decrypt and
    persist via ``complete_pairing``.

    ``complete_pairing`` is async — it receives the decrypted phrase as a
    plain string and is expected to write credentials.json + return a dict
    like ``{"state": "active", "account_id": "0x..."}``. The return value
    is forwarded back to the caller.

    ``phrase_validator`` checks BIP-39 word-count / casing. Defaults to the
    same 12/24-word lowercase-ASCII check the local HTTP server uses.
    """

    def _default_validate(p: str) -> bool:
        words = p.strip().split(" ")
        if len(words) not in (12, 24):
            return False
        return all(w.isascii() and w.isalpha() and w.islower() for w in words)

    validate = phrase_validator or _default_validate
    ws = session._ws

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            await ws.close()
        except Exception:
            pass
        raise RuntimeError("pair.relay: phrase upload timed out")
    except Exception as err:
        try:
            await ws.close()
        except Exception:
            pass
        raise RuntimeError(f"pair.relay: websocket error while awaiting forward: {err}")

    msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    if msg.get("type") != "forward":
        try:
            await ws.send(json.dumps({"type": "nack", "error": "expected_forward"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise RuntimeError(f"pair.relay: unexpected frame '{msg.get('type')}'")

    client_pubkey = msg.get("client_pubkey")
    nonce = msg.get("nonce")
    ciphertext = msg.get("ciphertext")
    # Optional audit-only field. Carries "generate" | "import" so the
    # gateway can log WHICH UI path the browser used, without branching
    # on it — decrypt + completion are mode-agnostic. Anything else is
    # ignored (not rejected; the relay validates enum membership upstream).
    forward_mode = msg.get("mode")
    if forward_mode not in ("generate", "import"):
        forward_mode = None

    if not isinstance(client_pubkey, str) or not isinstance(nonce, str) or not isinstance(
        ciphertext, str
    ):
        try:
            await ws.send(json.dumps({"type": "nack", "error": "bad_forward_body"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise RuntimeError("pair.relay: forward frame missing required fields")

    # Decrypt locally (ciphertext + shared secret derivation never leave this host).
    try:
        plaintext = decrypt_pairing_payload(
            sk_gateway_b64=session.keypair.sk_b64,
            pk_device_b64=client_pubkey,
            sid=session.token,
            nonce_b64=nonce,
            ciphertext_b64=ciphertext,
        )
    except (InvalidTag, ValueError) as err:
        logger.warning(
            "pair.remote_client: decrypt failed for token=%s…: %s",
            session.token[:8],
            type(err).__name__,
        )
        try:
            await ws.send(json.dumps({"type": "nack", "error": "decrypt_failed"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise

    try:
        phrase = plaintext.decode("utf-8")
        phrase = unicodedata.normalize("NFKC", phrase).strip().lower()
        phrase = " ".join(phrase.split())
    except UnicodeDecodeError:
        try:
            await ws.send(json.dumps({"type": "nack", "error": "bad_utf8"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise
    finally:
        plaintext = b"\x00" * len(plaintext)  # noqa: F841 — best-effort scrub

    if not validate(phrase):
        try:
            await ws.send(json.dumps({"type": "nack", "error": "invalid_mnemonic"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise RuntimeError("pair.relay: phrase failed BIP-39 validation")

    try:
        result = await complete_pairing(phrase)
    except Exception as err:
        logger.error("pair.remote_client: complete_pairing raised: %r", err)
        try:
            await ws.send(json.dumps({"type": "nack", "error": "completion_failed"}))
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        raise
    finally:
        phrase = ""  # noqa: F841 — drop our reference

    # Ack the relay so the browser gets a 204.
    try:
        await ws.send(json.dumps({"type": "ack"}))
    except Exception as err:
        logger.warning(
            "pair.remote_client: ack send failed for token=%s…: %r",
            session.token[:8],
            err,
        )
    finally:
        try:
            await ws.close()
        except Exception:
            pass

    logger.info(
        "pair.remote_client: session completed token=%s… mode=%s",
        session.token[:8],
        forward_mode or "unspecified",
    )
    return result


async def pair_via_relay(
    *,
    complete_pairing: CompleteHandler,
    relay_base_url: Optional[str] = None,
    pin: Optional[str] = None,
) -> dict:
    """One-shot convenience: open session, return the handle to the caller
    via the ``initial_callback`` hook, then await the phrase upload and
    run the completion handler.

    Most callers will want to split this into two calls so the agent can
    tell the user the URL + PIN before blocking. See the tool handler in
    ``hermes/pair_tool.py`` for that pattern.
    """
    session = await open_remote_pair_session(
        relay_base_url=relay_base_url,
        pin=pin,
    )
    return await await_phrase_upload(session, complete_pairing=complete_pairing)
