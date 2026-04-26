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
"""
from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from totalreclaw.hermes import _warn_if_built_in_memory_enabled


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
