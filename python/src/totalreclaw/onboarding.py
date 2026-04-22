"""First-run onboarding detection + welcome copy for TotalReclaw.

This module is the Python-side parity surface for the TypeScript plugin
3.3.0 first-run UX. Users switching between OpenClaw (plugin) and Hermes
(Python) must see the same welcome text, the same branch question, and
the canonical ``recovery phrase`` terminology — no ``mnemonic`` leakage,
no ``seed phrase`` / ``recovery code`` / ``recovery key`` divergence.

What this module owns:

* :func:`detect_first_run` — cheap, synchronous, no-network probe that
  returns ``True`` when the canonical credentials file is missing,
  empty, unparseable, or doesn't carry a recognised credentials key.
* :func:`build_welcome_message` — renders the welcome + branch-question
  copy for either the local or the remote invocation context.
* Module-level copy constants (``WELCOME_MESSAGE``, ``BRANCH_QUESTION``,
  ``STORAGE_GUIDANCE``, …) — exported verbatim so tests can assert on
  them and the CLI / plugin layers can render them without duplicating
  strings.
* :data:`CANONICAL_CREDENTIALS_PATH` — the ``~/.totalreclaw/credentials.json``
  path shared across Hermes, OpenClaw, and MCP.
* :func:`maybe_emit_welcome` — emits the welcome once-per-process. Uses
  a module-level flag (``_emitted_this_process``) plus a best-effort
  sentinel file so repeat imports within the same session stay quiet.

What this module deliberately does NOT do:

* Never prints or logs the recovery phrase itself — welcome copy only.
* No network calls. No env var reads beyond what's needed to classify
  local-vs-remote mode.
* No blocking prompts. The wizard (``hermes setup``) lives in
  :mod:`totalreclaw.hermes.cli`; this module only emits the welcome
  surface + detection helper.

Added 2.3.1 (2026-04-20).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional, TextIO

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical paths + mode detection
# ---------------------------------------------------------------------------

#: The single path every TotalReclaw client (Python, TS plugin, MCP)
#: reads + writes credentials to. Keep in sync with
#: ``agent/state.py::_try_auto_configure`` and the plugin's
#: ``fs-helpers.ts::credentialsPath``.
CANONICAL_CREDENTIALS_PATH: Path = Path.home() / ".totalreclaw" / "credentials.json"

#: Sentinel file written on first welcome emission. Lives in the same
#: ``~/.totalreclaw`` dir as credentials, so users who wipe credentials
#: (to re-onboard) also wipe the sentinel and re-see the welcome.
_WELCOME_SENTINEL_PATH: Path = Path.home() / ".totalreclaw" / ".welcome_shown"

# Module-level flag so a Hermes runtime that imports both the client and
# the plugin doesn't emit the welcome twice per process.
_emitted_this_process: bool = False


# ---------------------------------------------------------------------------
# Canonical copy (verbatim — tests assert on these strings byte-for-byte)
# ---------------------------------------------------------------------------

WELCOME_MESSAGE = """
Welcome to TotalReclaw — encrypted, agent-portable memory.

