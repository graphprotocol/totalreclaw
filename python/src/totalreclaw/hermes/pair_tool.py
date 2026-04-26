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
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Relay-session handshake result — returned from the worker thread to the
# tool body once the relay has acknowledged ``session/open``. Kept tiny so
# we can push it through a threading.Queue without risking phrase material.
# ---------------------------------------------------------------------------


@dataclass
class _OpenedSession:
    """Metadata the tool needs to return to the agent."""

    url: str
    pin: str
    expires_at: str


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
# Relay-mode: a background THREAD runs the entire pair session.
#
# rc.13 asyncio-lifecycle fix. Earlier RCs (rc.10–rc.12) opened the relay
# WebSocket on the Hermes tool-invocation loop, then used
# ``loop.create_task(...)`` to keep waiting on it after the tool
# returned. Hermes tears that loop down as soon as the tool returns, so
# the background task was destroyed mid-``ws.recv()`` — logged as::
#
#     Task was destroyed but it is pending!
#     RuntimeError: no running event loop
#
# during ``WebSocketCommonProtocol.close_connection``. The relay saw no
# ack, timed out at 15s, and returned 502 to the browser.
#
# Fix (Option 2 from the rc.13 design notes): run the ENTIRE pair
# session — open + opened + forward + decrypt + configure + ack + close
# — on a dedicated OS thread that owns its own ``asyncio`` event loop.
# The WebSocket is created INSIDE the thread loop, not on the caller
# loop, so it is never bound to a loop that closes.
#
# The tool body still needs ``{url, pin, expires_at}`` to return to the
# agent synchronously. We use a small ``queue.Queue`` handshake: the
# thread runs ``open_remote_pair_session`` to completion, pushes the
# metadata, then proceeds to ``await_phrase_upload`` without the tool
# body being involved. The tool blocks briefly (a few hundred ms — just
# the TCP / TLS handshake + one WS round-trip) on the queue before
# returning.
#
# Why NOT Option 1 (block synchronously up to the 5-minute TTL):
#   Option 1 would pin the user's chat for up to 5 minutes while the
#   browser flow runs. The whole point of the pair tool returning
#   ``{url, pin}`` fast is so the agent can relay those to the user
#   verbatim and the user can go open the URL without the chat
#   stalling.
#
# Why NOT Option 3 (plugin lifecycle hook holding a long-lived task):
#   Hermes's plugin runtime doesn't expose a persistent background-task
#   hook. Option 3 would have required hermes-agent core changes,
#   which is out of rc.13 scope (gateway-side hotfix).
#
# Observability: the worker thread emits structured lifecycle events so
# the next layer of failure (decrypt error, credential-write error, ack
# send error) is debuggable at a glance in ``docker compose logs``:
#
#   - pair.relay_completion_started  → waiter thread spawned
#   - pair.relay_opened              → relay ACK'd session/open
#   - pair.relay_decrypt_ok / _failed → decrypt + complete-pairing
#   - pair.relay_ack_sent / _failed  → ack frame written to WS
#   - pair.relay_completion_done     → thread exit (with outcome)
# ---------------------------------------------------------------------------


# Queue sentinel — pushed by the worker thread if ``open_remote_pair_session``
# raises before it can report a URL+PIN back. Carries the exception so
# the tool body can re-raise it to the agent.
@dataclass
class _OpenFailed:
    error: BaseException


