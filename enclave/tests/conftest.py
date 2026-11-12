"""Shared fixtures for the enclave tests.

Test-safety invariants (mirrors python/tests/conftest.py):
* every test runs as ENCLAVE_ENV=dev, and no ENCLAVE_* value is inherited
  from the developer's shell;
* the Python client's relay default is forced to staging, never production.

Fixtures import enclave modules lazily so each module's tests run as soon as
that module exists.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import warnings
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support import T0

if TYPE_CHECKING:
    from totalreclaw_enclave.clock import FixedClock
    from totalreclaw_enclave.db import Database
    from totalreclaw_enclave.deps import Deps
    from totalreclaw_enclave.settings import Settings

for _name in [n for n in os.environ if n.startswith("ENCLAVE_")]:
    del os.environ[_name]
os.environ["ENCLAVE_ENV"] = "dev"
os.environ["TOTALRECLAW_SERVER_URL"] = "https://api-staging.totalreclaw.xyz"


@pytest.fixture
def clock() -> FixedClock:
    from totalreclaw_enclave.clock import FixedClock

    return FixedClock(T0)


@pytest.fixture
def dev_settings(tmp_path: Path) -> Settings:
    from totalreclaw_enclave.settings import load_settings

    return load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_DB_PATH": str(tmp_path / "enclave.sqlite3")})


@pytest.fixture
async def db(tmp_path: Path, clock: FixedClock) -> AsyncIterator[Database]:
    from totalreclaw_enclave.db import Database

    database = Database(tmp_path / "test.sqlite3")
    await database.open(now=clock.now())
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def deps(dev_settings: Settings, clock: FixedClock) -> Deps:
    from totalreclaw_enclave.deps import build_deps

    return build_deps(dev_settings, clock=clock)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo configure_logging's global changes after a test."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_hooks = (sys.excepthook, threading.excepthook, warnings.showwarning)
    access = logging.getLogger("uvicorn.access")
    saved_access_disabled = access.disabled
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        sys.excepthook, threading.excepthook, warnings.showwarning = saved_hooks
        access.disabled = saved_access_disabled
