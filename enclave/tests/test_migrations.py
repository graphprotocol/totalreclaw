from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support import T0, VAULT_A, VAULT_B
from totalreclaw_enclave.db.migrations import (
    MIGRATIONS,
    SCHEMA_MIGRATIONS_DDL,
    Migration,
    MigrationError,
    apply_migrations,
)

# Spec §3.1, column for column: (name, declared type, NOT NULL, PK position).
EXPECTED_SCHEMA: dict[str, list[tuple[str, str, int, int]]] = {
    "vaults": [
        ("vault_id", "TEXT", 1, 1),
        ("sealed_bundle", "BLOB", 1, 0),
        ("signing_kind", "TEXT", 1, 0),
        ("chain_id", "INTEGER", 1, 0),
        ("data_edge", "TEXT", 1, 0),
        ("tier_cache", "TEXT", 0, 0),
        ("created_at", "INTEGER", 1, 0),
        ("last_seen_at", "INTEGER", 1, 0),
    ],
    "oauth_clients": [
        ("client_id", "TEXT", 1, 1),
        ("kind", "TEXT", 1, 0),
        ("redirect_uris", "TEXT", 1, 0),
        ("metadata", "TEXT", 1, 0),
        ("created_at", "INTEGER", 1, 0),
    ],
    "connect_sessions": [
        ("id", "TEXT", 1, 1),
        ("client_id", "TEXT", 1, 0),
        ("redirect_uri", "TEXT", 1, 0),
        ("state", "TEXT", 0, 0),
        ("code_challenge", "TEXT", 1, 0),
        ("resource", "TEXT", 1, 0),
        ("eph_pub", "BLOB", 0, 0),
        ("sealed_eph_priv", "BLOB", 0, 0),
        ("nonce", "BLOB", 0, 0),
        ("status", "TEXT", 1, 0),
        ("vault_id", "TEXT", 0, 0),
        ("expires_at", "INTEGER", 1, 0),
    ],
    "auth_codes": [
        ("code_hash", "TEXT", 1, 1),
        ("session_id", "TEXT", 1, 0),
        ("vault_id", "TEXT", 1, 0),
        ("client_id", "TEXT", 1, 0),
        ("scopes", "TEXT", 1, 0),
        ("expires_at", "INTEGER", 1, 0),
        ("used", "INTEGER", 1, 0),
    ],
    "tokens": [
        ("token_hash", "TEXT", 1, 1),
        ("kind", "TEXT", 1, 0),
        ("vault_id", "TEXT", 1, 0),
        ("client_id", "TEXT", 1, 0),
        ("scopes", "TEXT", 1, 0),
        ("expires_at", "INTEGER", 1, 0),
        ("rotated_from", "TEXT", 0, 0),
        ("revoked_at", "INTEGER", 0, 0),
    ],
    "conversations": [
        ("vault_id", "TEXT", 1, 1),
        ("conversation_id", "TEXT", 1, 2),
        ("sealed_slot", "BLOB", 1, 0),
        ("last_activity", "INTEGER", 1, 0),
        ("updated_at", "INTEGER", 1, 0),
    ],
    "ingest_queue": [
        ("ingest_id", "TEXT", 1, 1),
        ("vault_id", "TEXT", 1, 0),
        ("conversation_id", "TEXT", 1, 0),
        ("sealed_payload", "BLOB", 1, 0),
        ("status", "TEXT", 1, 0),
        ("attempts", "INTEGER", 1, 0),
        ("next_attempt_at", "INTEGER", 1, 0),
        ("created_at", "INTEGER", 1, 0),
    ],
    "inference_verifications": [
        ("provider", "TEXT", 1, 1),
        ("model", "TEXT", 1, 2),
        ("report_hash", "TEXT", 1, 3),
        ("signing_key", "TEXT", 1, 0),
        ("verified_at", "INTEGER", 1, 0),
        ("expires_at", "INTEGER", 1, 0),
        ("policy_version", "TEXT", 1, 0),
    ],
    "usage_local": [
        ("vault_id", "TEXT", 1, 1),
        ("month", "TEXT", 1, 2),
        ("ingests", "INTEGER", 1, 0),
        ("inference_tokens", "INTEGER", 1, 0),
    ],
    "audit_log": [
        ("ts", "INTEGER", 1, 0),
        ("vault_hash", "TEXT", 0, 0),
        ("event", "TEXT", 1, 0),
        ("details", "TEXT", 1, 0),
    ],
}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _schema_sql(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return list(conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"))


def _insert_vault(conn: sqlite3.Connection, vault_id: str) -> None:
    conn.execute(
        "INSERT INTO vaults (vault_id, sealed_bundle, signing_kind, chain_id, data_edge, created_at, last_seen_at)"
        " VALUES (?, ?, 'session-key', 100, '0xe7a4d2677b686e13775ba9092631089e35f0bb91', ?, ?)",
        (vault_id, b"\x00opaque", T0, T0),
    )


def test_initial_schema_matches_spec_3_1_exactly(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    assert apply_migrations(conn, now=T0) == [1]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(EXPECTED_SCHEMA) | {"schema_migrations"}
    for table, expected in EXPECTED_SCHEMA.items():
        actual = [(r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]
        assert actual == expected, table


def test_every_table_is_strict(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    for table in EXPECTED_SCHEMA:
        strict = [r for r in conn.execute("PRAGMA table_list") if r[1] == table][0][5]
        assert strict == 1, table


def test_reapplying_is_a_no_op(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    before = _schema_sql(conn)
    assert apply_migrations(conn, now=T0 + 60) == []
    assert _schema_sql(conn) == before
    assert list(conn.execute("SELECT version, name, applied_at FROM schema_migrations")) == [
        (1, "initial_schema", T0)
    ]


def test_crash_after_ddl_before_bookkeeping_recovers(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    for statement in MIGRATIONS[0].statements:  # DDL ran, version row never written
        conn.execute(statement)
    assert apply_migrations(conn, now=T0) == [1]


def test_newer_database_is_refused(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, 'future', ?)", (T0,))
    with pytest.raises(MigrationError, match="newer than this build"):
        apply_migrations(conn, now=T0)


def test_versions_must_be_contiguous(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    gap = (MIGRATIONS[0], Migration(3, "gap", ("SELECT 1",)))
    with pytest.raises(MigrationError, match="contiguous"):
        apply_migrations(conn, now=T0, migrations=gap)


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    broken = (*MIGRATIONS, Migration(2, "broken", ("CREATE TABLE extra (x INTEGER) STRICT", "NOT SQL")))
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, now=T0, migrations=broken)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "extra" not in tables
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [1]


def test_unpair_cascades_from_vaults(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    for vault in (VAULT_A, VAULT_B):
        _insert_vault(conn, vault)
        conn.execute("INSERT INTO conversations VALUES (?, 'c1', x'00', ?, ?)", (vault, T0, T0))
        conn.execute(
            "INSERT INTO ingest_queue VALUES (?, ?, 'c1', x'00', 'queued', 0, ?, ?)",
            (f"ing-{vault[-4:]}", vault, T0, T0),
        )
        conn.execute(
            "INSERT INTO tokens VALUES (?, 'access', ?, 'grok', 'memory.read', ?, NULL, NULL)",
            (vault[2:].ljust(64, "0")[:64], vault, T0 + 3600),
        )
        conn.execute(
            "INSERT INTO auth_codes VALUES (?, 's1', ?, 'grok', 'memory.read', ?, 0)",
            (vault[2:].ljust(64, "1")[:64], vault, T0 + 60),
        )
        conn.execute("INSERT INTO usage_local VALUES (?, '2026-09', 1, 0)", (vault,))
        conn.execute(
            "INSERT INTO connect_sessions (id, client_id, redirect_uri, code_challenge, resource, status,"
            " vault_id, expires_at) VALUES (?, 'grok', 'https://grok.com/connectors/oauth/callback', 'cc',"
            " 'https://enclave.totalreclaw.xyz/mcp', 'complete', ?, ?)",
            (f"cs-{vault[-4:]}", vault, T0 + 600),
        )
    conn.execute("INSERT INTO audit_log (ts, vault_hash, event) VALUES (?, 'abcdef0123456789', 'x.y')", (T0,))

    conn.execute("DELETE FROM vaults WHERE vault_id = ?", (VAULT_A,))

    for table in ("conversations", "ingest_queue", "tokens", "auth_codes", "usage_local"):
        remaining = [r[0] for r in conn.execute(f"SELECT vault_id FROM {table}")]
        assert remaining == [VAULT_B], table
    sessions = dict(conn.execute("SELECT id, vault_id FROM connect_sessions"))
    assert sessions == {f"cs-{VAULT_A[-4:]}": None, f"cs-{VAULT_B[-4:]}": VAULT_B}
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1


@pytest.mark.parametrize(
    "sql, params",
    [
        # vault_id must be a lowercase 0x address
        (
            "INSERT INTO vaults VALUES (?, x'00', 'session-key', 100, 'x', NULL, 0, 0)",
            ("0x" + "A1" * 20,),
        ),
        ("INSERT INTO vaults VALUES (?, x'00', 'session-key', 100, 'x', NULL, 0, 0)", ("0x1234",)),
        # signing_kind is closed
        ("INSERT INTO vaults VALUES (?, x'00', 'mnemonic', 100, 'x', NULL, 0, 0)", (VAULT_A,)),
        # STRICT: text in an INTEGER column
        ("INSERT INTO vaults VALUES (?, x'00', 'session-key', 'gnosis', 'x', NULL, 0, 0)", (VAULT_A,)),
        # oauth client kind is closed
        ("INSERT INTO oauth_clients VALUES ('c', 'static', '[]', '{}', 0)", ()),
        # token hashes are 64 hex chars
        ("INSERT INTO tokens VALUES ('short', 'access', 'v', 'c', 's', 0, NULL, NULL)", ()),
        # usage month is YYYY-MM
        ("INSERT INTO usage_local VALUES ('v', '2026-9', 0, 0)", ()),
    ],
)
def test_constraints_reject_malformed_rows(tmp_path: Path, sql: str, params: tuple[object, ...]) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    apply_migrations(conn, now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_schema_migrations_ddl_is_idempotent(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "s.sqlite3")
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    conn.execute(SCHEMA_MIGRATIONS_DDL)