def _run_relay_pair_on_thread(
    state: "PluginState",
    mode: Optional[str],
    handshake_timeout_s: float = 15.0,
) -> _OpenedSession:
    """Spawn a worker thread that runs the FULL relay pair session.

    Blocks the caller only until the relay returns ``opened``. After
    that, the thread runs ``await_phrase_upload`` independently and the
    tool body is free to return to the agent.

    Returns ``_OpenedSession(url, pin, expires_at)``. Raises on
    relay open failure (rate-limit, bad gateway, etc).
    """
    from ..pair.remote_client import await_phrase_upload, open_remote_pair_session

    handshake_q: "queue.Queue[Any]" = queue.Queue(maxsize=1)

    # Thread name uses the short relay host so the thread is identifiable
    # in ``pstack`` / py-spy dumps. Will be appended with the token once
    # the relay opens the session.
    thread_name = "totalreclaw-pair-relay"

    def _configure_from_phrase(token_tag: str, phrase: str) -> dict:
        """Synchronous credential write — runs on the thread's executor."""
        try:
            state.configure(phrase)
            client = state.get_client()
            eoa = getattr(client, "_eoa_address", None)
            logger.info(
                "pair.relay_decrypt_ok token=%s… eoa=%s",
                token_tag,
                eoa or "unknown",
            )
            return {"state": "active", "account_id": eoa}
        except Exception as err:
            logger.error(
                "pair.relay_decrypt_failed token=%s… stage=configure err=%r",
                token_tag,
                err,
            )
            return {"state": "error", "error": str(err)}

    def _thread_main() -> None:
        logger.info("pair.relay_completion_started")
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)

            async def _drive_full_session() -> None:
                """Open + opened + forward + decrypt + ack + close — all
                under a SINGLE ``run_until_complete`` on the thread loop.

                Splitting this across two ``run_until_complete`` calls
                (open first, then await_phrase_upload) leaves the WS
                protocol's keepalive tasks suspended between calls;
                websockets then sends close 1001 (going away) when the
                second run_until_complete starts, so ack fails. Keeping
                it in one coroutine holds the loop in continuous drive.
                """
                token_tag_local = "?"
                try:
                    session = await open_remote_pair_session(mode=mode)
                except BaseException as err:
                    handshake_q.put(_OpenFailed(err))
                    return

                token_tag_local = session.token[:8] if session.token else "?"
                logger.info(
                    "pair.relay_opened token=%s… url_host=%s",
                    token_tag_local,
                    _safe_host(session.url),
                )
                handshake_q.put(
                    _OpenedSession(
                        url=session.url,
                        pin=session.pin,
                        expires_at=session.expires_at,
                    )
                )

                async def _complete_pairing_async(phrase: str) -> dict:
                    return await loop.run_in_executor(
                        None, _configure_from_phrase, token_tag_local, phrase
                    )

                try:
                    result = await await_phrase_upload(
                        session,
                        complete_pairing=_complete_pairing_async,
                    )
                    logger.info(
                        "pair.relay_completion_done token=%s… outcome=ok state=%s",
                        token_tag_local,
                        result.get("state") if isinstance(result, dict) else "unknown",
                    )
                except Exception as err:
                    logger.warning(
                        "pair.relay_completion_done token=%s… outcome=error err=%r",
                        token_tag_local,
                        err,
                    )

            loop.run_until_complete(_drive_full_session())
        finally:
            # Drain stray tasks so websockets' close_connection coroutine
            # doesn't emit a "task was destroyed but pending" warning on
            # loop close — the same class of log line that gave rc.12
            # away.
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(
        target=_thread_main,
        name=thread_name,
        daemon=True,
    )
    t.start()

    # Block the tool body until the relay answers ``opened`` — or a
    # 15-second ceiling elapses (same as the relay's own respond-side
    # timeout, so we don't accidentally hold the tool open longer than
    # the browser will).
    try:
        handshake = handshake_q.get(timeout=handshake_timeout_s)
    except queue.Empty:
        raise RuntimeError(
            "pair.relay: handshake did not complete within "
            f"{handshake_timeout_s:.0f}s — relay unreachable?"
        ) from None

    if isinstance(handshake, _OpenFailed):
        raise handshake.error  # propagate the open-time error to the agent

    return handshake


