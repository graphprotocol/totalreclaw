"""
TotalReclaw E2EE crypto primitives.

Delegates to the totalreclaw_core Rust/PyO3 module for all crypto operations.
Maintains the same Python API for backward compatibility.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

import totalreclaw_core

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
    """Encrypt embedding: pack as LE float32 array -> base64 -> encrypt."""
    buf = struct.pack(f"<{len(embedding)}f", *embedding)
    return encrypt(base64.b64encode(buf).decode("ascii"), encryption_key)


def decrypt_embedding(
    encrypted_embedding: str, encryption_key: bytes
) -> list[float]:
    """Decrypt embedding back to float array."""
    decrypted_base64 = decrypt(encrypted_embedding, encryption_key)
    buf = base64.b64decode(decrypted_base64)
    count = len(buf) // 4
    return list(struct.unpack(f"<{count}f", buf))
