"""F1 (rc.24) — detached sidecar that owns the relay-pair WebSocket
through completion, even when the parent process exits.

Background — rc.23 ship-stopper #157 (BLOCKER)
----------------------------------------------
``hermes chat -q`` is a one-shot: each turn runs in a fresh Python
process that exits as soon as the agent reply is rendered. rc.13 ran
the entire pair WebSocket lifecycle on a daemon thread inside that
process. When the process exited, the thread (and its asyncio loop +
open WebSocket) died with it; the relay saw the WS close, tore the
session down, and returned 404/502 when the browser POSTed the
encrypted phrase.

Affected paths: ``hermes chat -q``, ``hermes chat --continue``, ACP
single-call, MCP single-call, programmatic Python harness.

rc.24 fix — the "always-via-sidecar" approach
---------------------------------------------
Instead of running the WS lifecycle on a daemon thread inside the
parent process, we ALWAYS spawn a fully-detached sidecar subprocess
that owns the WS through completion. The parent process:

  1. Generates an ephemeral gateway keypair (ECDH x25519).
  2. Spawns ``python -m totalreclaw.pair.completion_sidecar`` with
     ``start_new_session=True`` (POSIX ``setsid``) so the sidecar is
     reparented to ``init`` / launchd and survives parent exit.
  3. Passes pair-session parameters via env vars (NOT argv — argv is
     visible in ``ps``).
  4. Sets up a temp file ``~/.totalreclaw/.pair_handshake_<pid>.json``
     where the sidecar writes ``{token, pin, expires_at, url}`` after
     the relay returns ``opened``.
  5. Polls that file for up to ``handshake_timeout_s`` (default 15s)
     with a coarse busy-wait. As soon as the file appears, parses it,
     deletes it, and returns the metadata to the tool body.
  6. Tool body returns ``{url, pin, ...}`` to the agent. Process exits.
  7. Sidecar continues running detached, awaits the encrypted-phrase
     forward, decrypts locally with its in-process keypair, calls
     ``state.configure(phrase)`` (which writes
     ``~/.totalreclaw/credentials.json``), acks the relay, exits.

This is a stronger lifetime guarantee than the daemon-thread approach
because the sidecar is a separate OS process — POSIX ``setsid`` plus
double-fork (or ``start_new_session`` + ``preexec_fn``) detaches it
from the parent's process group, so the parent can exit cleanly while
the sidecar keeps the WebSocket open.

Phrase-safety preserved
-----------------------
- The recovery phrase is generated/typed in the BROWSER. The sidecar
  is the only ECDH endpoint that can decrypt it; the LLM context never
  sees the phrase or its key material.
- The keypair travels parent → sidecar via env vars (one ``fork``+
  ``execve`` boundary). The keys are 32-byte x25519 — opaque to the
  parent agent's LLM stream which has long since closed by then.
- The sidecar process disappears as soon as the phrase is persisted,
  so the in-memory window for the keypair is bounded by the user's
  browser-completion latency (typically <30s).
- The handshake file carries ``{token, pin, expires_at, url}`` only —
  NOT the keypair. The keypair lives in the sidecar's memory only.

Observability
-------------
The sidecar writes structured logs to
``~/.totalreclaw/.pair_sidecar.log`` (rotating after 200 KB) so that:

- Pair successes can be correlated with relay-side ``pair.respond_*``
  events even after the parent process is long gone.
- Decrypt failures, ack failures, and credential-write failures are
  diagnosable post-hoc (the user only sees a generic browser-side
  error otherwise).
- Phrase NEVER appears in this log file.

Usage
-----
This module is normally not imported directly. ``hermes/pair_tool.py``
calls :func:`spawn_completion_sidecar` which handles the fork. The
``__main__`` entry point is what the spawned subprocess invokes.

For tests, :func:`run_sidecar_inline` runs the sidecar logic inline in
the current process (no fork) — used by the F1 regression test to
verify the "parent process exits, sidecar still completes" promise.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem layout for the handshake handoff + sidecar log.
#
# ``~/.totalreclaw`` is the same directory ``credentials.json`` lives in,
# already created with mode 0o700 by the pair flow. The handshake file
# is short-lived (parent reads + deletes within ~15s of sidecar start)
# and the sidecar log file persists for post-hoc debugging — neither
# carries phrase material.
# ---------------------------------------------------------------------------


def _totalreclaw_dir() -> Path:
    """The same dir credentials.json lives in. Create lazily 0700."""
    d = Path.home() / ".totalreclaw"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _handshake_path_for(handshake_id: str) -> Path:
    """Path of the temp handshake file the sidecar writes after ``opened``."""
    return _totalreclaw_dir() / f".pair_handshake_{handshake_id}.json"


def _sidecar_log_path() -> Path:
    """Path of the rolling sidecar log file. Capped at 200 KB on rotate."""
    return _totalreclaw_dir() / ".pair_sidecar.log"


_LOG_MAX_BYTES = 200 * 1024  # 200 KB


def _configure_sidecar_logging() -> None:
    """Configure the sidecar's logger to write to the rolling log file.

    Phrase-safety: the log handler operates at module level on
    ``totalreclaw.pair`` loggers; nothing in the codebase passes the
    decrypted phrase to ``logger.*``. We do NOT log raw envelopes or
    payload contents — only metadata (token prefix, mode, ack outcome).
    """
    log_path = _sidecar_log_path()
    # Cheap manual rotation: if the file exceeds the cap, rename to
    # ``.1`` (overwriting any earlier ``.1``). One generation is enough
    # for an end-user pair-flow audit trail.
    try:
        if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
            log_path.replace(log_path.with_suffix(log_path.suffix + ".1"))
    except OSError:
        pass

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [pair-sidecar pid=%(process)d] %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Replace any existing handlers so the sidecar doesn't double-log
    # to stdout (which is closed via ``setsid`` anyway, but defensive).
    root.handlers = [handler]


# ---------------------------------------------------------------------------
# Env-var contract between parent and sidecar.
#
# The keypair fields are 32-byte x25519 keys, base64-encoded — they are
# NOT the recovery phrase. They are ephemeral session-only material:
# the sidecar uses them to ECDH-derive the AEAD key the browser
# encrypted the phrase against.
#
# Argv is intentionally NOT used — ``ps`` exposes argv to other users on
# multi-tenant hosts. Env vars are visible only to the same UID by
# default on Linux/macOS.
# ---------------------------------------------------------------------------


_ENV_RELAY_URL = "TR_PAIR_SIDECAR_RELAY_URL"
_ENV_HANDSHAKE_ID = "TR_PAIR_SIDECAR_HANDSHAKE_ID"
_ENV_MODE = "TR_PAIR_SIDECAR_MODE"
_ENV_SERVER_URL = "TR_PAIR_SIDECAR_SERVER_URL"
_ENV_OWNER_PROBE = "TR_PAIR_SIDECAR_OWNER_PROBE"


# ---------------------------------------------------------------------------
# Handshake-file contract.
#
# After the sidecar receives ``opened`` from the relay, it writes this
# JSON shape to the handshake file. The parent polls for the file,
# parses, deletes, and returns the URL/PIN/expires_at to the tool body.
# ---------------------------------------------------------------------------


@dataclass
class _HandshakeRecord:
    """Tiny JSON record handed off via temp file. NO key material."""

    url: str
    pin: str
    expires_at: str
    token: str
    # Status: "opened" on success, "error" with ``error_message`` on relay
    # rejection (so the parent can surface a clean error to the agent
    # rather than time out).
    status: str
    error_message: str = ""


# ---------------------------------------------------------------------------
# Parent side: spawn the sidecar + wait for handshake.
# ---------------------------------------------------------------------------


def _python_executable() -> str:
    """Return the interpreter the sidecar should run under.

    Prefer ``sys.executable`` so the sidecar uses the SAME venv the
    parent does (the venv is what has ``totalreclaw`` + its deps
    installed). Fall back to ``python3`` only if ``sys.executable`` is
    somehow empty (frozen-binary corner cases).
    """
    return sys.executable or "python3"


def spawn_completion_sidecar(
    *,
    mode: Optional[str],
    relay_url: Optional[str] = None,
    server_url: Optional[str] = None,
    handshake_timeout_s: float = 15.0,
) -> _HandshakeRecord:
    """Fork a detached sidecar; block until it reports ``opened``.

    Returns
    -------
    _HandshakeRecord
        The pair-session metadata to surface back to the agent.

    Raises
    ------
    RuntimeError
        If the sidecar does not return ``opened`` within
        ``handshake_timeout_s`` OR if the sidecar reports an error.
    """
    handshake_id = uuid.uuid4().hex
    handshake_path = _handshake_path_for(handshake_id)
    # Defensive: clear any stale file from a prior crashed sidecar.
    try:
        handshake_path.unlink()
    except FileNotFoundError:
        pass

    env = os.environ.copy()
    env[_ENV_HANDSHAKE_ID] = handshake_id
    if mode in ("generate", "import", "either"):
        env[_ENV_MODE] = mode
    if relay_url:
        env[_ENV_RELAY_URL] = relay_url
    if server_url:
        env[_ENV_SERVER_URL] = server_url
    # Carry through TOTALRECLAW_SERVER_URL / TOTALRECLAW_PAIR_RELAY_URL
    # explicitly even if the caller passed nothing — the sidecar runs
    # detached and inherits the parent env, but ``subprocess.Popen``
    # already does that; this branch is a no-op as long as we don't
    # override.

    cmd = [
        _python_executable(),
        "-m",
        "totalreclaw.pair.completion_sidecar",
        "--run",
    ]

    # Detach the child:
    #   - close_fds defaults to True on POSIX in 3.7+, so no parent FDs
    #     leak into the child beyond stdin/stdout/stderr (which we
    #     redirect to /dev/null below).
    #   - start_new_session=True puts the child in its own session/PG
    #     so a SIGHUP on the parent's terminal doesn't propagate.
    #   - stdin/stdout/stderr go to /dev/null because the parent will
    #     exit; without redirect, writing to closed stdout would crash
    #     the sidecar at the first log call.
    devnull_in = subprocess.DEVNULL
    devnull_out = subprocess.DEVNULL
    popen_kwargs: dict[str, Any] = {
        "env": env,
        "stdin": devnull_in,
        "stdout": devnull_out,
        "stderr": devnull_out,
        "close_fds": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        # Windows: detach via DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP.
        # Hermes's primary deployment surface is Linux/macOS containers;
        # this branch is best-effort.
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200  # type: ignore[assignment]

    logger.info(
        "pair.sidecar_spawn handshake=%s mode=%s relay=%s",
        handshake_id[:8],
        mode or "either",
        bool(relay_url),
    )
    try:
        subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — argv is internal
    except OSError as err:
        raise RuntimeError(
            f"pair.sidecar: failed to spawn completion sidecar: {err}"
        ) from err

    # Poll the handshake file. Coarse busy-wait — typical latency from
    # spawn → relay-opened is well under 1s on a healthy network.
    deadline = time.monotonic() + handshake_timeout_s
    while time.monotonic() < deadline:
        try:
            raw = handshake_path.read_bytes()
        except FileNotFoundError:
            time.sleep(0.05)
            continue

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Sidecar is mid-write — wait one more tick.
            time.sleep(0.05)
            continue

        # Best-effort cleanup once we've parsed it. The sidecar already
        # closed its handle to the file before writing complete.
        try:
            handshake_path.unlink()
        except OSError:
            pass

        try:
            record = _HandshakeRecord(**payload)
        except TypeError as err:
            raise RuntimeError(
                f"pair.sidecar: handshake payload malformed: {err!r}"
            ) from err

        if record.status == "error":
            raise RuntimeError(
                f"pair.sidecar: relay open failed: {record.error_message}"
            )
        if record.status != "opened":
            raise RuntimeError(
                f"pair.sidecar: unexpected handshake status {record.status!r}"
            )
        return record

    # Timeout. The sidecar may still be alive — leave it running; it
    # will time out on its own at the relay's 5-minute TTL. We just
    # surface a clean error to the agent.
    raise RuntimeError(
        f"pair.sidecar: handshake did not complete within "
        f"{handshake_timeout_s:.0f}s — sidecar may have crashed; "
        f"check {_sidecar_log_path()}"
    )


# ---------------------------------------------------------------------------
# Sidecar side: open relay session, wait for forward, configure state.
# ---------------------------------------------------------------------------


async def _drive_full_pair_session(
    *,
    handshake_id: str,
    mode: Optional[str],
    relay_url: Optional[str],
) -> None:
    """The end-to-end pair flow that runs INSIDE the detached sidecar.

    Mirrors :func:`hermes.pair_tool._run_relay_pair_on_thread.
    _drive_full_session` but writes the handshake metadata to a file
    (instead of a thread queue) so the parent can read+exit independently.
    """
    # Local imports — keep the sidecar's startup cheap. These dependencies
    # only matter inside the sidecar.
    from totalreclaw.pair.remote_client import (
        await_phrase_upload,
        open_remote_pair_session,
    )

    handshake_path = _handshake_path_for(handshake_id)

    def _atomic_write_handshake(record: _HandshakeRecord) -> None:
        """Write+rename so the parent never reads a half-written JSON.

        Uses a temp file in the same directory and ``os.replace`` for an
        atomic crossover. The parent's poller is tolerant of a missing
        file but NOT of a half-written one (json.JSONDecodeError races).
        """
        tmp_dir = handshake_path.parent
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(tmp_dir),
            delete=False,
        ) as fh:
            json.dump(asdict(record), fh)
            tmp = Path(fh.name)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, handshake_path)

    # 1. Open the relay session.
    try:
        session = await open_remote_pair_session(
            relay_base_url=relay_url,
            mode=mode,
        )
    except BaseException as err:  # noqa: BLE001 — we want any failure shape
        logger.warning(
            "pair.sidecar: open_remote_pair_session failed: %r", err
        )
        # Tell the parent so it can surface a clean error to the agent.
        _atomic_write_handshake(
            _HandshakeRecord(
                url="",
                pin="",
                expires_at="",
                token="",
                status="error",
                error_message=str(err),
            )
        )
        return

    token_tag = session.token[:8] if session.token else "?"
    logger.info(
        "pair.sidecar: relay opened token=%s… mode=%s", token_tag, mode or "either"
    )

    # 2. Tell the parent the metadata. After this point the parent is
    #    free to exit; the sidecar stays alive.
    _atomic_write_handshake(
        _HandshakeRecord(
            url=session.url,
            pin=session.pin,
            expires_at=session.expires_at,
            token=session.token,
            status="opened",
        )
    )

    # 3. Wait for the encrypted-phrase forward, decrypt, persist.
    async def _complete_pairing(phrase: str) -> dict:
        # Local import to avoid pulling agent + client modules into
        # spawn-time path.
        from totalreclaw.agent.state import AgentState

        state = AgentState()
        try:
            state.configure(phrase)
            client = state.get_client()
            eoa = getattr(client, "_eoa_address", None)
            logger.info(
                "pair.sidecar: state.configure ok token=%s… eoa=%s",
                token_tag,
                eoa or "unknown",
            )
            return {"state": "active", "account_id": eoa}
        except Exception as err:
            logger.error(
                "pair.sidecar: state.configure failed token=%s… err=%r",
                token_tag,
                err,
            )
            return {"state": "error", "error": str(err)}

    try:
        result = await await_phrase_upload(
            session,
            complete_pairing=_complete_pairing,
        )
        logger.info(
            "pair.sidecar: completion done token=%s… outcome=ok state=%s",
            token_tag,
            result.get("state") if isinstance(result, dict) else "unknown",
        )
    except Exception as err:
        logger.warning(
            "pair.sidecar: completion done token=%s… outcome=error err=%r",
            token_tag,
            err,
        )


def run_sidecar_inline(
    *,
    handshake_id: str,
    mode: Optional[str],
    relay_url: Optional[str],
) -> None:
    """Run the sidecar logic in-process (no fork). Used by tests.

    The CLI entry point (``__main__``) wraps this with logging
    configuration; the test path uses it directly so it can capture
    logs/state changes without crossing a process boundary.
    """
    asyncio.run(
        _drive_full_pair_session(
            handshake_id=handshake_id,
            mode=mode,
            relay_url=relay_url,
        )
    )


# ---------------------------------------------------------------------------
# Module entry point — invoked as ``python -m totalreclaw.pair.completion_sidecar``.
# ---------------------------------------------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="totalreclaw.pair.completion_sidecar")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the relay completion loop (default action when invoked).",
    )
    args = parser.parse_args(argv)
    if not args.run:
        # Invoked without --run: be quiet (this is a programmatic entry
        # point, not a user-facing CLI).
        return 0

    _configure_sidecar_logging()

    handshake_id = os.environ.get(_ENV_HANDSHAKE_ID, "")
    if not handshake_id:
        logger.error(
            "pair.sidecar: %s missing — bailing out (parent contract violation)",
            _ENV_HANDSHAKE_ID,
        )
        return 2

    mode = os.environ.get(_ENV_MODE) or None
    relay_url = os.environ.get(_ENV_RELAY_URL) or None

    try:
        asyncio.run(
            _drive_full_pair_session(
                handshake_id=handshake_id,
                mode=mode,
                relay_url=relay_url,
            )
        )
    except KeyboardInterrupt:
        logger.info("pair.sidecar: SIGINT — aborting")
        return 130
    except Exception as err:
        logger.exception("pair.sidecar: unhandled error: %r", err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
