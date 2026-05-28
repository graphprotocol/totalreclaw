"""
EIP-712 typed-data payload for ``SessionKeyPermissionGrant`` — Python side.

Mirrors the Solidity ``SessionKeyModule._grantDigest`` construction byte-for-byte
so a Python-signed grant verifies on-chain and a TS/viem-signed grant verifies
in Python. The cross-language parity fixture (cred-9) is the locking test for
that invariant.

Spec: ``docs/specs/cred/session-key-delegation.md`` §3.1, §4.2, §4.3.
Solidity reference: ``contracts/contracts/SessionKeyModule.sol`` (cred-5
stage 2, PR #272 — merged 2026-05-27).

Cross-spec invariants enforced here:
- Domain typehash, scope typehash, and grant typehash strings are duplicated
  verbatim from ``SessionKeyModule.sol``. Any drift breaks on-chain verify.
- ``selectors`` are hashed via ``keccak256(abi.encodePacked(selectors))`` —
  i.e. 4-byte selectors concatenated, NOT ABI-encoded with length prefix.
  This matches the Solidity ``keccak256(abi.encodePacked(g.selectors))``.
- Domain name is ``"TotalReclawSessionKey"``, version ``"1"``. ``chainId``
  + ``verifyingContract`` (the SessionKeyModule address) are the binding
  fields that prevent cross-chain / cross-module replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys
from eth_utils import keccak, to_checksum_address

# ---------------------------------------------------------------------------
# Typehashes — MUST match SessionKeyModule.sol byte-for-byte
# ---------------------------------------------------------------------------

DOMAIN_NAME: str = "TotalReclawSessionKey"
DOMAIN_VERSION: str = "1"

DOMAIN_TYPE_STR: bytes = (
    b"EIP712Domain(string name,string version,uint256 chainId,"
    b"address verifyingContract)"
)
SCOPE_TYPE_STR: bytes = b"Scope(address target,bytes4[] selectors,uint256 valueMax)"
GRANT_TYPE_STR: bytes = (
    b"SessionKeyPermissionGrant(address account,address signer,Scope scope,"
    b"uint256 nonce,uint256 issuedAt)"
    b"Scope(address target,bytes4[] selectors,uint256 valueMax)"
)

DOMAIN_TYPEHASH: bytes = keccak(DOMAIN_TYPE_STR)
SCOPE_TYPEHASH: bytes = keccak(SCOPE_TYPE_STR)
GRANT_TYPEHASH: bytes = keccak(GRANT_TYPE_STR)
DOMAIN_NAME_HASH: bytes = keccak(DOMAIN_NAME.encode())
DOMAIN_VERSION_HASH: bytes = keccak(DOMAIN_VERSION.encode())

# Selector constants (mirrors SessionKeyModule.sol).
EXECUTE_SELECTOR: bytes = bytes.fromhex("b61d27f6")
EXECUTE_BATCH_SELECTOR: bytes = bytes.fromhex("47e1da2a")

# Grant-format version. Bump requires a fresh module deployment.
GRANT_VERSION: int = 1


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


def _normalize_selectors(selectors: Sequence[bytes | str]) -> list[bytes]:
    """Coerce mixed-form selectors to a list of exactly-4-byte bytes."""
    out: list[bytes] = []
    for s in selectors:
        if isinstance(s, str):
            s = bytes.fromhex(s.removeprefix("0x"))
        if not isinstance(s, (bytes, bytearray)) or len(s) != 4:
            raise ValueError(f"selector must be exactly 4 bytes; got {s!r}")
        out.append(bytes(s))
    return out


@dataclass(frozen=True)
class SessionKeyPermissionGrant:
    """EIP-712 typed-data grant authorising a session signer on a Smart Account.

    Field ordering matches the Solidity ``PermissionGrant`` struct used for the
    lazy-install ABI encoding. ``master_signature`` is filled AFTER signing —
    construct the grant without it, compute :meth:`eip712_digest`, sign with the
    master wallet, then return a new grant via :meth:`with_signature`.
    """

    account: str
    """Smart Account (CREATE2) address — the SA the grant binds to."""

    signer: str
    """EOA address derived from the session private key."""

    target: str
    """Scope target — the DataEdge contract address."""

    selectors: tuple[bytes, ...]
    """Scope selectors — typically ``(execute_selector, execute_batch_selector)``."""

    value_max: int
    """Scope value_max — MUST be 0 (session keys cannot move ETH)."""

    nonce: int
    """Monotonic per-(account, signer) install nonce. ``>=1`` for an installable grant."""

    issued_at: int
    """Unix-seconds timestamp. Informational only — no on-chain TTL check."""

    chain_id: int
    """EVM chain id — replay-protection field. Module rejects mismatches."""

    verifying_contract: str
    """SessionKeyModule address on this chain — replay-protection field."""

    version: int = GRANT_VERSION
    """Grant format version. Module rejects unknown versions."""

    master_signature: bytes = b""
    """65-byte ECDSA (r ‖ s ‖ v) from master wallet over :meth:`eip712_digest`."""

    # -----------------------------------------------------------------
    # Construction helpers
    # -----------------------------------------------------------------

    def with_signature(self, signature: bytes) -> "SessionKeyPermissionGrant":
        """Return a copy with ``master_signature`` populated."""
        if len(signature) != 65:
            raise ValueError(
                f"master_signature must be exactly 65 bytes; got {len(signature)}"
            )
        return SessionKeyPermissionGrant(
            account=self.account,
            signer=self.signer,
            target=self.target,
            selectors=self.selectors,
            value_max=self.value_max,
            nonce=self.nonce,
            issued_at=self.issued_at,
            chain_id=self.chain_id,
            verifying_contract=self.verifying_contract,
            version=self.version,
            master_signature=signature,
        )

    # -----------------------------------------------------------------
    # EIP-712 digest — MUST match SessionKeyModule._grantDigest byte-for-byte
    # -----------------------------------------------------------------

    def scope_struct_hash(self) -> bytes:
        """``keccak256(abi.encode(SCOPE_TYPEHASH, target, keccak(packedSelectors), valueMax))``."""
        selectors_norm = _normalize_selectors(self.selectors)
        packed_selectors_hash = keccak(b"".join(selectors_norm))
        return keccak(
            abi_encode(
                ["bytes32", "address", "bytes32", "uint256"],
                [
                    SCOPE_TYPEHASH,
                    to_checksum_address(self.target),
                    packed_selectors_hash,
                    self.value_max,
                ],
            )
        )

    def struct_hash(self) -> bytes:
        """``keccak256(abi.encode(GRANT_TYPEHASH, account, signer, scopeHash, nonce, issuedAt))``."""
        return keccak(
            abi_encode(
                ["bytes32", "address", "address", "bytes32", "uint256", "uint256"],
                [
                    GRANT_TYPEHASH,
                    to_checksum_address(self.account),
                    to_checksum_address(self.signer),
                    self.scope_struct_hash(),
                    self.nonce,
                    self.issued_at,
                ],
            )
        )

    def domain_separator(self) -> bytes:
        """``keccak256(abi.encode(DOMAIN_TYPEHASH, nameHash, versionHash, chainId, verifyingContract))``."""
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    DOMAIN_TYPEHASH,
                    DOMAIN_NAME_HASH,
                    DOMAIN_VERSION_HASH,
                    self.chain_id,
                    to_checksum_address(self.verifying_contract),
                ],
            )
        )

    def eip712_digest(self) -> bytes:
        """``keccak256(0x19 0x01 ‖ domainSeparator ‖ structHash)`` — the byte sequence
        the master wallet signs."""
        return keccak(b"\x19\x01" + self.domain_separator() + self.struct_hash())

    # -----------------------------------------------------------------
    # Sign / recover — raw secp256k1 (NO EIP-191 prefix; matches Solidity ecrecover)
    # -----------------------------------------------------------------

    def sign(self, master_priv_key: bytes) -> "SessionKeyPermissionGrant":
        """Sign the digest with the master wallet private key and return a
        grant carrying the 65-byte ECDSA signature.

        Uses raw secp256k1 over the EIP-712 digest (NO EIP-191
        ``\\x19Ethereum Signed Message:\\n32`` prefix) — matches Solidity's
        ``ecrecover(_grantDigest(...), v, r, s)`` exactly.
        """
        return self.with_signature(sign_digest(self.eip712_digest(), master_priv_key))

    def recover_master(self) -> str:
        """Recover the master wallet address from ``master_signature``.

        Raises if no signature is attached. Returns a checksum-cased address.
        """
        if len(self.master_signature) != 65:
            raise ValueError("master_signature is empty — call .sign() first")
        return recover_address(self.eip712_digest(), self.master_signature)

    # -----------------------------------------------------------------
    # Lazy-install ABI encoding
    # -----------------------------------------------------------------

    def to_install_struct_tuple(self) -> tuple:
        """Tuple matching Solidity ``PermissionGrant`` field order.

        Field order (LOAD-BEARING — see SessionKeyModule.sol struct):
          version, account, signer, target, selectors, valueMax,
          nonce, issuedAt, chainId, verifyingContract, masterSignature
        """
        return (
            self.version,
            to_checksum_address(self.account),
            to_checksum_address(self.signer),
            to_checksum_address(self.target),
            _normalize_selectors(self.selectors),
            self.value_max,
            self.nonce,
            self.issued_at,
            self.chain_id,
            to_checksum_address(self.verifying_contract),
            self.master_signature,
        )


# ---------------------------------------------------------------------------
# Module-level signing primitives (kept module-public so users who want to
# sign a UserOp hash with a session key without instantiating a Grant can
# reuse the same raw-secp256k1 path).
# ---------------------------------------------------------------------------


def sign_digest(digest: bytes, priv_key: bytes) -> bytes:
    """Sign a 32-byte digest with raw secp256k1 — NO EIP-191 prefix.

    Returns 65 bytes ``r ‖ s ‖ v`` with ``v`` in {27, 28} per Ethereum
    convention. Matches the Solidity ``ecrecover(digest, v, r, s)`` shape
    used inside ``SessionKeyModule._recoverEcdsa``.
    """
    if len(digest) != 32:
        raise ValueError(f"digest must be 32 bytes; got {len(digest)}")
    if len(priv_key) != 32:
        raise ValueError(f"priv_key must be 32 bytes; got {len(priv_key)}")
    sig = eth_keys.PrivateKey(priv_key).sign_msg_hash(digest)
    # eth_keys.Signature.v is 0/1; Ethereum wire format uses 27/28.
    return (
        sig.r.to_bytes(32, "big")
        + sig.s.to_bytes(32, "big")
        + bytes([sig.v + 27])
    )


def recover_address(digest: bytes, signature: bytes) -> str:
    """Recover a checksum-cased address from a 65-byte ``r‖s‖v`` signature
    over the given 32-byte digest.

    Inverse of :func:`sign_digest`. Tolerates ``v`` in {0, 1, 27, 28}.
    """
    if len(digest) != 32:
        raise ValueError(f"digest must be 32 bytes; got {len(digest)}")
    if len(signature) != 65:
        raise ValueError(f"signature must be 65 bytes; got {len(signature)}")
    r = int.from_bytes(signature[0:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    v_raw = signature[64]
    v_normalized = v_raw - 27 if v_raw in (27, 28) else v_raw
    sig = eth_keys.Signature(vrs=(v_normalized, r, s))
    return sig.recover_public_key_from_msg_hash(digest).to_checksum_address()


# ABI signature of the PermissionGrant tuple — kept module-level so both the
# encoder and any future decoder share a single source of truth.
PERMISSION_GRANT_TUPLE_ABI: str = (
    "(uint8,address,address,address,bytes4[],uint256,uint256,uint256,"
    "uint256,address,bytes)"
)


def encode_install_signature(
    grant: SessionKeyPermissionGrant, ecdsa_sig: bytes
) -> bytes:
    """ABI-encode ``(PermissionGrant, bytes)`` for the lazy-install UserOp signature.

    Matches the layout ``SessionKeyModule._decodeInstallSig`` decodes via
    ``abi.decode(sig, (PermissionGrant, bytes))``.
    """
    if len(ecdsa_sig) != 65:
        raise ValueError(
            f"ecdsa_sig must be 65 bytes (r||s||v); got {len(ecdsa_sig)}"
        )
    if len(grant.master_signature) != 65:
        raise ValueError(
            "grant.master_signature must be set before encoding install sig"
        )
    return abi_encode(
        [PERMISSION_GRANT_TUPLE_ABI, "bytes"],
        [grant.to_install_struct_tuple(), ecdsa_sig],
    )


__all__ = [
    "DOMAIN_NAME",
    "DOMAIN_VERSION",
    "DOMAIN_TYPE_STR",
    "SCOPE_TYPE_STR",
    "GRANT_TYPE_STR",
    "DOMAIN_TYPEHASH",
    "SCOPE_TYPEHASH",
    "GRANT_TYPEHASH",
    "DOMAIN_NAME_HASH",
    "DOMAIN_VERSION_HASH",
    "EXECUTE_SELECTOR",
    "EXECUTE_BATCH_SELECTOR",
    "GRANT_VERSION",
    "PERMISSION_GRANT_TUPLE_ABI",
    "SessionKeyPermissionGrant",
    "encode_install_signature",
    "sign_digest",
    "recover_address",
]
