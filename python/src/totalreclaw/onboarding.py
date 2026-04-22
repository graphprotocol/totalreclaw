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
* :func:`maybe_emit_welcome` — historically emitted a first-run welcome
  banner to stdout. **As of 2.3.1rc9 this is a no-op by default** (see
  "Banner suppression" below). The signature is preserved for
  backwards-compat with existing call sites.

What this module deliberately does NOT do:

* Never prints or logs the recovery phrase itself — welcome copy only.
* No network calls. No env var reads beyond what's needed to classify
  local-vs-remote mode.
* No blocking prompts. Agent-driven setup runs through
  ``totalreclaw_pair`` (browser-side crypto, phrase-safe); user-in-
  terminal setup runs through the ``totalreclaw setup`` CLI wizard
  OUTSIDE any agent context.

Banner suppression (2.3.1rc9):
    Earlier RCs wrote a multi-paragraph welcome banner to stdout on
    every ``import totalreclaw.hermes`` / ``TotalReclaw(...)`` call
    when ``~/.totalreclaw/credentials.json`` was absent. Two problems
    surfaced during the rc.8 Hermes auto-QA run with the Git-plugin
    install path:

    1. Chat-breaker: banner dominated ``hermes chat -q`` stdout in
       agent contexts, so the QA harness could not parse the
       ``session_id`` from the response and the chat step failed.
    2. Phrase-safety violation: the banner told the user to
       ``Run: totalreclaw setup``. When an agent reads that hint and
       invokes the CLI through its shell tool, the CLI's interactive
       prompts echo the recovery phrase through the agent's stdout /
       LLM context — which violates the absolute phrase-safety rule
       (``recovery phrase MUST NEVER cross the LLM context``).

    Fix: suppress the banner entirely. Agent-driven setup flows via
    the ``totalreclaw_pair`` tool, which SKILL.md routes to on a
    "Set up TotalReclaw" prompt. The pair flow runs entirely in a
    browser-side crypto handshake — no phrase ever touches stdout
    or the LLM context. The ``totalreclaw setup`` CLI wizard still
    exists for user-in-terminal setup and emits its own prompts
    directly; it is not supposed to be invoked from an agent shell.

    Copy constants (``WELCOME_MESSAGE``, ``BRANCH_QUESTION``,
    ``STORAGE_GUIDANCE``, etc.) remain exported — the CLI wizard and
    cross-client parity tests still consume them — but
    ``maybe_emit_welcome`` no longer renders them to any stream.

Added 2.3.1 (2026-04-20); banner suppressed 2.3.1rc9 (2026-04-23).
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

LOCAL_MODE_INSTRUCTIONS = (
    "Ask your agent to 'Set up TotalReclaw' — it will walk you through a QR "
    "pairing flow. Your recovery phrase never crosses the chat."
)

REMOTE_MODE_INSTRUCTIONS = """
Ask your agent to 'Set up TotalReclaw' — it will walk you through a QR pairing flow. Scan the QR on your phone, enter the 6-digit PIN the agent shows you, and pick "Generate new" or "Restore existing". Your recovery phrase never crosses the chat. Your recovery phrase never leaves this machine.
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
    """Historically emitted a first-run welcome banner to ``stream``.

    **As of 2.3.1rc9 this is a no-op.** The function is preserved so
    existing callers (``totalreclaw.client.TotalReclaw.__init__``,
    ``totalreclaw.hermes.register``) don't break on upgrade, but it
    never writes to ``stream`` and always returns ``False``. See the
    module docstring "Banner suppression" section for rationale.

    Why a no-op instead of a removal:

    * Two in-tree callers still invoke it. Removing the function would
      force a coordinated plugin + client update. A no-op is strictly
      safer.
    * The banner text violated the absolute phrase-safety rule: it
      suggested ``Run: totalreclaw setup``, a CLI that emits the
      recovery phrase to stdout. In an agent-driven context
      (``hermes chat -q``, ``openclaw chat``, etc.) the agent reads
      stdout back into its own LLM context, so the phrase would cross
      the LLM boundary. Suppressing the banner removes the hint.
    * The banner ALSO broke the ``hermes chat -q`` stdout format in
      agent harnesses: multi-paragraph output dominated the session-id
      response, so QA harnesses could not parse it and the chat step
      failed. Suppression unblocks the harness immediately.

    Parameters are retained byte-for-byte so call sites don't have to
    change. They are all ignored.
    """
    # Mark as "emitted" so any subsequent caller that treats a prior
    # emission as a signal (e.g. an integration smoke test) still sees
    # the once-per-process semantics it expects. This is cheap and
    # preserves the existing observable flag behaviour.
    global _emitted_this_process
    _emitted_this_process = True
    return False


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
