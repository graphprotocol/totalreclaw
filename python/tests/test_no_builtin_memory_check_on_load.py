"""2.5.3rc4: plugin load no longer checks Hermes' built-in ``memory`` tool.

rc.26 (issue #167) made ``register()`` shell out to ``hermes tools list``
and WARN when the built-in memory tool was enabled; 2.3.7rc2 added a
recursion guard because that subprocess loads this plugin again. TotalReclaw
now runs alongside the built-in memory on purpose and mirrors its writes, so
the enabled tool is the expected state: the check, its warning and its
subprocess are gone. Pin that plugin load spawns nothing.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import totalreclaw.hermes as hermes_pkg


def test_register_spawns_no_subprocess(caplog):
    ctx = MagicMock()
    with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                hermes_pkg.register(ctx)
    run.assert_not_called()
    popen.assert_not_called()
    assert "hermes tools disable memory" not in caplog.text


def test_builtin_memory_warning_is_removed():
    assert not hasattr(hermes_pkg, "_warn_if_built_in_memory_enabled")
    assert not hasattr(hermes_pkg, "_RECURSION_GUARD_ENV")
