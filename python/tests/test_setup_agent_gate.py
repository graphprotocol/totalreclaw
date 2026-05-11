"""P0 phrase-safety gate on `totalreclaw setup` / `hermes setup`
(added 2.3.6rc4 after the 2.3.2 stable QA on Pop-OS Hermes container —
docs/notes/2026-05-11-hermes-rc3-failure-plan.md).

Verifies the gate added in ``totalreclaw.hermes.cli`` refuses to run the
wizard when invoked from an agent shell (non-TTY stdin or known agent
env markers) UNLESS the explicit ``allow_non_tty=True`` opt-out is set.

Why this test exists:
On 2026-05-11 a Hermes chat agent invoked ``totalreclaw setup`` via its
shell tool. The wizard's default (silent-save since 2.3.1rc2) did NOT
print the recovery phrase, but the wallet was generated locally via the
CLI rather than via the architecturally-correct browser-pair flow
(``totalreclaw_pair`` tool). The phrase then sits in plaintext at
``~/.totalreclaw/credentials.json`` accessible to any subsequent agent
shell command — a future agent turn that runs
``cat ~/.totalreclaw/credentials.json`` immediately leaks the phrase
into LLM context. P0 fix: refuse to run the wizard at all when an agent
shell is detected.

Test matrix:
* Non-TTY stdin (the actual observed failure mode, ``echo ... | totalreclaw setup``) → refused
* Each agent-runtime env marker present → refused
* TTY + no markers → passes the gate (wizard proceeds; we mock
  downstream so we don't actually run the wizard, just verify the gate
  doesn't fire)
* ``allow_non_tty=True`` override → bypasses the gate even on non-TTY
* Refusal message contains the redirect to ``totalreclaw_pair``
* Refusal exit code is non-zero (specifically 3, to distinguish from
  exit 0 success, exit 1 wizard error, exit 2 file-write error)
"""

from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from totalreclaw.hermes import cli as hermes_cli


# ---------------------------------------------------------------------------
# _is_agent_runtime — the detection function itself
# ---------------------------------------------------------------------------


class TestIsAgentRuntime:
    """Direct unit tests for ``_is_agent_runtime`` (the detection oracle)."""

    def test_non_tty_stdin_fires(self) -> None:
        """``sys.stdin.isatty() == False`` (piped / no controlling terminal)
        is the canonical agent-shell signal."""
        with patch("sys.stdin.isatty", return_value=False):
            with patch.dict(os.environ, {}, clear=False):
                # Ensure no env markers are set during this test
                for marker in hermes_cli._AGENT_RUNTIME_ENV_MARKERS:
                    os.environ.pop(marker, None)
                assert hermes_cli._is_agent_runtime() is True

    def test_tty_stdin_with_no_env_passes(self) -> None:
        """Real user terminal: TTY stdin + no agent env markers → not
        agent-runtime."""
        with patch("sys.stdin.isatty", return_value=True):
            with patch.dict(os.environ, {}, clear=False):
                for marker in hermes_cli._AGENT_RUNTIME_ENV_MARKERS:
                    os.environ.pop(marker, None)
                assert hermes_cli._is_agent_runtime() is False

    @pytest.mark.parametrize("marker", hermes_cli._AGENT_RUNTIME_ENV_MARKERS)
    def test_each_env_marker_fires(self, marker: str) -> None:
        """Each documented agent-runtime env marker, when set to any
        truthy value, must trigger the gate even on a TTY stdin."""
        with patch("sys.stdin.isatty", return_value=True):
            with patch.dict(os.environ, {marker: "1"}, clear=False):
                assert hermes_cli._is_agent_runtime() is True

    def test_empty_env_marker_value_does_not_fire(self) -> None:
        """An env var set to the empty string should not fire the gate
        (the dict iteration uses ``os.environ.get`` which returns "" as
        falsy here — confirms our truthy semantics)."""
        with patch("sys.stdin.isatty", return_value=True):
            with patch.dict(
                os.environ, {"HERMES_AGENT_RUN": ""}, clear=False
            ):
                # Empty string env var is falsy → gate must not fire
                # off only this marker
                assert hermes_cli._is_agent_runtime() is False

    def test_unknown_env_marker_does_not_fire(self) -> None:
        """Random env vars must NOT trigger the gate — only the
        whitelist in ``_AGENT_RUNTIME_ENV_MARKERS``."""
        with patch("sys.stdin.isatty", return_value=True):
            with patch.dict(
                os.environ, {"RANDOM_UNRELATED_VAR": "1"}, clear=False
            ):
                for marker in hermes_cli._AGENT_RUNTIME_ENV_MARKERS:
                    os.environ.pop(marker, None)
                assert hermes_cli._is_agent_runtime() is False


