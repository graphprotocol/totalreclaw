"""Async facade over stdlib ``sqlite3``.

One connection, owned by one dedicated thread (``ThreadPoolExecutor(1)``):
every statement runs on that thread, so SQLite sees a single writer and the
event loop never blocks on disk I/O. Multi-statement work goes through
``Database.run(fn)``, which wraps ``fn(conn)`` in ``BEGIN IMMEDIATE`` /
``COMMIT`` (``ROLLBACK`` on any exception).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from totalreclaw_enclave.db.migrations import MIGRATIONS, Migration, apply_migrations
from totalreclaw_enclave.errors import SafeMessageError

T = TypeVar("T")

MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT tables

# Applied to every connection. FULL (not NORMAL) so a committed ingest job or
# refresh-token rotation survives power loss, not only a process crash.
PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    # Overwrite deleted content (unpair deletes sealed bundles) instead of
    # leaving free pages on the volume.
    "PRAGMA secure_delete = ON",
    "PRAGMA trusted_schema = OFF",
)


class DatabaseClosedError(SafeMessageError, RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str, *, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self._path = Path(path)
        self._migrations = tuple(migrations)
        self._executor: ThreadPoolExecutor | None = None
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def open(self, *, now: int) -> list[int]:
        """Connect, apply PRAGMAs and pending migrations. Returns applied versions.

        If anything fails (e.g. a schema newer than this build), the connection
        and the DB thread are released before the exception propagates.
        """
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError("SQLite >= 3.37 is required (STRICT tables)")
        if self._executor is not None:
            raise RuntimeError("database already open")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="enclave-db")
        try:
            return await self._submit(self._open_sync, now)
        except BaseException:
            await self.close()
            raise

    def _open_sync(self, now: int) -> list[int]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn = conn  # set first, so close() releases it if a step below fails
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        return apply_migrations(conn, now=now, migrations=self._migrations)

    async def close(self) -> None:
        if self._executor is None:
            return
        await self._submit(self._close_sync)
        self._executor.shutdown(wait=True)
        self._executor = None

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def _submit(self, fn: Callable[..., T], *args: object) -> T:
        if self._executor is None:
            raise DatabaseClosedError("database is not open")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseClosedError("database is not open")
        return self._conn

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` on the DB thread inside one write transaction."""

        def _tx() -> T:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        return await self._submit(_tx)

    async def execute(self, sql: str, params: Sequence[object] = ()) -> int:
        """Run one write statement in its own transaction. Returns ``rowcount``."""
        return await self.run(lambda conn: conn.execute(sql, params).rowcount)

    async def fetchone(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Row | None:
        return await self._submit(lambda: self._connection().execute(sql, params).fetchone())

    async def fetchall(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        return await self._submit(lambda: self._connection().execute(sql, params).fetchall())
