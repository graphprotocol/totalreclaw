"""rc.26: defensive WARN log when Hermes built-in `memory` is enabled.

Context — rc.24 NO-GO finding (issue #167)
-------------------------------------------

The SKILL.md / hermes-setup.md install flow auto-disables Hermes
built-in memory at install time. But if the user later runs
``hermes tools enable memory`` manually, both tools compete for
"remember X" / "recall X" intents and TotalReclaw silently regresses
into the rc.24 bug (memories landing in MEMORY.md instead of vault).

What this test enforces
-----------------------

The plugin's ``register()`` function MUST run a defensive check at
plugin load: shell out to ``hermes tools list``, parse for an
enabled ``memory`` tool, and emit a WARN log line if found. The WARN
must be best-effort — must NOT crash plugin load if the CLI is
absent or returns garbage.

Implementation lives in ``totalreclaw.hermes.__init__:_warn_if_built_in_memory_enabled``.

Recursion-guard tests (2.3.7rc2)
--------------------------------

The 2.3.7rc2 hotfix adds a sentinel env var so the spawned
``hermes tools list`` subprocess short-circuits its own plugin-load
check (which would otherwise fork-bomb the host). Tests below
verify: (a) the function early-returns when the sentinel is set,
(b) the subprocess is invoked with the sentinel set in its env.
"""
from __future__ import annotations

import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from totalreclaw.hermes import (
    _RECURSION_GUARD_ENV,
    _warn_if_built_in_memory_enabled,
)


def _make_proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


class TestWarnIfBuiltInMemoryEnabled:
    def test_warns_when_memory_listed_as_enabled(self, caplog):
        """If `hermes tools list` includes `memory  enabled`, WARN."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("memory  enabled\nfoo  disabled\n")
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        ), "Expected a WARN line when built-in memory is enabled"
        # Must include the recommended remediation command.
        assert any(
            "hermes tools disable memory" in rec.message for rec in caplog.records
        ), "WARN line must include the `hermes tools disable memory` remediation"

    def test_warns_on_colon_format(self, caplog):
        """Tolerates `memory: enabled` (alternative output format)."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("memory: enabled\n")
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        )

    def test_no_warn_when_memory_disabled(self, caplog):
        """If `memory` is listed as disabled, do NOT WARN."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run",
            return_value=_make_proc("memory  disabled\nfoo  enabled\n"),
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert not any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        ), "Should NOT WARN when built-in memory is disabled"

    def test_no_warn_when_memory_absent_from_listing(self, caplog):
        """If the CLI doesn't list a `memory` tool at all, no WARN
        (older Hermes versions, custom builds, etc.)."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run",
            return_value=_make_proc("foo  enabled\nbar  disabled\n"),
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert not any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        )

    def test_no_warn_when_hermes_cli_absent(self, caplog):
        """`hermes` not on PATH → silently skip (no WARN, no crash)."""
        with patch("shutil.which", return_value=None):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert not any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        )

    def test_no_warn_on_subprocess_timeout(self, caplog):
        """`hermes tools list` hangs → silently skip (no WARN)."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=5),
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert not any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        )

    def test_no_warn_on_nonzero_exit(self, caplog):
        """If `hermes tools list` exits non-zero, skip (no WARN)."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("error: not authorized", returncode=1)
        ):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.hermes"):
                _warn_if_built_in_memory_enabled()
        assert not any(
            "Hermes built-in 'memory' tool is enabled" in rec.message
            for rec in caplog.records
        )

    def test_does_not_raise_on_unexpected_error(self):
        """Defensive: even unhandled exceptions inside the shell call
        must not propagate — the function is wrapped in a broad
        try/except inside register(), but the unit MUST also tolerate
        whatever subprocess decides to raise."""
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", side_effect=PermissionError("denied")
        ):
            try:
                _warn_if_built_in_memory_enabled()
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"_warn_if_built_in_memory_enabled raised: {exc!r}")