# ---------------------------------------------------------------------------
# run_setup — the gate's effect on the wizard entry point
# ---------------------------------------------------------------------------


class TestRunSetupGate:
    """Integration tests verifying that ``run_setup`` actually refuses
    when ``_is_agent_runtime()`` fires + a useful error message is
    written to stderr."""

    def test_refuses_when_agent_runtime_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Default ``allow_non_tty=False`` + non-TTY stdin → refusal
        with exit code 3."""
        creds = tmp_path / "creds.json"
        with patch.object(hermes_cli, "_is_agent_runtime", return_value=True):
            rc = hermes_cli.run_setup(credentials_path=creds)
        assert rc == 3, (
            f"expected exit 3 from agent-gate refusal, got {rc}"
        )
        # Refusal message went to stderr
        captured = capsys.readouterr()
        assert "totalreclaw_pair" in captured.err, (
            "refusal message must redirect agents to totalreclaw_pair tool"
        )
        assert "non-TTY" in captured.err or "agent-runtime" in captured.err, (
            "refusal message must explain what triggered the gate"
        )
        # No credentials file written on refusal
        assert not creds.exists(), (
            "refusal path must not touch the credentials file"
        )

    def test_allow_non_tty_bypasses_gate(self, tmp_path: Path) -> None:
        """``allow_non_tty=True`` opt-out → wizard proceeds even when
        ``_is_agent_runtime()`` would fire."""
        creds = tmp_path / "creds.json"
        # Mock the gate to fire AND mock downstream wizard to short-circuit
        # without actually running the full interactive flow.
        with patch.object(hermes_cli, "_is_agent_runtime", return_value=True):
            with patch.object(
                hermes_cli, "_ask_branch", return_value=None
            ) as ask:
                # Stub detect_first_run + io machinery so the wizard reaches
                # _ask_branch without hitting real stdin.
                with patch.object(
                    hermes_cli, "detect_first_run", return_value=True
                ):
                    rc = hermes_cli.run_setup(
                        credentials_path=creds,
                        allow_non_tty=True,
                    )
        # _ask_branch was reached → gate was bypassed
        assert ask.called, (
            "with allow_non_tty=True, gate must not fire and wizard "
            "must proceed past the gate to the branch prompt"
        )
        # _ask_branch returned None → wizard hit the unknown-choice
        # exit path (rc=1). The exact exit code isn't the point —
        # the point is that _ask_branch was reached at all.
        assert rc in (0, 1, 2), (
            f"after bypass, wizard runs normally; got unexpected rc={rc}"
        )

    def test_user_terminal_passes_gate(self, tmp_path: Path) -> None:
        """TTY stdin + no env markers → gate doesn't fire; wizard
        proceeds. Same scaffolding as the allow_non_tty test but with
        ``_is_agent_runtime()`` returning False naturally."""
        creds = tmp_path / "creds.json"
        with patch.object(hermes_cli, "_is_agent_runtime", return_value=False):
            with patch.object(
                hermes_cli, "_ask_branch", return_value=None
            ) as ask:
                with patch.object(
                    hermes_cli, "detect_first_run", return_value=True
                ):
                    rc = hermes_cli.run_setup(credentials_path=creds)
        assert ask.called, (
            "on a real user terminal, the gate must not fire and the "
            "wizard must proceed past the gate"
        )

    def test_refusal_message_points_to_guide(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The refusal message must include the hermes-setup.md guide
        URL so the agent can resolve confusion immediately."""
        creds = tmp_path / "creds.json"
        with patch.object(hermes_cli, "_is_agent_runtime", return_value=True):
            hermes_cli.run_setup(credentials_path=creds)
        captured = capsys.readouterr()
        assert "hermes-setup.md" in captured.err, (
            "refusal message must reference the user guide for further "
            "context"
        )


# ---------------------------------------------------------------------------
# CLI argparse plumbing
# ---------------------------------------------------------------------------


class TestArgparseAllowNonTtyPropagation:
    """``hermes setup --allow-non-tty`` must propagate through to
    ``run_setup(allow_non_tty=True)``."""

    def test_flag_propagates(self) -> None:
        with patch.object(hermes_cli, "run_setup", return_value=0) as m:
            hermes_cli.main(["setup", "--allow-non-tty"])
        m.assert_called_once()
        call_kwargs = m.call_args.kwargs
        assert call_kwargs.get("allow_non_tty") is True, (
            f"--allow-non-tty must propagate; got {call_kwargs}"
        )

    def test_flag_default_is_false(self) -> None:
        with patch.object(hermes_cli, "run_setup", return_value=0) as m:
            hermes_cli.main(["setup"])
        m.assert_called_once()
        call_kwargs = m.call_args.kwargs
        assert call_kwargs.get("allow_non_tty") is False, (
            f"default must be False; got {call_kwargs}"
        )
