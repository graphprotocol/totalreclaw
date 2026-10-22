"""``audit_log`` writer (spec §3.1: "Never contains plaintext, tokens or keys").

``AuditLogger.record`` validates BEFORE writing and raises
``AuditFieldRejected`` for anything plaintext-shaped; nothing is written on
rejection. The check (see ``redaction.py`` for the shared definitions):

* ``event``: dotted lowercase name, 2-4 segments, <= 64 chars
  (full match of ``[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*){1,3}``), e.g. ``enclave.boot``.
* ``vault_id`` (optional): ``0x`` + 40 hex; stored only as ``vault_hash``
  (16 hex chars from the injected ``VaultHasher``), never raw.
* ``details``: a flat mapping of at most 16 entries whose keys pass
  ``key_violation`` and whose values pass ``value_violation``; serialized
  JSON <= 1024 bytes.

Rejection messages name the key and the rule, never the value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Final

from totalreclaw_enclave.clock import Clock
from totalreclaw_enclave.db import Database
from totalreclaw_enclave.errors import SafeMessageError
from totalreclaw_enclave.redaction import key_violation, value_violation

AuditValue = str | int | float | bool | None
VaultHasher = Callable[[str], str]

_EVENT_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}")
_VAULT_ID_RE = re.compile(r"0x[0-9a-f]{40}")
_VAULT_HASH_RE = re.compile(r"[0-9a-f]{16}")
MAX_DETAIL_ENTRIES: Final = 16
MAX_DETAILS_BYTES: Final = 1024
_UNKEYED_TAG: Final = b"tr-enclave-audit-v1\x00"


class AuditFieldRejected(SafeMessageError, ValueError):
    """A field was plaintext-shaped. The message never contains the value."""


def unkeyed_vault_hash(vault_id: str) -> str:
    """Placeholder ``VaultHasher`` until ENC-3 injects a root-keyed one.

    SHA-256 over a domain tag and the lowercase address, first 16 hex chars.
    Unkeyed, so it only hides the address from casual reading (a Smart
    Account address is public on Gnosis); ENC-3 replaces it with
    HMAC-SHA256(key derived from the sealing root) so operators cannot link
    audit rows to vaults.
    """
    return hashlib.sha256(_UNKEYED_TAG + vault_id.lower().encode("ascii")).hexdigest()[:16]


def validate_audit_record(event: str, vault_id: str | None, details: Mapping[str, AuditValue] | None) -> str:
    """Validate an audit record; return the canonical ``details`` JSON."""
    if not isinstance(event, str) or len(event) > 64 or not _EVENT_RE.fullmatch(event):
        raise AuditFieldRejected("event must be a dotted lowercase name of 2-4 segments, <= 64 chars")
    if vault_id is not None and (
        not isinstance(vault_id, str) or not _VAULT_ID_RE.fullmatch(vault_id.lower())
    ):
        raise AuditFieldRejected("vault_id must be 0x followed by 40 hex characters")
    details = dict(details or {})
    if len(details) > MAX_DETAIL_ENTRIES:
        raise AuditFieldRejected(f"details has more than {MAX_DETAIL_ENTRIES} entries")
    for key, value in details.items():
        reason = key_violation(key)
        if reason is not None:
            raise AuditFieldRejected(f"details key rejected: {reason}")
        reason = value_violation(value)
        if reason is not None:
            raise AuditFieldRejected(f"details['{key}'] rejected: {reason}")
    encoded = json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise AuditFieldRejected(f"details exceeds {MAX_DETAILS_BYTES} bytes")
    return encoded


class AuditLogger:
    def __init__(self, db: Database, clock: Clock, vault_hasher: VaultHasher = unkeyed_vault_hash) -> None:
        self._db = db
        self._clock = clock
        self._vault_hasher = vault_hasher

    def vault_hash(self, vault_id: str) -> str:
        """The value stored in ``audit_log.vault_hash`` (also safe as a log field)."""
        hashed = self._vault_hasher(vault_id.lower())
        if not _VAULT_HASH_RE.fullmatch(hashed):
            raise AuditFieldRejected("vault_hasher must return 16 lowercase hex characters")
        return hashed

    async def record(
        self,
        event: str,
        *,
        vault_id: str | None = None,
        details: Mapping[str, AuditValue] | None = None,
    ) -> None:
        encoded = validate_audit_record(event, vault_id, details)
        vault_hash = self.vault_hash(vault_id) if vault_id is not None else None
        await self._db.execute(
            "INSERT INTO audit_log (ts, vault_hash, event, details) VALUES (?, ?, ?, ?)",
            (self._clock.now(), vault_hash, event, encoded),
        )
