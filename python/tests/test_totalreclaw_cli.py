"""Tests for the standalone `totalreclaw` CLI (added 2.3.1rc2).

Covers:
- `totalreclaw` (no subcommand) prints help + returns 0.
- `totalreclaw setup` delegates to hermes_cli.run_setup with emit_phrase plumbed through.
- `totalreclaw doctor` returns 2 when no credentials file (fast-path "setup first").
- `totalreclaw doctor` returns 0 or 1 when credentials exist (depending on checks).
- `totalreclaw doctor` detects a corrupt credentials.json and returns 2.
- `totalreclaw doctor` validates BIP-39 mnemonic and flags invalid ones.

Network-touching checks (SA derivation, relay health probe) are best-effort
and won't fail the test run when offline — the doctor degrades gracefully.
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from totalreclaw import cli as tr_cli


# A canonical 12-word test vector with a valid BIP-39 checksum.
VALID_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


class TestMainEntry:
    def test_no_subcommand_prints_help(self, capsys) -> None:
        """`totalreclaw` with no subcommand prints a helpful message."""
        rc = tr_cli.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "totalreclaw" in captured.out.lower()

    def test_setup_forwards_to_hermes_run_setup(self, tmp_path: Path) -> None:
        """`totalreclaw setup --credentials-path <p>` → hermes_cli.run_setup."""
        creds = tmp_path / "credentials.json"
        with patch("totalreclaw.hermes.cli.run_setup", return_value=0) as m:
            rc = tr_cli.main(["setup", "--credentials-path", str(creds)])
        assert rc == 0
        m.assert_called_once()
        args, kwargs = m.call_args
        assert kwargs.get("credentials_path") == creds
        assert kwargs.get("emit_phrase") is False

    def test_setup_emit_phrase_forwarded(self, tmp_path: Path) -> None:
        """`totalreclaw setup --emit-phrase` forwards the flag."""
        creds = tmp_path / "credentials.json"
        with patch("totalreclaw.hermes.cli.run_setup", return_value=0) as m:
            rc = tr_cli.main(["setup", "--credentials-path", str(creds), "--emit-phrase"])
        assert rc == 0
        args, kwargs = m.call_args
        assert kwargs.get("emit_phrase") is True


class TestDoctor:
    def test_no_credentials_returns_2(self, tmp_path: Path, capsys) -> None:
        """Missing credentials file → exit 2 + prompt to run setup."""
        creds = tmp_path / "does-not-exist.json"
        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc == 2
        captured = capsys.readouterr()
        assert "totalreclaw setup" in captured.out.lower()

    def test_corrupt_credentials_returns_2(self, tmp_path: Path, capsys) -> None:
        """credentials.json that doesn't parse → exit 2 (setup-level failure)."""
        creds = tmp_path / "credentials.json"
        creds.write_text("this is not json at all")
        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc == 2
        captured = capsys.readouterr()
        assert "corrupt" in captured.out.lower() or "fail" in captured.out.lower()

    def test_valid_mnemonic_progresses_past_parse(self, tmp_path: Path, capsys) -> None:
        """Valid credentials → doctor progresses past the parse check to the rest."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"mnemonic": VALID_MNEMONIC}))

        # We don't care about the exit code here (network/LLM checks may
        # produce warnings). What matters: the mnemonic check passes.
        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc in (0, 1)  # NOT 2 — setup is complete.
        captured = capsys.readouterr()
        assert "valid BIP-39" in captured.out

    def test_invalid_mnemonic_flagged(self, tmp_path: Path, capsys) -> None:
        """Garbage mnemonic → doctor flags it but still progresses."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"mnemonic": "not really a real phrase here at all just words"}))

        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc in (0, 1)
        captured = capsys.readouterr()
        assert "validation failed" in captured.out.lower() or "fail" in captured.out.lower()

    def test_cached_scope_address_surfaced(self, tmp_path: Path, capsys) -> None:
        """A pre-cached scope_address is shown in doctor output."""
        creds = tmp_path / "credentials.json"
        creds.write_text(
            json.dumps(
                {
                    "mnemonic": VALID_MNEMONIC,
                    "scope_address": "0x1234567890abcdef1234567890abcdef12345678",
                }
            )
        )
        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc in (0, 1)
        captured = capsys.readouterr()
        assert "0x1234567890abcdef1234567890abcdef12345678" in captured.out.lower() or \
               "0x1234567890abcdef1234567890abcdef12345678" in captured.out

    def test_rc_bug_report_status_surfaced(self, tmp_path: Path, capsys) -> None:
        """Doctor surfaces whether the RC bug-report tool is active."""
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"mnemonic": VALID_MNEMONIC}))

        rc = tr_cli.run_doctor(credentials_path=creds)
        assert rc in (0, 1)
        captured = capsys.readouterr()
        # Output mentions "totalreclaw_report_qa_bug" regardless of RC vs
        # stable — the message differentiates via the "REGISTERED" / "not
        # registered" / "Stable build" wording.
        assert "totalreclaw_report_qa_bug" in captured.out


