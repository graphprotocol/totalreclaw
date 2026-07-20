"""
TotalReclaw E2EE crypto primitives.

Delegates to the totalreclaw_core Rust/PyO3 module for all crypto operations.
Maintains the same Python API for backward compatibility.
"""
from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass

import totalreclaw_core

# Fixed production embedding dim — used to infer dtype on read (see
# decrypt_embedding). Imported here rather than re-declared so there is a
# single source of truth.
from totalreclaw.embedding import EMBEDDING_DIMS

# ---------------------------------------------------------------------------
# Key Derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedKeys:
    salt: bytes
    auth_key: bytes
    encryption_key: bytes
    dedup_key: bytes


def derive_keys_from_mnemonic(mnemonic: str) -> DerivedKeys:
    """Derive all crypto keys from a BIP-39 mnemonic.

    Delegates to totalreclaw_core (Rust/PyO3).
    """
    result = totalreclaw_core.derive_keys_from_mnemonic(mnemonic)
    return DerivedKeys(
        salt=result["salt"],
        auth_key=result["auth_key"],
        encryption_key=result["encryption_key"],
        dedup_key=result["dedup_key"],
    )


def compute_auth_key_hash(auth_key: bytes) -> str:
    """SHA-256(authKey) as hex string."""
    return totalreclaw_core.compute_auth_key_hash(auth_key)


def derive_lsh_seed(mnemonic: str, salt: bytes) -> bytes:
    """Derive the 32-byte LSH seed."""
    return totalreclaw_core.derive_lsh_seed(mnemonic, salt)


# ---------------------------------------------------------------------------
# XChaCha20-Poly1305 Encryption / Decryption
# ---------------------------------------------------------------------------


def encrypt(plaintext: str, encryption_key: bytes) -> str:
    """Encrypt with XChaCha20-Poly1305.

    Wire format: nonce(24) || tag(16) || ciphertext -> base64.
    """
    return totalreclaw_core.encrypt(plaintext, encryption_key)


def decrypt(encrypted_base64: str, encryption_key: bytes) -> str:
    """Decrypt XChaCha20-Poly1305.

    Expects wire format: nonce(24) || tag(16) || ciphertext.
    """
    return totalreclaw_core.decrypt(encrypted_base64, encryption_key)


# ---------------------------------------------------------------------------
# Blind Indices + Stemming
# ---------------------------------------------------------------------------


def generate_blind_indices(text: str) -> list[str]:
    """Generate blind indices (SHA-256 hashes of tokens).

    Tokenization rules (matches canonical TypeScript/Rust implementation):
      1. Lowercase
      2. Remove punctuation (keep Unicode letters, numbers, whitespace)
      3. Split on whitespace
      4. Filter tokens shorter than 2 characters

    Each surviving token is SHA-256 hashed and returned as a hex string.
    Stemmed variants are prefixed with "stem:" before hashing.
    The returned array is deduplicated.
    """
    return totalreclaw_core.generate_blind_indices(text)


# ---------------------------------------------------------------------------
# Content Fingerprint (Dedup)
# ---------------------------------------------------------------------------


def generate_content_fingerprint(plaintext: str, dedup_key: bytes) -> str:
    """HMAC-SHA256 content fingerprint. Returns 64-char hex string."""
    return totalreclaw_core.generate_content_fingerprint(plaintext, dedup_key)


# ---------------------------------------------------------------------------
# Embedding Encryption
# ---------------------------------------------------------------------------


def encrypt_embedding(embedding: list[float], encryption_key: bytes) -> str:
    """Encrypt embedding: pack as LE half-precision (f16) array -> base64 -> encrypt.

    Production embeddings are unit-normalized floats in ~[-1, 1] (Harrier
    640-dim), all representable in IEEE-754 half precision. struct's ``'e'``
    format code packs IEEE-754 half, halving the embedding portion of the
    stored blob (2560B -> 1280B for a 640-dim vector) at no retrieval-quality
    cost (round-trip cosine p99 = 1.0; see research/2026-07-06-f16-...-poc.md).
    """
    # #479 Part B: prefer the core codec when the installed totalreclaw_core
    # wheel exposes it (canonical rules + fail-closed NaN/inf/overflow guard —
    # deliberately stricter than this struct fallback, see core's
    # embedding_codec.rs). Same prefer-core-with-fallback pattern as
    # segment_sessions: no release-order dependency on the core wheel.
    core_encode = getattr(totalreclaw_core, "encode_embedding_canonical", None)
    if core_encode is not None:
        return encrypt(core_encode(list(embedding)), encryption_key)
    buf = struct.pack(f"<{len(embedding)}e", *embedding)
    return encrypt(base64.b64encode(buf).decode("ascii"), encryption_key)


def decrypt_embedding(
    encrypted_embedding: str, encryption_key: bytes
) -> list[float]:
    """Decrypt embedding back to float array, inferring dtype from byte length.

    The dtype is keyed on the fixed production dim rather than a wire marker
    (a header byte would break legacy f32 decode):

      - ``len(buf) == EMBEDDING_DIMS * 2`` -> f16 (new 640-dim writes, 1280B).
        Production embeddings are ALWAYS 640-dim, so a 1280B buffer can only be
        a new f16 write — the length alone is unambiguous here.
      - otherwise -> f32 (legacy 640-dim blobs written before this change at
        2560B, AND any non-640-dim vector such as the 1024-dim test fixture).

    This guarantees every pre-existing f32 embedding on-chain still decodes
    (2560B -> f32) while new 640-dim writes are f16 (1280B).
    """
    decrypted_payload = decrypt(encrypted_embedding, encryption_key)

    # #479 Part B: universal decode. Prefer the core decoder when available;
    # the local fallback implements the same dispatch, INCLUDING the legacy
    # TS-plugin JSON-array format this client previously could not read at
    # all (base64.b64decode choked on "[0.1, ...]" — the Hermes-reading-
    # plugin-facts half of the parity bug).
    core_decode = getattr(totalreclaw_core, "decode_embedding_universal", None)
    if core_decode is not None:
        return [float(v) for v in core_decode(decrypted_payload)]

    trimmed = decrypted_payload.strip()
    if trimmed.startswith("["):
        # Legacy TS plugin format: JSON float array.
        parsed = json.loads(trimmed)
        if not isinstance(parsed, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in parsed
        ):
            raise ValueError("JSON embedding payload is not an array of numbers")
        return [float(v) for v in parsed]

    buf = base64.b64decode(trimmed, validate=True)
    if len(buf) == EMBEDDING_DIMS * 2:
        # Canonical f16 write (640-dim). Upcast half -> Python float.
        return list(struct.unpack(f"<{len(buf) // 2}e", buf))
    if len(buf) % 4 == 0:
        # Legacy f32 blob (any dim, incl. pre-change 640-dim + non-production dims).
        count = len(buf) // 4
        return list(struct.unpack(f"<{count}f", buf))
    raise ValueError(
        f"decoded embedding buffer has length {len(buf)} (expected "
        f"{EMBEDDING_DIMS * 2} for f16, or a multiple of 4 for f32)"
    )
