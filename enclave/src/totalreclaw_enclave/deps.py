"""Dependency container passed to every subsystem.

``Deps`` is built once per process (``build_deps``) and handed to each
``Subsystem.routes`` / ``Subsystem.lifespan`` factory. Later leaves add
fields with defaults (never reorder existing ones).

``RootProvider`` and ``Sealer`` are typed placeholders: ENC-2 ships no
implementation and no caller. ENC-3 owns their final shape (it may extend
these Protocols in its PR), ships the dev root provider and the sealer, and
passes them to ``build_deps``; ENC-11 adds the CKD root provider.

``create_app`` calls every ``routes(settings, deps)`` factory before the
lifespan runs, so ``Deps`` exists before any root does. A ``Sealer`` (and a
root-keyed ``vault_hasher``) must therefore hold the async ``RootProvider``
and fetch the root lazily, never at construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from totalreclaw_enclave.audit import AuditLogger, VaultHasher, unkeyed_vault_hash
from totalreclaw_enclave.clock import Clock, SystemClock
from totalreclaw_enclave.db import Database
from totalreclaw_enclave.errors import SafeMessageError
from totalreclaw_enclave.settings import Settings


class NotConfiguredError(SafeMessageError):
    """A subsystem asked for a dependency this build has not wired yet."""


@runtime_checkable
class RootProvider(Protocol):
    """Source of the 32-byte sealing root (spec §8 "Key handling").

    ``kind`` is ``"dev"`` (deterministic test root, ``ENCLAVE_ENV=dev`` only;
    ENC-3) or ``"ckd"`` (NEAR MPC CKD; ENC-11). The root lives in memory only.
    """

    kind: str

    async def get_root(self) -> bytes: ...


@runtime_checkable
class Sealer(Protocol):
    """XChaCha20-Poly1305 app sealing (spec §3.1; ENC-3).

    Per-vault key = HKDF(root, "tr-enclave-seal-v1" || vault_id); per-instance
    key for ``connect_sessions.sealed_eph_priv``. ``open_*`` raises on any
    AEAD failure (wrong root, tampered blob, wrong ``aad``).
    """

    def seal_for_vault(self, vault_id: str, plaintext: bytes, *, aad: bytes) -> bytes: ...

    def open_for_vault(self, vault_id: str, sealed: bytes, *, aad: bytes) -> bytes: ...

    def seal_for_instance(self, plaintext: bytes, *, aad: bytes) -> bytes: ...

    def open_for_instance(self, sealed: bytes, *, aad: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Deps:
    settings: Settings
    db: Database
    clock: Clock
    audit: AuditLogger
    root_provider: RootProvider | None = None
    sealer: Sealer | None = None

    def require_root_provider(self) -> RootProvider:
        if self.root_provider is None:
            raise NotConfiguredError("no RootProvider is wired (ENC-3 / ENC-11)")
        return self.root_provider

    def require_sealer(self) -> Sealer:
        if self.sealer is None:
            raise NotConfiguredError("no Sealer is wired (ENC-3)")
        return self.sealer


def build_deps(
    settings: Settings,
    *,
    clock: Clock | None = None,
    root_provider: RootProvider | None = None,
    sealer: Sealer | None = None,
    vault_hasher: VaultHasher = unkeyed_vault_hash,
) -> Deps:
    """Construct (not open) the process dependencies. ``create_app`` opens the DB.

    ENC-3 / ENC-11 pass their root provider, sealer and root-keyed vault
    hasher here; ENC-2 wires none of them.
    """
    clock = clock if clock is not None else SystemClock()
    db = Database(settings.db_path)
    return Deps(
        settings=settings,
        db=db,
        clock=clock,
        audit=AuditLogger(db, clock, vault_hasher),
        root_provider=root_provider,
        sealer=sealer,
    )
