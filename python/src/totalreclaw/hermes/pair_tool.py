"""Hermes agent tool: ``totalreclaw_pair``.

2.3.1rc10: default flow now routes through the relay-brokered WebSocket
(``api-staging.totalreclaw.xyz/pair/*``). The tool still returns
``{url, pin, expires_at, qr_png_b64, qr_unicode}`` — the URL now points
at the universally-reachable relay instead of a gateway-loopback port.

Backwards-compat: ``TOTALRECLAW_PAIR_MODE=local`` preserves the rc.4–rc.9
loopback HTTP flow for air-gapped / offline / self-hosted setups.

See ``~/.claude/projects/-Users-pdiogo-Documents-code-totalreclaw-
internal/memory/project_phrase_safety_rule.md`` for the governing rule.
The phrase still NEVER crosses LLM context — only the crypto-safe URL +
PIN round-trips through the agent.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (unchanged from rc.5 — tool contract stays stable across relay pivot)
# ---------------------------------------------------------------------------

PAIR_SCHEMA: Dict[str, Any] = {
    "name": "totalreclaw_pair",
    "description": (
        "Start a remote pairing session so the user can create or import "
        "a TotalReclaw recovery phrase from their browser. Returns a "
        "pairing URL and a 6-digit PIN. Relay both to the user verbatim — "
        "the user opens the URL in their browser and types the PIN. The "
        "phrase is entered and encrypted IN THE BROWSER and uploaded "
        "end-to-end-encrypted to this Hermes gateway; it NEVER touches "
        "the LLM provider or this chat transcript. Use this tool whenever "
        "the user wants to set up TotalReclaw. NEVER shell out to "
        "`totalreclaw setup` or `hermes setup` — those commands are for "
        "users running them in their own terminal OUTSIDE an agent shell."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["generate", "import", "either"],
                "description": (
                    '"generate" = browser creates a NEW 12-word recovery '
                    'phrase (pair page hides the import option). "import" = '
                    'user pastes an EXISTING phrase (pair page hides the '
                    'generate option). "either" = pair page shows both with '
                    'a tab switcher so the user picks (default — safest when '
                    "you don't know whether the user is new or returning)."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Local-mode module-singleton pair-HTTP server (lazy).
#
# Kept intact for ``TOTALRECLAW_PAIR_MODE=local`` — rc.4–rc.9 behaviour.
# ---------------------------------------------------------------------------

_SERVER_LOCK = threading.Lock()
_SERVER_INSTANCE: Optional["PairServerSingleton"] = None


class PairServerSingleton:
    """Wrap a single :class:`PairHttpServer` so multiple tool calls
    reuse one background server (one port, one credentials-write path).

    Not thread-safe for construction; caller uses the module-level lock.
    """

    def __init__(self, server, sessions_path: Path) -> None:
        self.server = server
        self.sessions_path = sessions_path
        self.started = False

    def ensure_started(self) -> None:
        if not self.started:
            self.server.start()
            self.started = True


def _resolve_sessions_dir() -> Path:
    """Default to ``~/.totalreclaw`` — the same dir credentials.json lives in."""
    return Path.home() / ".totalreclaw"


def _complete_pairing_handler(state: "PluginState"):
    """Closure that writes credentials.json + configures state — LOCAL mode."""
    from .pair_tool_completion import complete_pairing

    def _handler(phrase: str, session) -> Any:
        return complete_pairing(phrase, session, state)

    return _handler


def _get_or_build_local_server(state: "PluginState"):
    """Return the module-singleton local-mode pair server."""
    global _SERVER_INSTANCE
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is not None:
            return _SERVER_INSTANCE

        from ..pair import build_pair_http_server, default_pair_sessions_path
        from ..pair.http_server import PairHttpConfig

        base_dir = _resolve_sessions_dir()
        base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sessions_path = default_pair_sessions_path(base_dir)

        bind_host = os.environ.get("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
        bind_port_raw = os.environ.get("TOTALRECLAW_PAIR_BIND_PORT", "0")
        try:
            bind_port = int(bind_port_raw)
        except ValueError:
            bind_port = 0

        cfg = PairHttpConfig(
            sessions_path=sessions_path,
            complete_pairing=_complete_pairing_handler(state),
            bind_host=bind_host,
            bind_port=bind_port,
            logger=logger,
        )
        server = build_pair_http_server(cfg)
        _SERVER_INSTANCE = PairServerSingleton(server=server, sessions_path=sessions_path)
        return _SERVER_INSTANCE


# ---------------------------------------------------------------------------
# Pair-mode selector
# ---------------------------------------------------------------------------


def _pair_mode() -> str:
    """Return 'local' or 'relay' based on env.

    rc.10 default: 'relay'. Users can opt back into the rc.4–rc.9 loopback
    flow with ``TOTALRECLAW_PAIR_MODE=local``.
    """
    v = (os.environ.get("TOTALRECLAW_PAIR_MODE") or "relay").strip().lower()
    return "local" if v == "local" else "relay"


# ---------------------------------------------------------------------------
# Relay-mode: a background task completes the pairing after the tool returns.
# ---------------------------------------------------------------------------


def _spawn_relay_completion_task(
    session,
    state: "PluginState",
) -> None:
    """Schedule the phrase-upload-wait as a detached asyncio task.

    Called synchronously from the tool body. The tool returns the
    {url, pin, ...} payload to the agent, then this task blocks on the
    WebSocket until the user completes the browser flow (or the 5-minute
    TTL lapses).
    """
    from ..pair.remote_client import await_phrase_upload

    async def _complete_pairing(phrase: str) -> dict:
        # Run the synchronous state.configure in a thread so we don't stall
        # the asyncio loop while credentials.json + EOA derivation happens.
        loop = asyncio.get_running_loop()

        def _do() -> dict:
            try:
                state.configure(phrase)
                client = state.get_client()
                eoa = getattr(client, "_eoa_address", None)
                logger.info(
                    "pair-tool(relay): credentials configured for EOA %s (token %s…)",
                    eoa or "unknown",
                    session.token[:8],
                )
                return {"state": "active", "account_id": eoa}
            except Exception as err:
                logger.error(
                    "pair-tool(relay): complete_pairing failed token=%s…: %r",
                    session.token[:8],
                    err,
                )
                return {"state": "error", "error": str(err)}

        return await loop.run_in_executor(None, _do)

    async def _runner() -> None:
        try:
            await await_phrase_upload(session, complete_pairing=_complete_pairing)
        except Exception as err:
            logger.warning(
                "pair-tool(relay): background task failed token=%s…: %r",
                session.token[:8] if session.token else "?",
                err,
            )

    # Fire-and-forget task on the current event loop. Hermes tool handlers
    # run under asyncio so ``get_running_loop`` succeeds.
    loop = asyncio.get_running_loop()
    loop.create_task(_runner())


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


async def pair(args: dict, state: "PluginState", **kwargs) -> str:
    """Agent-callable handler for ``totalreclaw_pair``.

    Returns a JSON-encoded ``{url, pin, expires_at, qr_png_b64, qr_unicode,
    mode, instructions}`` payload. NO phrase data crosses this return value.

    Mode selection:
      - ``TOTALRECLAW_PAIR_MODE=local`` → rc.4–rc.9 loopback HTTP server.
      - unset / any other value → rc.10 relay-brokered WebSocket flow.

    UI-mode selection (passed to the pair page):
      - ``"generate"`` / ``"import"`` — pin the pair page to one panel.
      - ``"either"`` (default) — let the user pick on the page. This is
        the safest default for agents that don't know whether the user
        has an existing phrase or is creating one fresh.
    """
    raw_mode = args.get("mode")
    if raw_mode in ("generate", "import", "either"):
        mode = raw_mode
    else:
        mode = "either"
    pair_mode = _pair_mode()

    try:
        if pair_mode == "local":
            # Local mode's pair_page.py predates "either" — map it to
            # the first-run default (generate) since that's what a new
            # user on a fresh local install is most likely doing.
            local_mode = mode if mode in ("generate", "import") else "generate"
            url, pin, expires_iso, session = await _pair_local(state, local_mode)
        else:
            url, pin, expires_iso = await _pair_relay(state, mode)

        qr_png_b64 = ""
        qr_unicode = ""
        try:
            from ..pair import encode_png, encode_unicode  # type: ignore

            qr_png_b64 = base64.b64encode(encode_png(url)).decode("ascii")
            qr_unicode = encode_unicode(url)
        except Exception as qr_err:  # pragma: no cover — soft-fail
            logger.warning("QR encode failed (non-fatal): %s", qr_err)

        logger.info(
            "totalreclaw_pair: mode=%s transport=%s qr_png=%d qr_unicode=%d",
            mode,
            pair_mode,
            len(qr_png_b64),
            len(qr_unicode),
        )

        if mode == "generate":
            step3 = (
                "The browser generates a new 12-word recovery phrase. "
                "Write it down BEFORE confirming — the phrase is "
                "unrecoverable if lost.\n"
            )
        elif mode == "import":
            step3 = (
                "Paste your existing 12 or 24-word recovery phrase in the "
                "browser (never in this chat).\n"
            )
        else:
            step3 = (
                "On the pair page, pick 'Generate new' if you don't have a "
                "TotalReclaw recovery phrase yet, or 'Import existing' if "
                "you do. The phrase stays in the browser — never in chat.\n"
            )

        return json.dumps(
            {
                "url": url,
                "pin": pin,
                "expires_at": expires_iso,
                "mode": mode,
                "qr_png_b64": qr_png_b64,
                "qr_unicode": qr_unicode,
                "instructions": (
                    f"Relay these to the user verbatim:\n"
                    f"1. Open {url} in your browser.\n"
                    f"2. Enter PIN {pin} when asked.\n"
                    f"3. " + step3
                    + f"4. The encrypted phrase uploads to this gateway; it never crosses "
                    f"this chat.\n"
                    f"5. Come back to chat once the browser says 'Paired'. "
                    f"Restart the Hermes gateway so the plugin picks up the new "
                    f"credentials."
                ),
            }
        )
    except Exception as err:
        logger.error("totalreclaw_pair failed: %s", err)
        return json.dumps({"error": f"Failed to start pairing session: {err}"})


async def _pair_local(state: "PluginState", mode: str):
    """rc.4–rc.9 loopback HTTP-server path (preserved for backwards-compat)."""
    from ..pair import create_pair_session, generate_gateway_keypair

    singleton = _get_or_build_local_server(state)
    singleton.ensure_started()

    kp = generate_gateway_keypair()
    session = create_pair_session(
        singleton.sessions_path,
        mode=mode,
        sk_b64=kp.sk_b64,
        pk_b64=kp.pk_b64,
    )

    base_url = singleton.server.url_for(session.sid)
    url = f"{base_url}#pk={kp.pk_b64}"
    expires_iso = datetime.fromtimestamp(
        session.expires_at_ms / 1000.0, tz=timezone.utc
    ).isoformat()

    logger.info(
        "totalreclaw_pair(local): session %s… mode=%s port=%d",
        session.sid[:8],
        mode,
        singleton.server.port,
    )
    return url, session.secondary_code, expires_iso, session


async def _pair_relay(state: "PluginState", mode: str):
    """rc.10 relay-brokered WebSocket path.

    The relay's pair HTML page bundles the full BIP-39 English wordlist
    (2048 words) and supports all three modes natively via ``crypto.subtle``:

      - ``"generate"`` — browser creates a fresh 12-word phrase. The
        wordlist is inlined in the page; no external fetch.
      - ``"import"``   — user pastes an existing phrase.
      - ``"either"``   — pair page shows both with a tab switcher.

    The ``mode`` is passed to the relay via the ``open`` frame so the
    server-rendered HTML only shows the panel(s) the caller asked for.
    """
    from ..pair.remote_client import open_remote_pair_session

    session = await open_remote_pair_session(mode=mode)
    _spawn_relay_completion_task(session, state)

    return session.url, session.pin, session.expires_at
