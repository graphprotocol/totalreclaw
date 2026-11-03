from __future__ import annotations

import math

import pytest

from tests.support import FAKE_PHRASE, FAKE_TOKEN, MEMORY_TEXT, SENTINELS, T0, VAULT_A
from totalreclaw_enclave.audit import AuditFieldRejected, AuditLogger, unkeyed_vault_hash
from totalreclaw_enclave.clock import FixedClock
from totalreclaw_enclave.db import Database


async def _rows(db: Database) -> list[tuple[object, ...]]:
    return [tuple(r) for r in await db.fetchall("SELECT ts, vault_hash, event, details FROM audit_log")]


async def test_records_a_clean_event(db: Database, clock: FixedClock) -> None:
    audit = AuditLogger(db, clock)
    await audit.record(
        "oauth.client.registered",
        vault_id=VAULT_A,
        details={"kind": "dcr", "count": 2, "ok": True, "ratio": 0.5, "note": None},
    )
    [(ts, vault_hash, event, details)] = await _rows(db)
    assert ts == T0
    assert event == "oauth.client.registered"
    assert vault_hash == unkeyed_vault_hash(VAULT_A)
    assert len(vault_hash) == 16 and vault_hash != VAULT_A
    assert details == '{"count":2,"kind":"dcr","note":null,"ok":true,"ratio":0.5}'


async def test_raw_vault_id_is_never_stored(db: Database, clock: FixedClock) -> None:
    await AuditLogger(db, clock).record("vault.paired", vault_id=VAULT_A.upper().replace("0X", "0x"))
    for row in await _rows(db):
        for column in row:
            assert VAULT_A[2:] not in str(column).lower()


@pytest.mark.parametrize(
    "event",
    ["boot", "Enclave.Boot", "enclave boot", "enclave.", "a.b.c.d.e", "x" * 70 + ".y", "enclave.boot\n"],
)
async def test_rejects_malformed_event_names(db: Database, clock: FixedClock, event: str) -> None:
    with pytest.raises(AuditFieldRejected, match="event"):
        await AuditLogger(db, clock).record(event)
    assert await _rows(db) == []


@pytest.mark.parametrize(
    "key",
    [
        "text", "query", "memory_text", "fact", "message_count", "prompt", "turn",
        "phrase", "recovery_phrase", "mnemonic", "seed", "secret", "password",
        "token", "access_token", "api_key", "key", "code", "code_verifier", "bundle",
        "wallet_address", "eoa", "payload", "ciphertext", "Kind", "has space", "",
    ],
)  # fmt: skip
async def test_rejects_denied_or_malformed_keys(db: Database, clock: FixedClock, key: str) -> None:
    with pytest.raises(AuditFieldRejected, match="key"):
        await AuditLogger(db, clock).record("a.b", details={key: "x"})
    assert await _rows(db) == []


@pytest.mark.parametrize(
    "value",
    [
        MEMORY_TEXT,
        FAKE_PHRASE,
        FAKE_TOKEN,
        "eyJhbGciOiJFUzI1NiJ9",
        "0x" + "ab" * 32,  # private-key shape
        VAULT_A,  # an address in details defeats vault_hash
        "a" * 24,  # 24-char alnum run
        '{"k":"v"}',
        "x" * 65,
        "line\nbreak",
        {"nested": 1},
        ["a"],
        b"bytes",
        math.nan,
        2**64,
    ],
)
async def test_rejects_plaintext_shaped_values(db: Database, clock: FixedClock, value: object) -> None:
    with pytest.raises(AuditFieldRejected, match="rejected"):
        await AuditLogger(db, clock).record("a.b", details={"field": value})  # type: ignore[dict-item]
    assert await _rows(db) == []


async def test_rejection_message_never_contains_the_value(db: Database, clock: FixedClock) -> None:
    audit = AuditLogger(db, clock)
    for value in (MEMORY_TEXT, FAKE_PHRASE, FAKE_TOKEN):
        with pytest.raises(AuditFieldRejected) as info:
            await audit.record("a.b", details={"field": value})
        for sentinel in SENTINELS:
            assert sentinel not in str(info.value)


async def test_accepts_short_identifiers_and_urls(db: Database, clock: FixedClock) -> None:
    await AuditLogger(db, clock).record(
        "oauth.client.seen",
        details={
            "client_id": "https://grok.com/.well-known/oauth-client.json",
            "ingest_ref": "3f9a1c07b2e4",
            "signing_kind": "session-key",
            "model": "glm-5.3-flash",
        },
    )
    assert len(await _rows(db)) == 1


@pytest.mark.parametrize("vault_id", ["0x1234", "a1" * 21, "0x" + "g1" * 20, ""])
async def test_rejects_malformed_vault_id(db: Database, clock: FixedClock, vault_id: str) -> None:
    with pytest.raises(AuditFieldRejected, match="vault_id"):
        await AuditLogger(db, clock).record("a.b", vault_id=vault_id)


async def test_rejects_oversized_details(db: Database, clock: FixedClock) -> None:
    audit = AuditLogger(db, clock)
    with pytest.raises(AuditFieldRejected, match="entries"):
        await audit.record("a.b", details={f"k{i}": i for i in range(17)})
    with pytest.raises(AuditFieldRejected, match="bytes"):
        await audit.record("a.b", details={f"k{i}": "ab-" * 21 for i in range(16)})


async def test_injected_vault_hasher_is_used_and_checked(db: Database, clock: FixedClock) -> None:
    await AuditLogger(db, clock, vault_hasher=lambda _v: "0123456789abcdef").record("a.b", vault_id=VAULT_A)
    assert (await _rows(db))[0][1] == "0123456789abcdef"
    with pytest.raises(AuditFieldRejected, match="vault_hasher"):
        await AuditLogger(db, clock, vault_hasher=lambda v: v).record("a.b", vault_id=VAULT_A)


async def test_checksummed_vault_id_is_lowercased_before_hashing(db: Database, clock: FixedClock) -> None:
    checksummed = "0x" + "A1b2" * 10
    await AuditLogger(db, clock).record("vault.seen", vault_id=checksummed)
    assert (await _rows(db))[0][1] == unkeyed_vault_hash(checksummed.lower())


async def test_non_ascii_values_are_refused(db: Database, clock: FixedClock) -> None:
    with pytest.raises(AuditFieldRejected, match="rejected"):
        await AuditLogger(db, clock).record("a.b", details={"reason": "médico"})