Your memories are stored end-to-end encrypted and on-chain. You can restore them on any agent — OpenClaw, Hermes, or NanoClaw — with a single recovery phrase.
""".strip()

BRANCH_QUESTION = """
Let's set up your account. Do you already have a recovery phrase, or should we generate a new one?
""".strip()

LOCAL_MODE_INSTRUCTIONS = "Run: totalreclaw setup  (or: hermes setup)"

REMOTE_MODE_INSTRUCTIONS = """
Run: totalreclaw setup  (or: hermes setup if you're on a Hermes-specific install)

You'll be prompted to either restore from an existing recovery phrase or generate a new one. Your phrase never leaves this machine.
""".strip()

STORAGE_GUIDANCE = """
Your recovery phrase is 12 words. Store it somewhere safe — a password manager works well. Use it only for TotalReclaw. Don't reuse it anywhere else. Don't put funds on it.
""".strip()

RESTORE_PROMPT = "Enter your 12-word recovery phrase to restore your account:"

GENERATED_CONFIRMATION = """
A new recovery phrase has been generated. Write it down now, somewhere safe. This is the only way to restore your account later.
""".strip()


# Accepted credentials key names. Matches
# ``agent/state.py::_extract_mnemonic_from_creds`` — canonical key is
# ``mnemonic`` (plugin 3.2.0+), legacy Python clients wrote
# ``recovery_phrase``; both count as "already onboarded" for first-run
# detection.
_CREDENTIAL_KEYS = ("mnemonic", "recovery_phrase")


# ---------------------------------------------------------------------------
# First-run detection
# ---------------------------------------------------------------------------


def detect_first_run(credentials_path: Optional[Path] = None) -> bool:
    """Return True when the given credentials path doesn't identify an
    onboarded user.

    Treats the following as first-run (returns ``True``):

    * File does not exist.
    * File exists but is empty.
    * File exists but isn't valid JSON.
    * File is valid JSON but isn't an object (e.g. ``[]``, ``"x"``).
    * File is a JSON object but doesn't carry a non-empty string under
      ``mnemonic`` OR ``recovery_phrase``.

    Returns ``False`` only when the file parses to a dict and carries at
    least one non-empty string credential key — i.e. the user has
    already been onboarded.

    Never raises — any filesystem/JSON error collapses to ``True`` so
    the caller defaults to the "show welcome + suggest setup" path.

    Parameters
    ----------
    credentials_path : Path, optional
        Path to the credentials file. Defaults to
        :data:`CANONICAL_CREDENTIALS_PATH`.
    """
    path = credentials_path if credentials_path is not None else CANONICAL_CREDENTIALS_PATH

    try:
        if not path.exists():
            return True
    except OSError:
        return True

    try:
        raw = path.read_text()
    except OSError:
        return True

    if not raw.strip():
        return True

    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return True

    if not isinstance(parsed, dict):
        return True

    for key in _CREDENTIAL_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return False

    return True


# ---------------------------------------------------------------------------
# Local / remote mode detection
# ---------------------------------------------------------------------------


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def detect_mode(relay_url: Optional[str] = None) -> Literal["local", "remote"]:
    """Classify whether Hermes is running against a local or remote gateway.

    Order:
      1. If ``relay_url`` is passed, parse its host and return ``local`` if
         it matches a loopback host, else ``remote``.
      2. Else if ``TOTALRECLAW_LOCAL_GATEWAY`` or ``HERMES_LOCAL_GATEWAY``
         env vars are truthy (``1``, ``true``, ``yes``), return ``local``.
      3. Else read ``TOTALRECLAW_SERVER_URL`` / ``TOTALRECLAW_RELAY_URL``
         from env and apply the same host classification.
      4. Default to ``remote``.
    """
    def _classify_url(url: str) -> Literal["local", "remote"]:
        u = url.strip().lower()
        if not u:
            return "remote"
        # crude host extraction — good enough to spot loopback hosts.
        try:
            from urllib.parse import urlparse
            parsed = urlparse(u if "://" in u else f"http://{u}")
            host = (parsed.hostname or "").lower()
        except Exception:
            return "remote"
        if host in _LOCAL_HOSTS:
            return "local"
        return "remote"

    if relay_url:
        return _classify_url(relay_url)

    for flag_var in ("TOTALRECLAW_LOCAL_GATEWAY", "HERMES_LOCAL_GATEWAY"):
        raw = os.environ.get(flag_var, "").strip().lower()
        if raw in ("1", "true", "yes"):
            return "local"

    for url_var in ("TOTALRECLAW_SERVER_URL", "TOTALRECLAW_RELAY_URL"):
        env_url = os.environ.get(url_var)
        if env_url:
            return _classify_url(env_url)

    return "remote"


# ---------------------------------------------------------------------------
# Welcome message rendering
# ---------------------------------------------------------------------------


def build_welcome_message(mode: Literal["local", "remote"]) -> str:
    """Return the full welcome + branch-question copy for the given mode.

    Both modes render the same welcome + branch question header; the
    call-to-action differs (local users get a one-line ``Run: hermes
    setup``; remote users get a longer note on the phrase-never-leaves
    guarantee).

    Parameters
    ----------
    mode : {"local", "remote"}
        Invocation context. Pass the result of :func:`detect_mode`, or
        override for tests.
    """
    if mode not in ("local", "remote"):
        raise ValueError(f"mode must be 'local' or 'remote', got {mode!r}")

    instructions = LOCAL_MODE_INSTRUCTIONS if mode == "local" else REMOTE_MODE_INSTRUCTIONS

    # Two blank lines between each section for readability in a terminal.
    return (
        f"{WELCOME_MESSAGE}\n\n"
        f"{BRANCH_QUESTION}\n\n"
        f"{instructions}"
    )


# ---------------------------------------------------------------------------
# Once-per-process welcome emission
# ---------------------------------------------------------------------------


def maybe_emit_welcome(
    credentials_path: Optional[Path] = None,
    relay_url: Optional[str] = None,
    stream: Optional[TextIO] = None,
    *,
    use_sentinel: bool = True,
) -> bool:
    """Emit the welcome message to ``stream`` iff this is first-run + we
    haven't emitted in this process yet.

    Returns ``True`` if the welcome was emitted, ``False`` otherwise
    (already onboarded, already emitted in this process, or
    sentinel-suppressed).

    Suppression strategy:

    * Per-process: a module-level flag. Prevents double-emission when
      both the client and the Hermes plugin import the onboarding
      module during the same session.
    * Per-host (best-effort): a sentinel file at
      ``~/.totalreclaw/.welcome_shown``. Prevents re-emission on every
      command invocation for a first-run user who's chosen to defer
      setup. Pass ``use_sentinel=False`` to disable (useful for tests).

    Never emits the recovery phrase itself. Only ever writes the
    welcome + branch-question copy + the instructions for the detected
    mode.
    """
    global _emitted_this_process

    if _emitted_this_process:
        return False

    if not detect_first_run(credentials_path):
        return False

    if use_sentinel:
        try:
            if _WELCOME_SENTINEL_PATH.exists():
                _emitted_this_process = True  # still mark to avoid repeat probes
                return False
        except OSError:
            pass

    mode = detect_mode(relay_url)
    message = build_welcome_message(mode)

    out = stream if stream is not None else _default_stream()
    try:
        out.write(message + "\n")
        try:
            out.flush()
        except Exception:
            pass
    except Exception:
        # Never block the client on a broken stdout — just log and move on.
        logger.debug("totalreclaw.onboarding: welcome write failed", exc_info=True)
        return False

    _emitted_this_process = True

    if use_sentinel:
        try:
            _WELCOME_SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            _WELCOME_SENTINEL_PATH.write_text("")
            try:
                _WELCOME_SENTINEL_PATH.chmod(0o600)
            except OSError:
                pass
        except OSError:
            # Read-only home dir / perms — tolerate and rely on the
            # per-process flag alone.
            logger.debug("totalreclaw.onboarding: sentinel write failed", exc_info=True)

    return True


def _default_stream() -> TextIO:
    """Return ``sys.stdout`` — indirected so tests can monkeypatch."""
    import sys
    return sys.stdout


def _reset_for_tests() -> None:
    """Reset the module-level emission flag. Test-only helper."""
    global _emitted_this_process
    _emitted_this_process = False


__all__ = [
    "WELCOME_MESSAGE",
    "BRANCH_QUESTION",
    "LOCAL_MODE_INSTRUCTIONS",
    "REMOTE_MODE_INSTRUCTIONS",
    "STORAGE_GUIDANCE",
    "RESTORE_PROMPT",
    "GENERATED_CONFIRMATION",
    "CANONICAL_CREDENTIALS_PATH",
    "detect_first_run",
    "detect_mode",
    "build_welcome_message",
    "maybe_emit_welcome",
]
