"""Hermes CLI — ``hermes setup`` subcommand for first-run onboarding.

Mirrors the TypeScript plugin 3.3.0 ``openclaw totalreclaw onboard``
wizard. Same branch question, same ``recovery phrase`` terminology,
same last-3-words confirmation challenge for generated phrases.

Entry point
-----------
Registered via ``[project.scripts]`` in ``python/pyproject.toml``::

    [project.scripts]
    hermes = "totalreclaw.hermes.cli:main"

A pip-installed user gets a ``hermes`` executable on PATH after
``pip install totalreclaw``. The only subcommand in 2.3.1 is ``setup``;
the namespace is left open so future releases can add ``status`` /
``export`` / ``chat`` subcommands without renaming the binary.

Security
--------
* No network. No env reads beyond TTY/mode detection.
* The recovery phrase is NEVER written to stdout, logs, or the Hermes
  plugin context. Generated phrases go to stderr for the ``write it
  down`` banner, then disappear with the process.
* File writes land at the canonical credentials path
  (``~/.totalreclaw/credentials.json``) with mode ``0600``. Matches
  the plugin's ``writeCredentialsJson``.
* On non-TTY stdin the generate-flow confirmation still runs (so
  scripted installers work), but the restore-flow prints a clear
  warning that the phrase was visible in the prompt.

Added 2.3.1 (2026-04-20).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, TextIO

from totalreclaw.onboarding import (
    CANONICAL_CREDENTIALS_PATH,
    GENERATED_CONFIRMATION,
    RESTORE_PROMPT,
    STORAGE_GUIDANCE,
    WELCOME_MESSAGE,
    BRANCH_QUESTION,
    detect_first_run,
)

logger = logging.getLogger(__name__)


# BIP-39 word count for the onboarding flow. 12 words = 128 bits of
# entropy. Matches plugin-side generateMnemonic(wordlist, 128) +
# plugin-side validation.
_MNEMONIC_WORD_COUNT = 12


# ---------------------------------------------------------------------------
# I/O adapter — kept minimal so tests can mock stdin/stdout
# ---------------------------------------------------------------------------


class _IO:
    """Thin stdin/stdout wrapper for the wizard.

    Attributes
    ----------
    stdin : TextIO
        Source of user input.
    stdout : TextIO
        Destination for prompts + success banners.
    stderr : TextIO
        Destination for the generated-phrase banner + warnings. Stderr
        is used for the phrase so users piping ``hermes setup > out``
        do NOT capture the phrase into a log file.
    is_tty : bool
        True if stdin is a real TTY. Used to decide whether to warn
        about visible input in the restore flow.
    """

    def __init__(
        self,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
        stderr: Optional[TextIO] = None,
    ):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        # isatty() may raise on some wrapper streams — default to False.
        try:
            self.is_tty = bool(self.stdin.isatty())
        except Exception:
            self.is_tty = False

    def write(self, text: str) -> None:
        self.stdout.write(text)
        try:
            self.stdout.flush()
        except Exception:
            pass

    def write_err(self, text: str) -> None:
        self.stderr.write(text)
        try:
            self.stderr.flush()
        except Exception:
            pass

    def prompt(self, question: str) -> str:
        """Write the prompt + read one line of user input. Returns trimmed."""
        self.write(question)
        try:
            line = self.stdin.readline()
        except Exception:
            return ""
        if line == "":
            # EOF on non-TTY stdin
            return ""
        return line.rstrip("\r\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_phrase(raw: str) -> str:
    """Collapse whitespace + lowercase a pasted BIP-39 phrase.

    Accepts space-separated OR newline-separated input (matches the
    task requirement "space-separated or one-per-line"). Does NOT
    validate the checksum — that's :func:`_validate_mnemonic`.
    """
    import unicodedata

    # NFKC + strip zero-width chars the way the plugin does.
    cleaned = unicodedata.normalize("NFKC", raw)
    cleaned = "".join(ch for ch in cleaned if ch not in ("\u200b", "\u200c", "\u200d", "\ufeff"))
    words = cleaned.strip().lower().split()
    return " ".join(words)


def _validate_mnemonic(phrase: str) -> bool:
    """True iff the phrase is a 12-word BIP-39 mnemonic with a valid checksum.

    Uses :meth:`eth_account.Account.from_mnemonic` for the checksum check
    — it raises ``eth_account.hdaccount.ValidationError`` on bad
    checksums / unknown words. No new prod dependency needed; the
    ``eth-account`` package is already in ``pyproject.toml`` for EOA
    derivation.
    """
    words = phrase.split()
    if len(words) != _MNEMONIC_WORD_COUNT:
        return False
    try:
        from eth_account import Account

        Account.enable_unaudited_hdwallet_features()
        Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
    except Exception:
        return False
    return True


def _generate_mnemonic() -> str:
    """Generate a fresh 12-word BIP-39 mnemonic via ``eth_account``.

    Uses the same path as the existing ``totalreclaw_setup`` tool so
    the generated phrases produce identical Smart Account addresses
    regardless of whether the user onboards via the CLI or via the
    plugin tool call.
    """
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    _acct, mnemonic = Account.create_with_mnemonic()
    return mnemonic.strip()


def _write_credentials(credentials_path: Path, mnemonic: str) -> None:
    """Write ``{"mnemonic": ...}`` at mode 0600.

    Canonical shape — matches plugin 3.2.0+ and Python 2.2.2+. This is
    the write path every fresh setup lands on (legacy ``recovery_phrase``
    key is read-compatible but never newly written).
    """
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(json.dumps({"mnemonic": mnemonic}))
    try:
        credentials_path.chmod(0o600)
    except OSError:
        # Windows / read-only FS — best-effort, user can tighten perms.
        logger.debug("Could not chmod 0600 on %s", credentials_path, exc_info=True)


def _last_n_words(mnemonic: str, n: int = 3) -> list[str]:
    return mnemonic.split()[-n:]


# ---------------------------------------------------------------------------
# Wizard flows
# ---------------------------------------------------------------------------


def _confirm_overwrite(io: _IO, credentials_path: Path) -> bool:
    """Prompt the user before overwriting an existing credentials file.

    Returns ``True`` if the user typed ``y`` or ``yes``. Defaults to
    ``False`` (safe default — don't clobber an existing vault).
    """
    io.write(f"Account already set up at {credentials_path}. Overwrite? [y/N]: ")
    try:
        answer = io.stdin.readline().strip().lower()
    except Exception:
        answer = ""
    return answer in ("y", "yes")


def _ask_branch(io: _IO) -> str:
    """Ask the branch question. Returns ``restore`` / ``generate`` / ``cancel``."""
    io.write(BRANCH_QUESTION + "\n")
    io.write("[restore/generate]: ")
    try:
        raw = io.stdin.readline().strip().lower()
    except Exception:
        raw = ""
    if raw in ("r", "restore"):
        return "restore"
    if raw in ("g", "generate", "new"):
        return "generate"
    return "cancel"


def _run_restore(io: _IO, credentials_path: Path) -> int:
    """Interactive restore flow. Returns exit code."""
    io.write("\n" + RESTORE_PROMPT + "\n")
    if not io.is_tty:
        io.write_err(
            "Warning: stdin is not a TTY. Your recovery phrase will be visible "
            "in this prompt. Consider running 'hermes setup' on a real terminal.\n"
        )
    io.write("(12 words, space-separated or one per line — press Enter twice when done)\n> ")

    lines: list[str] = []
    try:
        # Accept: one-liner, OR multi-line input terminated by blank line
        # OR EOF. Consume at most 24 lines to bound memory.
        for _ in range(32):
            line = io.stdin.readline()
            if line == "":
                break  # EOF
            stripped = line.rstrip("\r\n")
            lines.append(stripped)
            # Early exit if we already have 12 words on the first non-empty line
            joined_so_far = _normalize_phrase(" ".join(lines))
            if len(joined_so_far.split()) >= _MNEMONIC_WORD_COUNT:
                break
            # Blank line terminator (user hit enter twice)
            if stripped.strip() == "" and any(prev.strip() for prev in lines[:-1]):
                break
    except Exception:
        io.write_err("Failed to read recovery phrase from stdin.\n")
        return 2

    phrase = _normalize_phrase(" ".join(lines))
    if not _validate_mnemonic(phrase):
        io.write_err(
            "\nThat is not a valid 12-word BIP-39 recovery phrase. "
            "Check the word list + spelling and try again.\n"
        )
        return 1

    try:
        _write_credentials(credentials_path, phrase)
    except OSError as e:
        io.write_err(f"\nCould not write credentials file: {e}\n")
        return 2

    io.write(
        f"\nAccount restored. Credentials saved to {credentials_path} "
        "(mode 0600).\nMemory tools are now active in Hermes.\n"
    )
    return 0


def _run_generate(io: _IO, credentials_path: Path) -> int:
    """Interactive generate flow. Returns exit code."""
    try:
        mnemonic = _generate_mnemonic()
    except Exception as e:
        io.write_err(f"\nCould not generate recovery phrase: {e}\n")
        return 2
    words = mnemonic.split()
    if len(words) != _MNEMONIC_WORD_COUNT:
        io.write_err(
            f"\nInternal error: generated phrase has {len(words)} words (expected 12).\n"
        )
        return 2

    # STORAGE_GUIDANCE copy — print BEFORE showing the phrase so the
    # user reads the handling rules before they're tempted to screenshot.
    io.write("\n" + STORAGE_GUIDANCE + "\n")

    # Phrase banner to stderr — keeps it out of `hermes setup > out.txt`.
    io.write_err("\nYour recovery phrase (WRITE THIS DOWN):\n\n")
    # Render as a 4x3 grid, numbered.
    for row in range(3):
        cells = []
        for col in range(4):
            idx = row * 4 + col
            cells.append(f"{idx + 1:>2}. {words[idx]:<12}")
        io.write_err("  " + "".join(cells) + "\n")
    io.write_err("\n")

    # Last-3-words confirmation challenge — matches the plugin's
    # "retype probe words" pattern, simplified to the tail 3 since
    # users typically write the phrase top-to-bottom.
    io.write(
        "Before we save, type the LAST 3 words of your phrase "
        "(space-separated) to confirm you wrote it down:\n> "
    )
    try:
        typed = io.stdin.readline().rstrip("\r\n").strip().lower()
    except Exception:
        typed = ""

    expected = " ".join(_last_n_words(mnemonic, 3))
    if typed != expected:
        io.write_err(
            "\nWord mismatch. No credentials have been written. "
            "Write the phrase down carefully and re-run `hermes setup`.\n"
        )
        return 1

    try:
        _write_credentials(credentials_path, mnemonic)
    except OSError as e:
        io.write_err(f"\nCould not write credentials file: {e}\n")
        return 2

    io.write("\n" + GENERATED_CONFIRMATION + "\n")
    io.write(
        f"\nCredentials saved to {credentials_path} (mode 0600). "
        "Memory tools are now active in Hermes.\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_setup(
    credentials_path: Optional[Path] = None,
    io: Optional[_IO] = None,
) -> int:
    """Run the interactive setup wizard. Returns exit code.

    Parameters
    ----------
    credentials_path : Path, optional
        Override for tests. Defaults to ``CANONICAL_CREDENTIALS_PATH``.
    io : _IO, optional
        Pre-built IO adapter. Defaults to stdin/stdout/stderr.
    """
    path = credentials_path if credentials_path is not None else CANONICAL_CREDENTIALS_PATH
    wizard_io = io if io is not None else _IO()

    # Welcome banner — always printed for the setup subcommand (not
    # suppressed by the per-process flag in ``onboarding.py``). The
    # subcommand is an explicit user-initiated action; they asked to
    # see the onboarding flow.
    wizard_io.write(WELCOME_MESSAGE + "\n\n")

    if not detect_first_run(path):
        if not _confirm_overwrite(wizard_io, path):
            wizard_io.write("\nSetup cancelled. Existing credentials untouched.\n")
            return 0
        wizard_io.write("\n")

    branch = _ask_branch(wizard_io)
    if branch == "restore":
        return _run_restore(wizard_io, path)
    if branch == "generate":
        return _run_generate(wizard_io, path)

    wizard_io.write_err(
        "\nUnrecognised choice. Expected 'restore' or 'generate'. Aborting.\n"
    )
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    """``hermes`` console-script entry point."""
    parser = argparse.ArgumentParser(
        prog="hermes",
        description=(
            "TotalReclaw Hermes plugin CLI. First-run onboarding + maintenance "
            "commands for the Python-side encrypted memory vault. "
            "Run 'hermes setup' on a fresh machine to generate or restore a "
            "recovery phrase."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sp_setup = sub.add_parser(
        "setup",
        help=(
            "Interactive first-run wizard. Generates a new 12-word recovery "
            "phrase or restores from an existing one. Writes credentials to "
            "~/.totalreclaw/credentials.json."
        ),
    )
    sp_setup.add_argument(
        "--credentials-path",
        type=Path,
        default=None,
        help="Override the credentials file location (default: ~/.totalreclaw/credentials.json).",
    )

    args = parser.parse_args(argv)

    if args.command == "setup":
        return run_setup(credentials_path=args.credentials_path)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
