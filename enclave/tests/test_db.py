from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.support import T0, VAULT_A
from totalreclaw_enclave.db import Database, DatabaseClosedError
from totalreclaw_enclave.db.migrations import MigrationError


async def test_open_applies_pragmas_and_migrations(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "enclave.sqlite3")
    assert await database.open(now=T0) == [1]
    try:
        assert (await database.fetchone("PRAGMA journal_mode"))[0] == "wal"
        assert (await database.fetchone("PRAGMA synchronous"))[0] == 2  # FULL
        assert (await database.fetchone("PRAGMA foreign_keys"))[0] == 1
        assert (await database.fetchone("PRAGMA secure_delete"))[0] == 1
        assert (await database.fetchone("PRAGMA trusted_schema"))[0] == 0
    finally:
        await database.close()
    reopened = Database(tmp_path / "nested" / "enclave.sqlite3")
    assert await reopened.open(now=T0) == []  # already migrated
    await reopened.close()


async def test_statements_run_on_the_dedicated_db_thread(db: Database) -> None:
    name = await db.run(lambda _conn: threading.current_thread().name)
    assert name.startswith("enclave-db")
    assert name != threading.current_thread().name


async def test_run_rolls_back_on_exception(db: Database) -> None:
    def insert_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'a.b')", (T0,))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await db.run(insert_then_fail)
    assert (await db.fetchone("SELECT count(*) FROM audit_log"))[0] == 0


class _CommitFails:
    """Connection proxy simulating a COMMIT-time failure (e.g. disk full).

    Everything delegates to the real connection except ``execute("COMMIT")``,
    which raises. Used only for fault injection from this test.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._wrapped = conn

    @property
    def in_transaction(self) -> bool:
        return self._wrapped.in_transaction

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("simulated COMMIT-time failure")
        return self._wrapped.execute(sql, params)


async def test_run_rolls_back_when_commit_fails(db: Database) -> None:
    raw = db._conn
    assert raw is not None
    db._conn = _CommitFails(raw)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            await db.run(
                lambda conn: conn.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'a.b')", (T0,))
            )
    finally:
        db._conn = raw
    # A COMMIT-time failure must not wedge the shared connection: the failed
    # transaction is rolled back, so later writes still succeed.
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'c.d')", (T0 + 1,))
    assert (await db.fetchone("SELECT count(*) FROM audit_log"))[0] == 1


async def test_concurrent_writes_are_serialized(db: Database) -> None:
    await asyncio.gather(
        *(db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'a.b')", (T0 + i,)) for i in range(50))
    )
    assert (await db.fetchone("SELECT count(*) FROM audit_log"))[0] == 50


async def test_execute_returns_rowcount_and_fetchall_rows(db: Database) -> None:
    for i in range(3):
        await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'a.b')", (T0 + i,))
    assert await db.execute("DELETE FROM audit_log WHERE ts >= ?", (T0 + 1,)) == 2
    rows = await db.fetchall("SELECT ts, event FROM audit_log")
    assert [(r["ts"], r["event"]) for r in rows] == [(T0, "a.b")]


async def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO conversations VALUES (?, 'c1', x'00', ?, ?)", (VAULT_A, T0, T0))


async def test_closed_database_refuses_work(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.sqlite3")
    with pytest.raises(DatabaseClosedError):
        await database.fetchone("SELECT 1")
    await database.open(now=T0)
    await database.close()
    await database.close()  # idempotent
    with pytest.raises(DatabaseClosedError):
        await database.execute("SELECT 1")


async def test_opening_twice_is_refused(db: Database) -> None:
    with pytest.raises(RuntimeError, match="already open"):
        await db.open(now=T0)


async def test_failed_open_releases_the_db_thread_and_connection(tmp_path: Path) -> None:
    path = tmp_path / "e.sqlite3"
    newer = Database(path)
    await newer.open(now=T0)
    await newer.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, 'future', ?)", (T0,)
    )
    await newer.close()

    def db_threads() -> int:
        return sum(t.name.startswith("enclave-db") for t in threading.enumerate())

    before = db_threads()
    older = Database(path)
    for _ in range(2):  # a failed open leaves the instance closed, so a retry fails the same way
        with pytest.raises(MigrationError, match="newer than this build"):
            await older.open(now=T0)
        assert db_threads() == before
        with pytest.raises(DatabaseClosedError):
            await older.fetchone("SELECT 1")