class TestRecursionGuard:
    """2.3.7rc2 hotfix: prevent fork-bomb from the
    ``hermes tools list`` subprocess re-loading this plugin → calling
    ``_warn_if_built_in_memory_enabled`` recursively → spawning
    another ``hermes tools list``…

    On pop-os 2026-05-14 this storm pinned a 4-core box (load avg 4.04,
    7+ concurrent ``hermes tools list`` processes per container, dozens
    of ``[hermes] <defunct>`` zombies). 5s timeout per call did NOT
    help — each call forks many children before its own timeout fires.
    """

    def test_sentinel_env_var_short_circuits(self, caplog, monkeypatch):
        """When ``_TOTALRECLAW_SKIP_BUILTIN_MEMORY_CHECK=1`` is set in
        the parent env, the function MUST return immediately without
        invoking ``shutil.which`` or ``subprocess.run`` — otherwise the
        recursion can't terminate."""
        monkeypatch.setenv(_RECURSION_GUARD_ENV, "1")
        with patch("shutil.which") as mock_which, patch(
            "subprocess.run"
        ) as mock_run:
            _warn_if_built_in_memory_enabled()
        mock_which.assert_not_called()
        mock_run.assert_not_called()

    def test_sentinel_set_to_other_value_does_not_short_circuit(
        self, monkeypatch
    ):
        """Only exact ``1`` triggers the short-circuit. Other values
        (e.g. ``0``, ``false``, empty) must NOT short-circuit — this
        keeps the guard predictable + minimises confusion if the env
        leaks from an unrelated source."""
        monkeypatch.setenv(_RECURSION_GUARD_ENV, "0")
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("foo  disabled\n")
        ) as mock_run:
            _warn_if_built_in_memory_enabled()
        mock_run.assert_called_once()

    def test_subprocess_invoked_with_sentinel_in_child_env(self, monkeypatch):
        """The subprocess MUST be spawned with
        ``_TOTALRECLAW_SKIP_BUILTIN_MEMORY_CHECK=1`` in its env.
        Without this, the child plugin-load recurses → fork bomb."""
        # Strip any pre-existing sentinel from the parent so the spawn
        # path is exercised (not the early-return path).
        monkeypatch.delenv(_RECURSION_GUARD_ENV, raising=False)
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("memory  disabled\n")
        ) as mock_run:
            _warn_if_built_in_memory_enabled()
        mock_run.assert_called_once()
        call = mock_run.call_args
        passed_env = call.kwargs.get("env")
        assert passed_env is not None, (
            "subprocess.run MUST be called with an explicit env= so the "
            "recursion-guard sentinel is set. Inheriting the parent env "
            "won't work — the parent's env may not contain the sentinel."
        )
        assert passed_env.get(_RECURSION_GUARD_ENV) == "1", (
            f"subprocess.run env MUST contain "
            f"{_RECURSION_GUARD_ENV}=1 so the recursive plugin load "
            f"short-circuits. Got: {passed_env.get(_RECURSION_GUARD_ENV)!r}"
        )

    def test_subprocess_env_preserves_parent_environment(self, monkeypatch):
        """The child env MUST inherit the parent env (PATH, HOME,
        TOTALRECLAW_* etc.) plus the sentinel — not be a stripped
        env with only the sentinel. Otherwise the spawned ``hermes``
        won't find its dependencies."""
        monkeypatch.delenv(_RECURSION_GUARD_ENV, raising=False)
        monkeypatch.setenv("TOTALRECLAW_TEST_MARKER", "preserved")
        with patch("shutil.which", return_value="/usr/bin/hermes"), patch(
            "subprocess.run", return_value=_make_proc("")
        ) as mock_run:
            _warn_if_built_in_memory_enabled()
        passed_env = mock_run.call_args.kwargs["env"]
        assert passed_env.get("TOTALRECLAW_TEST_MARKER") == "preserved", (
            "Child env must inherit parent env vars; the sentinel is "
            "an addition, not a replacement."
        )

    def test_recursion_guard_env_name_is_stable(self):
        """The sentinel env var name is part of the contract between
        parent + child processes. Pin it so a rename doesn't break the
        guard for users on older deployed versions of the plugin (the
        parent process may be running a newer version while a stale
        subprocess from a transition window runs an older version)."""
        assert _RECURSION_GUARD_ENV == "_TOTALRECLAW_SKIP_BUILTIN_MEMORY_CHECK"
