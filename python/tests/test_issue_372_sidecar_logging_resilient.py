"""#372 — pair sidecar must NOT crash when its log file can't be opened.

QA-hermes-prestable-2.4.4rc6 F4: ``_configure_sidecar_logging`` called
``logging.FileHandler(~/.totalreclaw/.pair_sidecar.log)`` which raised
``FileNotFoundError`` (stale docker bind-mount inode after ``wipe.sh tr``)
and crashed the sidecar before any pairing could complete — the pair flow
failed on the first 4 attempts.

Logging is a best-effort audit trail. A FileHandler failure (missing/stale
dir, permissions, full disk, read-only mount) must degrade to no-file
logging, never abort the pairing the sidecar exists to perform.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


def _restore_root_logging():
    """Snapshot + restore the root logger so these tests don't leak global
    handler/level state into the rest of the suite."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    return root, saved_handlers, saved_level


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    root, handlers, level = _restore_root_logging()
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)


def test_sidecar_logging_survives_filehandler_oserror(monkeypatch, tmp_path):
    """A FileHandler that raises OSError must NOT propagate — the sidecar
    falls back to a NullHandler and keeps going."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from totalreclaw.pair import completion_sidecar as cs

    def _boom(*a, **k):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(cs.logging, "FileHandler", _boom)

    # Must not raise.
    cs._configure_sidecar_logging()

    root = logging.getLogger()
    assert root.handlers, "root logger must still have a handler"
    assert all(
        isinstance(h, logging.NullHandler) for h in root.handlers
    ), "must degrade to NullHandler when the file handler can't be created"


def test_sidecar_logging_writes_file_in_normal_case(monkeypatch, tmp_path):
    """Regression: the happy path still attaches a working file handler."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from totalreclaw.pair import completion_sidecar as cs

    cs._configure_sidecar_logging()
    logging.getLogger().info("pair-sidecar test line")

    log_file = tmp_path / ".totalreclaw" / ".pair_sidecar.log"
    assert log_file.exists(), "normal case must still write the rolling log file"
    assert "pair-sidecar test line" in log_file.read_text(encoding="utf-8")


def test_sidecar_logging_survives_missing_parent_dir(monkeypatch, tmp_path):
    """Even if the log path's parent is missing at handler-build time
    (stale-mount shape), configuration must not raise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from totalreclaw.pair import completion_sidecar as cs

    # Force the log path under a parent that does not exist, bypassing
    # _totalreclaw_dir()'s mkdir, to mimic the stale-inode failure.
    missing = tmp_path / "gone" / ".pair_sidecar.log"
    monkeypatch.setattr(cs, "_sidecar_log_path", lambda: missing)

    cs._configure_sidecar_logging()  # must not raise
    root = logging.getLogger()
    assert root.handlers