# ---------------------------------------------------------------------------
# memory-status + activate-memory-provider (#351 §5.4) — the commands older
# setup guides referenced as `hermes activate-memory-provider` (which never
# existed). Now real subcommands of the `totalreclaw` console script.
# ---------------------------------------------------------------------------


class TestMemoryProviderCli:
    def test_memory_status_fresh_is_none(self, tmp_path: Path, capsys):
        rc = tr_cli.main(["memory-status", "--hermes-home", str(tmp_path)])
        out = json.loads(capsys.readouterr().out.strip())
        assert rc == 0
        assert out == {"provider": "none"}

    def test_activate_then_status_is_totalreclaw(self, tmp_path: Path, capsys):
        rc = tr_cli.main(["activate-memory-provider", "--hermes-home", str(tmp_path)])
        assert rc == 0
        act_out = capsys.readouterr().out
        assert "active provider:  totalreclaw" in act_out
        assert "builtin disabled: True" in act_out

        rc2 = tr_cli.main(["memory-status", "--hermes-home", str(tmp_path)])
        assert rc2 == 0
        assert json.loads(capsys.readouterr().out.strip()) == {"provider": "totalreclaw"}

    def test_activate_writes_sidecar_at_discoverable_path(self, tmp_path: Path, capsys):
        tr_cli.main(["activate-memory-provider", "--hermes-home", str(tmp_path)])
        capsys.readouterr()
        # Discoverable user-provider path = $HERMES_HOME/plugins/<name>/, NOT
        # the nested plugins/memory/<name>/ (that one Hermes never scans).
        assert (tmp_path / "plugins" / "totalreclaw" / "__init__.py").exists()
        assert not (tmp_path / "plugins" / "memory" / "totalreclaw").exists()

    def test_activate_disables_builtin_in_config(self, tmp_path: Path, capsys):
        tr_cli.main(["activate-memory-provider", "--hermes-home", str(tmp_path)])
        capsys.readouterr()
        import yaml
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["memory"]["provider"] == "totalreclaw"
        assert cfg["memory"]["memory_enabled"] is False
        assert cfg["memory"]["user_profile_enabled"] is False

    # ------------------------------------------------------------------
    # #390: the top-level CLI must surface the `--force` flag that
    # install_sidecar() advertises in its error message. Pre-fix, the
    # error said "Pass force=True (or --force on the CLI) to replace it"
    # but the console-script CLI didn't register --force, leaving users
    # stuck whenever they hit a hand-edited sidecar (e.g. after manual
    # plugin debugging).
    # ------------------------------------------------------------------

    def test_issue_390_activate_memory_provider_accepts_force(
        self, tmp_path: Path, capsys
    ) -> None:
        """`totalreclaw activate-memory-provider --force` overwrites a hand-edited sidecar."""
        # Seed a hand-edited (managed-marker-less) sidecar at the discoverable path.
        sidecar_dir = tmp_path / "plugins" / "totalreclaw"
        sidecar_dir.mkdir(parents=True)
        hand_edited = "# hand-rolled sidecar by the user — no managed-marker\n"
        (sidecar_dir / "__init__.py").write_text(hand_edited, encoding="utf-8")

        # Without --force the CLI must refuse cleanly (exit 2, error on stderr).
        rc_no_force = tr_cli.main(
            ["activate-memory-provider", "--hermes-home", str(tmp_path)]
        )
        captured_no_force = capsys.readouterr()
        assert rc_no_force == 2
        assert "Refusing to overwrite" in captured_no_force.err
        assert "--force" in captured_no_force.err
        # The hand-edited file is untouched.
        assert (sidecar_dir / "__init__.py").read_text(encoding="utf-8") == hand_edited

        # With --force the CLI overwrites it.
        rc_force = tr_cli.main(
            ["activate-memory-provider", "--hermes-home", str(tmp_path), "--force"]
        )
        captured_force = capsys.readouterr()
        assert rc_force == 0
        assert "active provider:  totalreclaw" in captured_force.out
        # The managed marker is now present.
        rewritten = (sidecar_dir / "__init__.py").read_text(encoding="utf-8")
        assert "managed-by: totalreclaw.hermes.install_memory_provider" in rewritten

    def test_issue_390_install_memory_provider_accepts_force(
        self, tmp_path: Path, capsys
    ) -> None:
        """`totalreclaw install-memory-provider --force` overwrites a hand-edited sidecar (tools-only path)."""
        sidecar_dir = tmp_path / "plugins" / "totalreclaw"
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "__init__.py").write_text("# user-authored\n", encoding="utf-8")

        rc_no_force = tr_cli.main(
            ["install-memory-provider", "--hermes-home", str(tmp_path)]
        )
        captured_no_force = capsys.readouterr()
        assert rc_no_force == 2
        assert "Refusing to overwrite" in captured_no_force.err

        rc_force = tr_cli.main(
            ["install-memory-provider", "--hermes-home", str(tmp_path), "--force"]
        )
        captured_force = capsys.readouterr()
        assert rc_force == 0
        # tools-only path: provider is NOT switched to totalreclaw.
        assert "tools-only" in captured_force.out
        rewritten = (sidecar_dir / "__init__.py").read_text(encoding="utf-8")
        assert "managed-by: totalreclaw.hermes.install_memory_provider" in rewritten