def _safe_host(url: str) -> str:
    """Extract the host portion of ``url`` for logging.

    Strips scheme + path + fragment — the fragment carries the gateway
    pubkey, which we never want in logs even though it's public.
    """
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or "?"
    except Exception:
        return "?"


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

    rc.13 asyncio-lifecycle fix: the WebSocket + completion waiter ran
    on a dedicated worker thread inside the Hermes process. That fix
    held for long-lived ``hermes gateway run`` daemons but DID NOT
    survive ``hermes chat -q`` / ACP single-call / MCP single-call /
    Python harness — those all exit the Python process the moment the
    agent reply lands, killing the daemon thread (and its WebSocket)
    before the user can finish the browser flow. The relay then saw
    the WS close, tore the session down, and returned 404/502 for the
    eventual phrase POST.

    rc.24 (F1, ref ``QA-hermes-RC-2.3.1-rc.23-20260426.md`` Finding
    #157): always hand the WebSocket lifecycle off to a fully-detached
    sidecar SUBPROCESS (POSIX ``setsid``) before returning. The
    sidecar lives past the parent's exit because it's been reparented
    to ``init`` / ``launchd``, so a one-shot agent process can finish
    its turn while the sidecar holds the relay session open through
    the user's browser-completion latency. See
    :mod:`totalreclaw.pair.completion_sidecar` for the full rationale.

    The ``TOTALRECLAW_PAIR_SIDECAR=0`` env var falls back to the rc.13
    daemon-thread path. This is intentionally undocumented — it's an
    operator escape hatch for environments that block subprocess spawn
    (some sandboxes), not a user-facing knob. Setting it on a one-shot
    process re-introduces the rc.23 NO-GO bug; do not use casually.
    """
    if _sidecar_enabled():
        # Run the spawn in a worker thread so the (synchronous) Popen
        # + handshake-poll doesn't block the asyncio loop. The whole
        # call is fast (<1s typical) since the sidecar reports back as
        # soon as the relay returns ``opened``.
        record = await asyncio.to_thread(_run_relay_pair_via_sidecar, mode)
        return record.url, record.pin, record.expires_at

    # rc.13 fallback path — daemon-thread inside the parent process.
    opened = await asyncio.to_thread(_run_relay_pair_on_thread, state, mode)
    return opened.url, opened.pin, opened.expires_at


def _sidecar_enabled() -> bool:
    """rc.24 default ON. Set ``TOTALRECLAW_PAIR_SIDECAR=0`` to disable.

    Disabling re-exposes the rc.23 lifecycle bug for short-lived
    process invocations. Only sensible for long-lived daemon hosts
    that explicitly want the daemon-thread path back (e.g. environments
    where subprocess spawn is blocked by a sandbox).
    """
    raw = (os.environ.get("TOTALRECLAW_PAIR_SIDECAR") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _run_relay_pair_via_sidecar(mode: Optional[str]):
    """Sync helper — spawns the sidecar and waits for its handshake.

    Returns the
    :class:`totalreclaw.pair.completion_sidecar._HandshakeRecord` so
    the caller can pull ``url`` / ``pin`` / ``expires_at`` off it.

    rc.24 (F1) — see module docstring for design rationale.
    """
    # Local import: keeps cold-start path light; sidecar module pulls
    # in subprocess + asyncio + uuid which the regular tool body
    # doesn't need.
    from ..pair.completion_sidecar import spawn_completion_sidecar

    # Forward operator overrides the user might have set in env so the
    # sidecar talks to the same relay + relay endpoint the parent would
    # have used. ``subprocess.Popen(env=...)`` already inherits these
    # by default; passing them through ``spawn_completion_sidecar``
    # keeps the contract explicit and lets tests monkeypatch the env.
    relay_url = os.environ.get("TOTALRECLAW_PAIR_RELAY_URL")
    server_url = os.environ.get("TOTALRECLAW_SERVER_URL")
    return spawn_completion_sidecar(
        mode=mode,
        relay_url=relay_url,
        server_url=server_url,
    )
