"""
totalreclaw.bundle — ``derived-bundle-v1`` credential bundle (Option E Phase 2).

What an agent host holds *instead of* the BIP-39 recovery phrase: the four
HKDF-derived vault-global working keys, plus a signing key, but never the
phrase or the 64-byte seed that reproduces it. See
``docs/specs/totalreclaw/client-consistency.md`` ("Credential Material") for
the full cross-client contract.

Mirrors ``totalreclaw.crypto``'s shape exactly: frozen dataclasses plus
functions delegating to ``totalreclaw_core``. No logic lives here —
derivation, parsing, and validation are pure computation and belong in
``rust/totalreclaw-core/src/bundle.rs`` (CLAUDE.md's shared-core-first
architecture rule).

Kept as a module separate from ``crypto.py`` deliberately: a bundle is a
credential concern, not a crypto primitive, and folding bundle helpers into
``crypto.py`` would make the import graph misleading (see
phase2-implementation-spec.md P2-5, "things that will tempt you").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import totalreclaw_core

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultKeys:
    """The four HKDF-derived vault-global working keys — SECRET."""

    encryption_key: bytes
    dedup_key: bytes
    auth_key: bytes
    lsh_seed: bytes

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return "VaultKeys(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class SigningMaterial:
    """The signing half — ``kind`` is the only non-secret field besides
    ``address``. ``private_key`` is SECRET; treat with the same care as the
    recovery phrase under ``kind == "owner-eoa"``.

    ``grant`` is the raw ``SessionKeyPermissionGrant`` JSON string when
    ``kind == "session-key"`` (Phase 3; not derivable by this client yet —
    see ``totalreclaw_core.derive_bundle_from_mnemonic``, which only ever
    produces ``"owner-eoa"``), else ``None``.
    """

    kind: str
    private_key: bytes
    address: str
    grant: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return (
            f"SigningMaterial(kind={self.kind!r}, address={self.address!r}, "
            f"private_key=<redacted>, grant=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class AccountRef:
    """Non-secret — the on-chain identity this bundle authenticates for."""

    smart_account: str
    chain_id: int


@dataclass(frozen=True)
class DerivedBundle:
    """A parsed, fully-validated ``derived-bundle-v1`` credential bundle.

    Deliberately has no seed field and no mnemonic field — see the module
    docstring. ``__repr__``/``__str__`` redact every secret field: a bundle
    reaching a traceback or a log line is the failure mode this whole phase
    exists to prevent, and the default dataclass repr would print
    everything.
    """

    vault: VaultKeys
    signing: SigningMaterial
    account: AccountRef
    provisioned_at: str
    provisioned_by: str

    def __repr__(self) -> str:
        return (
            "DerivedBundle(vault=<redacted>, "
            f"signing=SigningMaterial(kind={self.signing.kind!r}, "
            f"address={self.signing.address!r}, <redacted>), "
            f"account={self.account!r}, "
            f"provisioned_at={self.provisioned_at!r}, "
            f"provisioned_by={self.provisioned_by!r})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Functions — thin delegation to totalreclaw_core (PyO3)
# ---------------------------------------------------------------------------


def derive_bundle_from_mnemonic(
    mnemonic: str, chain_id: int, provisioned_by: str, smart_account: str
) -> DerivedBundle:
    """Derive a ``derived-bundle-v1`` bundle from a BIP-39 mnemonic.

    A **composition**, not a reimplementation — delegates entirely to
    ``totalreclaw_core.derive_bundle_from_mnemonic``, which composes the
    existing ``derive_keys_from_mnemonic`` + ``derive_lsh_seed`` +
    ``derive_eoa`` outputs. No new cryptography happens in this package.

    ``smart_account``: the CREATE2 Smart Account address. Required — this
    crate has no CREATE2 helper (the address comes from an ``eth_call`` RPC
    round-trip today; see ``client.py::_derive_smart_account_address``).
    """
    json_str = totalreclaw_core.derive_bundle_from_mnemonic(
        mnemonic, chain_id, provisioned_by, smart_account
    )
    return parse_bundle_v1(json_str)


def parse_bundle_v1(json_str: str) -> DerivedBundle:
    """Parse and fully validate a ``derived-bundle-v1`` JSON string.

    Raises ``ValueError`` on any violation of derived-bundle-v1.md §4.7 —
    unknown ``version``/``schema``, malformed hex, an unknown
    ``signing.kind``, a ``grant``/no-``grant`` mismatch with ``kind``, or an
    address/private-key mismatch. Delegates entirely to
    ``totalreclaw_core.parse_bundle_v1``.
    """
    parsed = totalreclaw_core.parse_bundle_v1(json_str)
    return DerivedBundle(
        vault=VaultKeys(
            encryption_key=parsed["vault"]["encryption_key"],
            dedup_key=parsed["vault"]["dedup_key"],
            auth_key=parsed["vault"]["auth_key"],
            lsh_seed=parsed["vault"]["lsh_seed"],
        ),
        signing=SigningMaterial(
            kind=parsed["signing"]["kind"],
            private_key=parsed["signing"]["private_key"],
            address=parsed["signing"]["address"],
            grant=parsed["signing"].get("grant"),
        ),
        account=AccountRef(
            smart_account=parsed["account"]["smart_account"],
            chain_id=parsed["account"]["chain_id"],
        ),
        provisioned_at=parsed["provisioned_at"],
        provisioned_by=parsed["provisioned_by"],
    )


def validate_bundle_v1(json_str: str) -> None:
    """Validate a ``derived-bundle-v1`` JSON string.

    Raises ``ValueError`` on any violation; returns ``None`` on success.
    Delegates entirely to ``totalreclaw_core.validate_bundle_v1``.
    """
    totalreclaw_core.validate_bundle_v1(json_str)
