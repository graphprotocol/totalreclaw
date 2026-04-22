"""Hermes agent tool: ``totalreclaw_pair``.

Shipped in 2.3.1rc4 as part of the phrase-safety hardening. Called by
the Hermes agent when the user asks to set up TotalReclaw. Spins up (or
reuses) a local-loopback HTTP pair server, creates a session, and
returns ``{url, pin, expires_at}`` to the agent — no phrase-adjacent
data. The agent relays the URL + PIN to the user, who completes the
flow in their browser. The recovery phrase NEVER crosses the LLM
context.

See ``~/.claude/projects/-Users-pdiogo-Documents-code-totalreclaw-
internal/memory/project_phrase_safety_rule.md`` for the governing rule.
"""
from __future__ import annotations

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
# Schema
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
                "enum": ["generate", "import"],
                "description": (
                    '"generate" = the browser will create a NEW 12-word '
                    'recovery phrase. "import" = the user pastes an '
                    "EXISTING phrase in the browser (never in this chat)."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Module-singleton pair-HTTP server (lazy)
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
    """Default to ``~/.totalreclaw`` — the same dir credentials.json lives in.

    Kept as a helper so tests can monkeypatch ``pathlib.Path.home``.
    """
    return Path.home() / ".totalreclaw"


def _complete_pairing_handler(state: "PluginState"):
    """Closure that writes credentials.json + configures state.

    ``state.configure(phrase)`` is the same code path that
    ``tools.setup`` used in rc.3. We reuse it because it handles the EOA
    derivation + credentials write atomically. The critical difference:
    this path runs INSIDE the HTTP handler after the browser has
    end-to-end-encrypted the phrase. The agent never saw the phrase, and
    this handler's return payload to the browser contains NO phrase data.
    """
    from .pair_tool_completion import complete_pairing

    def _handler(phrase: str, session) -> Any:
        return complete_pairing(phrase, session, state)

    return _handler


def _get_or_build_server(state: "PluginState"):
    """Return the module-singleton pair server; build on first call."""
    global _SERVER_INSTANCE
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is not None:
            return _SERVER_INSTANCE

        # Late import to avoid pulling cryptography into module load if
        # the tool is never called (e.g. on a stable build that hasn't
        # shipped this tool yet).
        from ..pair import build_pair_http_server, default_pair_sessions_path
        from ..pair.http_server import PairHttpConfig

        base_dir = _resolve_sessions_dir()
        base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sessions_path = default_pair_sessions_path(base_dir)

        # Bind host: 127.0.0.1 by default. Operators running Hermes in
        # Docker need to publish the port OR SSH-tunnel; those paths are
        # documented in the setup guides. No LAN bind.
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
# Tool handler
# ---------------------------------------------------------------------------


async def pair(args: dict, state: "PluginState", **kwargs) -> str:
    """Agent-callable handler for ``totalreclaw_pair``.

    Returns a JSON-encoded ``{url, pin, expires_at}`` payload. NO phrase
    data crosses this return value.
    """
    raw_mode = args.get("mode", "generate")
    mode = "import" if raw_mode == "import" else "generate"

    try:
        from ..pair import (
            create_pair_session,
            generate_gateway_keypair,
        )

        singleton = _get_or_build_server(state)
        singleton.ensure_started()

        kp = generate_gateway_keypair()
        session = create_pair_session(
            singleton.sessions_path,
            mode=mode,
            sk_b64=kp.sk_b64,
            pk_b64=kp.pk_b64,
        )

        base_url = singleton.server.url_for(session.sid)
        # The pubkey goes in the fragment so it never hits server logs.
        url = f"{base_url}#pk={kp.pk_b64}"

        expires_iso = datetime.fromtimestamp(
            session.expires_at_ms / 1000.0, tz=timezone.utc
        ).isoformat()

        logger.info(
            "totalreclaw_pair: session %s… mode=%s port=%d",
            session.sid[:8],
            mode,
            singleton.server.port,
        )

        return json.dumps(
            {
                "url": url,
                "pin": session.secondary_code,
                "expires_at": expires_iso,
                "mode": mode,
                "instructions": (
                    f"Relay these to the user verbatim:\n"
                    f"1. Open {url} in your browser.\n"
                    f"2. Enter PIN {session.secondary_code} when asked.\n"
                    f"3. "
                    + (
                        "The browser generates a new 12-word recovery phrase. "
                        "Write it down BEFORE confirming — the phrase is "
                        "unrecoverable if lost.\n"
                        if mode == "generate"
                        else "Paste your existing 12 or 24-word recovery phrase "
                        "in the browser (never in this chat).\n"
                    )
                    + f"4. The encrypted phrase uploads to this gateway; it never crosses "
                    f"this chat.\n"
                    f"5. Come back to chat once the browser says 'Pairing complete'. "
                    f"Restart the Hermes gateway so the plugin picks up the new "
                    f"credentials."
                ),
            }
        )
    except Exception as err:
        logger.error("totalreclaw_pair failed: %s", err)
        return json.dumps({"error": f"Failed to start pairing session: {err}"})
