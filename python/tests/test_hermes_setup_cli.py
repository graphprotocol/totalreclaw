"""Tests for the ``hermes setup`` CLI wizard (added 2.3.1).

Exercises:
- Happy-path restore with a mocked 12-word stdin → credentials.json written.
- Happy-path generate with a mocked last-3-words confirmation → STORAGE_GUIDANCE
  + GENERATED_CONFIRMATION printed + credentials written.
- Overwrite-confirmation reject path.
- Invalid 12-word input rejected (bad word count + bad BIP-39 checksum).
- Non-TTY stdin tolerated (scripted installers) in both flows.
- Generated phrase goes to stderr, NOT stdout — so ``hermes setup > out.txt``
  cannot capture the phrase into a log.

Mocking strategy: we use ``StringIO`` for stdin/stdout/stderr and a
pre-built ``_IO`` adapter so we never touch real stdin/TTY detection.
``_generate_mnemonic`` is mocked per-test to make the generate flow
deterministic.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from totalreclaw.hermes import cli as hermes_cli

# A canonical 12-word test vector with a valid BIP-39 checksum. Same
# one used across the suite — matches ``test_hermes_plugin::test_configure``.
VALID_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _make_io(
    stdin_text: str, is_tty: bool = True
) -> tuple[hermes_cli._IO, StringIO, StringIO]:
    """Build an _IO adapter with canned stdin + capturing stdout/stderr."""
    stdin = StringIO(stdin_text)
    stdout = StringIO()
    stderr = StringIO()
    io = hermes_cli._IO(stdin=stdin, stdout=stdout, stderr=stderr)
    io.is_tty = is_tty
    return io, stdout, stderr


# ---------------------------------------------------------------------------
# Restore flow
# ---------------------------------------------------------------------------


class TestRestoreFlow:
    def test_happy_path_space_separated(self, tmp_path: Path) -> None:
        """Restore flow: 12 words space-separated on one line."""
        creds = tmp_path / "credentials.json"
        stdin_text = f"restore\n{VALID_MNEMONIC}\n"
        io, stdout, stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0, stderr.getvalue()
        assert creds.exists()
        saved = json.loads(creds.read_text())
        assert saved["mnemonic"].strip() == VALID_MNEMONIC
        out = stdout.getvalue()
        assert "Account restored" in out
        # Canonical terminology in the prompt.
        assert "recovery phrase" in out.lower()

    def test_happy_path_multiline(self, tmp_path: Path) -> None:
        """Restore flow: one word per line, terminated by EOF."""
        creds = tmp_path / "credentials.json"
        words_per_line = "\n".join(VALID_MNEMONIC.split())
        stdin_text = f"restore\n{words_per_line}\n"
        io, stdout, _stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0
        saved = json.loads(creds.read_text())
        assert saved["mnemonic"].strip() == VALID_MNEMONIC

    def test_invalid_word_count_rejected(self, tmp_path: Path) -> None:
        """Fewer than 12 words → non-zero exit + no file written."""
        creds = tmp_path / "credentials.json"
        short = "abandon abandon abandon abandon"
        stdin_text = f"restore\n{short}\n"
        io, _stdout, stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc != 0
        assert not creds.exists()
        assert "valid" in stderr.getvalue().lower()

    def test_invalid_checksum_rejected(self, tmp_path: Path) -> None:
        """Bad BIP-39 checksum → non-zero exit + no file written.

        12 real BIP-39 words but in an order that doesn't produce a
        valid checksum. ``eth_account.Account.from_mnemonic`` raises on
        this — :func:`_validate_mnemonic` catches.
        """
        creds = tmp_path / "credentials.json"
        bad = "about " * 11 + "abandon"  # all valid words, wrong checksum
        stdin_text = f"restore\n{bad.strip()}\n"
        io, _stdout, stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc != 0
        assert not creds.exists()
        assert "valid" in stderr.getvalue().lower()

    def test_non_tty_restore_prints_visibility_warning(self, tmp_path: Path) -> None:
        """On non-TTY stdin the restore path still works but warns."""
        creds = tmp_path / "credentials.json"
        stdin_text = f"restore\n{VALID_MNEMONIC}\n"
        io, _stdout, stderr = _make_io(stdin_text, is_tty=False)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)
        assert rc == 0
        assert creds.exists()
        # Warning about visible input.
        assert "visible" in stderr.getvalue().lower()


# ---------------------------------------------------------------------------
# Generate flow
# ---------------------------------------------------------------------------


class TestGenerateFlow:
    def test_happy_path_silent(self, tmp_path: Path) -> None:
        """2.3.1rc2 default: generate → file written, phrase NOT shown, no confirmation prompt."""
        creds = tmp_path / "credentials.json"

        fake_mnem = VALID_MNEMONIC
        # No retype needed in silent mode — just pick the generate branch.
        stdin_text = "generate\n"
        io, stdout, stderr = _make_io(stdin_text)

        with patch.object(hermes_cli, "_generate_mnemonic", return_value=fake_mnem):
            rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0, stderr.getvalue()
        assert creds.exists()
        saved = json.loads(creds.read_text())
        assert saved["mnemonic"] == fake_mnem

        out = stdout.getvalue()
        err = stderr.getvalue()

        from totalreclaw.onboarding import STORAGE_GUIDANCE, GENERATED_CONFIRMATION

        assert STORAGE_GUIDANCE in out
        assert GENERATED_CONFIRMATION in out

        # 2.3.1rc2: phrase is NOT echoed to stdout OR stderr in default mode.
        assert fake_mnem not in out
        assert fake_mnem not in err
        # Individual words also absent (defensive — catches partial prints).
        assert fake_mnem.split()[0] not in err
        # Pointer to credentials.json retrieval is present.
        assert "cat" in out and "jq" in out and "mnemonic" in out
        # Warning that the phrase must be stored safely.
        assert "store" in out.lower() and "safely" in out.lower()

    def test_emit_phrase_opt_in(self, tmp_path: Path) -> None:
        """`--emit-phrase` (2.3.1rc2 opt-in) → phrase shown on stderr + last-3-words confirmation."""
        creds = tmp_path / "credentials.json"

        fake_mnem = VALID_MNEMONIC
        last3 = " ".join(fake_mnem.split()[-3:])

        stdin_text = f"generate\n{last3}\n"
        io, stdout, stderr = _make_io(stdin_text)

        with patch.object(hermes_cli, "_generate_mnemonic", return_value=fake_mnem):
            rc = hermes_cli.run_setup(credentials_path=creds, io=io, emit_phrase=True)

        assert rc == 0, stderr.getvalue()
        assert creds.exists()
        saved = json.loads(creds.read_text())
        assert saved["mnemonic"] == fake_mnem

        out = stdout.getvalue()
        err = stderr.getvalue()

        from totalreclaw.onboarding import STORAGE_GUIDANCE, GENERATED_CONFIRMATION

        assert STORAGE_GUIDANCE in out
        assert GENERATED_CONFIRMATION in out

        # Phrase words appear in STDERR (opt-in behaviour).
        assert fake_mnem.split()[0] in err
        # Phrase is NOT in stdout regardless of opt-in.
        assert fake_mnem not in out
        # Opt-in warning about terminal visibility is present.
        assert "visible" in err.lower() or "visible" in out.lower()

    def test_emit_phrase_wrong_confirmation_rejects(self, tmp_path: Path) -> None:
        """With --emit-phrase, mistyped last-3-words → non-zero exit + no file written."""
        creds = tmp_path / "credentials.json"
        fake_mnem = VALID_MNEMONIC

        stdin_text = "generate\nfoo bar baz\n"
        io, stdout, stderr = _make_io(stdin_text)

        with patch.object(hermes_cli, "_generate_mnemonic", return_value=fake_mnem):
            rc = hermes_cli.run_setup(credentials_path=creds, io=io, emit_phrase=True)

        assert rc != 0
        assert not creds.exists()
        assert "mismatch" in stderr.getvalue().lower()
        from totalreclaw.onboarding import GENERATED_CONFIRMATION

        assert GENERATED_CONFIRMATION not in stdout.getvalue()

    def test_non_tty_generate_silent_still_works(self, tmp_path: Path) -> None:
        """Generate flow (default silent mode) tolerates non-TTY stdin."""
        creds = tmp_path / "credentials.json"
        fake_mnem = VALID_MNEMONIC

        stdin_text = "generate\n"
        io, _stdout, _stderr = _make_io(stdin_text, is_tty=False)

        with patch.object(hermes_cli, "_generate_mnemonic", return_value=fake_mnem):
            rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0
        assert creds.exists()


# ---------------------------------------------------------------------------
# Overwrite confirmation
# ---------------------------------------------------------------------------


class TestOverwriteConfirmation:
    def test_reject_overwrite_keeps_existing(self, tmp_path: Path) -> None:
        """When creds already exist and user answers 'n', no changes."""
        creds = tmp_path / "credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"mnemonic": "old phrase here"})
        creds.write_text(original)

        # User answers "n" to the overwrite prompt. The branch question
        # and restore/generate branches are not reached.
        stdin_text = "n\n"
        io, stdout, _stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0
        # File is untouched.
        assert creds.read_text() == original
        # Cancellation message appears.
        assert "cancelled" in stdout.getvalue().lower()

    def test_accept_overwrite_proceeds_to_branch_question(self, tmp_path: Path) -> None:
        """When user answers 'y' they reach the restore/generate branch."""
        creds = tmp_path / "credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({"mnemonic": "old phrase to overwrite"}))

        # "y" to overwrite, then "restore", then the real phrase.
        stdin_text = f"y\nrestore\n{VALID_MNEMONIC}\n"
        io, _stdout, stderr = _make_io(stdin_text)

        rc = hermes_cli.run_setup(credentials_path=creds, io=io)

        assert rc == 0, stderr.getvalue()
        saved = json.loads(creds.read_text())
        # New phrase replaced the old one.
        assert saved["mnemonic"].strip() == VALID_MNEMONIC


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------


class TestMainEntry:
    def test_main_no_args_prints_help(self) -> None:
        """`hermes` with no subcommand prints help + returns 0."""
        rc = hermes_cli.main([])
        assert rc == 0

    def test_main_setup_invokes_run_setup(self, tmp_path: Path) -> None:
        """`hermes setup --credentials-path <p>` dispatches to run_setup."""
        creds = tmp_path / "c.json"
        with patch.object(hermes_cli, "run_setup", return_value=0) as m:
            rc = hermes_cli.main(["setup", "--credentials-path", str(creds)])
        assert rc == 0
        m.assert_called_once_with(credentials_path=creds, emit_phrase=False)

    def test_main_setup_emit_phrase_forwarded(self, tmp_path: Path) -> None:
        """`hermes setup --emit-phrase` forwards the flag to run_setup."""
        creds = tmp_path / "c.json"
        with patch.object(hermes_cli, "run_setup", return_value=0) as m:
            rc = hermes_cli.main(["setup", "--credentials-path", str(creds), "--emit-phrase"])
        assert rc == 0
        m.assert_called_once_with(credentials_path=creds, emit_phrase=True)
