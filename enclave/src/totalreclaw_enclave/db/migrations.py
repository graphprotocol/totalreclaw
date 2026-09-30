"""Numbered, idempotent schema migrations for the sealed-state database.

Rules for every later leaf:

* Never edit a released migration. Add ``Migration(N + 1, ...)`` at the end of
  ``MIGRATIONS``; versions are contiguous from 1.
* Every statement is idempotent on its own (``IF NOT EXISTS``), so a crash
  between a statement and the ``schema_migrations`` insert is safe to re-run.
* ``ALTER TABLE ... ADD COLUMN`` is not idempotent in SQLite: guard it with a
  ``PRAGMA table_info`` check inside a Python migration step (``Migration.steps``).

Migration 1 creates every table of spec §3.1 with exactly the spec's columns.
Sensitive columns (``sealed_*``) hold opaque BLOBs; sealing arrives in ENC-3.
Timestamps are INTEGER Unix seconds (UTC).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from totalreclaw_enclave.errors import SafeMessageError


class MigrationError(SafeMessageError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    # Optional Python steps, run after ``statements`` in the same transaction.
    steps: tuple[Callable[[sqlite3.Connection], None], ...] = field(default=())


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at INTEGER NOT NULL
) STRICT
"""

_V1_STATEMENTS: tuple[str, ...] = (
    # vaults — per-vault sealed derived-bundle-v1 (spec §3.1).
    """
    CREATE TABLE IF NOT EXISTS vaults (
        vault_id      TEXT    NOT NULL PRIMARY KEY
                      CHECK (length(vault_id) = 42 AND substr(vault_id, 1, 2) = '0x'
                             AND vault_id = lower(vault_id)),
        sealed_bundle BLOB    NOT NULL,
        signing_kind  TEXT    NOT NULL CHECK (signing_kind IN ('session-key', 'owner-eoa')),
        chain_id      INTEGER NOT NULL,
        data_edge     TEXT    NOT NULL,
        tier_cache    TEXT,
        created_at    INTEGER NOT NULL,
        last_seen_at  INTEGER NOT NULL
    ) STRICT
    """,
    # oauth_clients — public client metadata, never sealed.
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id     TEXT    NOT NULL PRIMARY KEY,
        kind          TEXT    NOT NULL CHECK (kind IN ('dcr', 'cimd', 'preregistered')),
        redirect_uris TEXT    NOT NULL,
        metadata      TEXT    NOT NULL,
        created_at    INTEGER NOT NULL
    ) STRICT
    """,
    # connect_sessions — one per /oauth/authorize; eph key material sealed per instance.
    """
    CREATE TABLE IF NOT EXISTS connect_sessions (
        id              TEXT    NOT NULL PRIMARY KEY,
        client_id       TEXT    NOT NULL,
        redirect_uri    TEXT    NOT NULL,
        state           TEXT,
        code_challenge  TEXT    NOT NULL,
        resource        TEXT    NOT NULL,
        eph_pub         BLOB,
        sealed_eph_priv BLOB,
        nonce           BLOB,
        status          TEXT    NOT NULL,
        vault_id        TEXT    REFERENCES vaults (vault_id) ON DELETE SET NULL,
        expires_at      INTEGER NOT NULL
    ) STRICT
    """,
    # auth_codes — single use, 60 s; stored hashed.
    """
    CREATE TABLE IF NOT EXISTS auth_codes (
        code_hash  TEXT    NOT NULL PRIMARY KEY CHECK (length(code_hash) = 64),
        session_id TEXT    NOT NULL,
        vault_id   TEXT    NOT NULL REFERENCES vaults (vault_id) ON DELETE CASCADE,
        client_id  TEXT    NOT NULL,
        scopes     TEXT    NOT NULL,
        expires_at INTEGER NOT NULL,
        used       INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1))
    ) STRICT
    """,
    # tokens — access 1 h / refresh rotating; stored hashed.
    """
    CREATE TABLE IF NOT EXISTS tokens (
        token_hash   TEXT    NOT NULL PRIMARY KEY CHECK (length(token_hash) = 64),
        kind         TEXT    NOT NULL CHECK (kind IN ('access', 'refresh')),
        vault_id     TEXT    NOT NULL REFERENCES vaults (vault_id) ON DELETE CASCADE,
        client_id    TEXT    NOT NULL,
        scopes       TEXT    NOT NULL,
        expires_at   INTEGER NOT NULL,
        rotated_from TEXT,
        revoked_at   INTEGER
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS tokens_vault_idx ON tokens (vault_id)",
    "CREATE INDEX IF NOT EXISTS tokens_rotated_from_idx ON tokens (rotated_from)",
    # conversations — per-vault sealed conversation slot.
    """
    CREATE TABLE IF NOT EXISTS conversations (
        vault_id        TEXT    NOT NULL REFERENCES vaults (vault_id) ON DELETE CASCADE,
        conversation_id TEXT    NOT NULL,
        sealed_slot     BLOB    NOT NULL,
        last_activity   INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL,
        PRIMARY KEY (vault_id, conversation_id)
    ) STRICT
    """,
    # ingest_queue — durable, per-vault sealed payloads.
    """
    CREATE TABLE IF NOT EXISTS ingest_queue (
        ingest_id       TEXT    NOT NULL PRIMARY KEY,
        vault_id        TEXT    NOT NULL REFERENCES vaults (vault_id) ON DELETE CASCADE,
        conversation_id TEXT    NOT NULL,
        sealed_payload  BLOB    NOT NULL,
        status          TEXT    NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL,
        created_at      INTEGER NOT NULL
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS ingest_queue_due_idx ON ingest_queue (status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS ingest_queue_vault_idx ON ingest_queue (vault_id)",
    # inference_verifications — cache of verified attestation reports (<= 10 min).
    """
    CREATE TABLE IF NOT EXISTS inference_verifications (
        provider       TEXT    NOT NULL,
        model          TEXT    NOT NULL,
        report_hash    TEXT    NOT NULL,
        signing_key    TEXT    NOT NULL,
        verified_at    INTEGER NOT NULL,
        expires_at     INTEGER NOT NULL,
        policy_version TEXT    NOT NULL,
        PRIMARY KEY (provider, model, report_hash)
    ) STRICT
    """,
    # usage_local — soft counter; the relay meter is authoritative (spec §4.4).
    """
    CREATE TABLE IF NOT EXISTS usage_local (
        vault_id         TEXT    NOT NULL REFERENCES vaults (vault_id) ON DELETE CASCADE,
        month            TEXT    NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
        ingests          INTEGER NOT NULL DEFAULT 0,
        inference_tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (vault_id, month)
    ) STRICT
    """,
    # audit_log — never plaintext, tokens or keys (writer: totalreclaw_enclave.audit).
    # No FK: audit rows outlive an unpaired vault; vault_hash is not the vault id.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        ts         INTEGER NOT NULL,
        vault_hash TEXT,
        event      TEXT    NOT NULL,
        details    TEXT    NOT NULL DEFAULT '{}'
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts)",
)

MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial_schema", _V1_STATEMENTS),)


def _check_sequence(migrations: Sequence[Migration]) -> None:
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise MigrationError("migration versions must be contiguous from 1")


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    return [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def apply_migrations(
    conn: sqlite3.Connection, *, now: int, migrations: Sequence[Migration] = MIGRATIONS
) -> list[int]:
    """Apply every pending migration, each in its own transaction.

    ``conn`` must be in autocommit mode (``isolation_level=None``). Returns the
    versions applied by this call (``[]`` when the schema is current).
    Refuses a database whose schema is newer than this build.
    """
    _check_sequence(migrations)
    done = set(applied_versions(conn))
    if done and max(done) > len(migrations):
        raise MigrationError("database schema is newer than this build; refusing to start")
    applied: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                conn.execute(statement)
            for step in migration.steps:
                step(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, now),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied.append(migration.version)
    return applied
