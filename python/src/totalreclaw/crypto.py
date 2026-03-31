"""
TotalReclaw E2EE crypto primitives.

Byte-for-byte compatible with mcp/src/subgraph/crypto.ts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import os
import re
import struct
import unicodedata
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from mnemonic import Mnemonic
import Stemmer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IV_LENGTH = 12
_TAG_LENGTH = 16
_KEY_LENGTH = 32

# Module-level Snowball English stemmer (Porter-compatible for our token set)
_stemmer = Stemmer.Stemmer("english")

# ---------------------------------------------------------------------------
# Key Derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedKeys:
    salt: bytes
    auth_key: bytes
    encryption_key: bytes
    dedup_key: bytes


def _hkdf_sha256(ikm: bytes, salt: bytes, info: str, length: int = 32) -> bytes:
    """Derive a key using HKDF-SHA256."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info.encode("utf-8"),
    ).derive(ikm)


def derive_keys_from_mnemonic(mnemonic: str) -> DerivedKeys:
    """Derive all crypto keys from a BIP-39 mnemonic.

    Matches mcp/src/subgraph/crypto.ts:deriveKeysFromMnemonic() exactly.
    """
    m = Mnemonic("english")
    seed = m.to_seed(mnemonic.strip())  # 64 bytes
    salt = seed[:32]
    auth_key = _hkdf_sha256(seed, salt, "totalreclaw-auth-key-v1")
    encryption_key = _hkdf_sha256(seed, salt, "totalreclaw-encryption-key-v1")
    dedup_key = _hkdf_sha256(seed, salt, "openmemory-dedup-v1")
    return DerivedKeys(
        salt=salt,
        auth_key=auth_key,
        encryption_key=encryption_key,
        dedup_key=dedup_key,
    )


def compute_auth_key_hash(auth_key: bytes) -> str:
    """SHA-256(authKey) as hex string."""
    return hashlib.sha256(auth_key).hexdigest()


def derive_lsh_seed(mnemonic: str, salt: bytes) -> bytes:
    """Derive the 32-byte LSH seed."""
    m = Mnemonic("english")
    seed = m.to_seed(mnemonic.strip())
    return _hkdf_sha256(seed, salt, "openmemory-lsh-seed-v1")


# ---------------------------------------------------------------------------
# AES-256-GCM Encryption / Decryption
# ---------------------------------------------------------------------------


def encrypt(plaintext: str, encryption_key: bytes) -> str:
    """Encrypt with AES-256-GCM.

    Wire format: iv(12) || tag(16) || ciphertext -> base64.
    """
    if len(encryption_key) != _KEY_LENGTH:
        raise ValueError(
            f"Invalid key length: expected {_KEY_LENGTH}, got {len(encryption_key)}"
        )
    iv = os.urandom(_IV_LENGTH)
    aesgcm = AESGCM(encryption_key)
    # cryptography lib returns ciphertext || tag
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext = ct_with_tag[:-_TAG_LENGTH]
    tag = ct_with_tag[-_TAG_LENGTH:]
    # Wire format: iv || tag || ciphertext (matching TypeScript)
    combined = iv + tag + ciphertext
    return base64.b64encode(combined).decode("ascii")


def decrypt(encrypted_base64: str, encryption_key: bytes) -> str:
    """Decrypt AES-256-GCM.

    Expects wire format: iv(12) || tag(16) || ciphertext.
    """
    if len(encryption_key) != _KEY_LENGTH:
        raise ValueError(
            f"Invalid key length: expected {_KEY_LENGTH}, got {len(encryption_key)}"
        )
    combined = base64.b64decode(encrypted_base64)
    if len(combined) < _IV_LENGTH + _TAG_LENGTH:
        raise ValueError("Encrypted data too short")
    iv = combined[:_IV_LENGTH]
    tag = combined[_IV_LENGTH : _IV_LENGTH + _TAG_LENGTH]
    ciphertext = combined[_IV_LENGTH + _TAG_LENGTH :]
    aesgcm = AESGCM(encryption_key)
    # cryptography lib expects ciphertext || tag
    plaintext_bytes = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext_bytes.decode("utf-8")


# ---------------------------------------------------------------------------
# Blind Indices + Stemming
# ---------------------------------------------------------------------------


def generate_blind_indices(text: str) -> list[str]:
    """Generate blind indices (SHA-256 hashes of tokens).

    Tokenization rules (matches mcp/src/subgraph/crypto.ts):
      1. Lowercase
      2. Remove punctuation (keep Unicode letters, numbers, whitespace)
      3. Split on whitespace
      4. Filter tokens shorter than 2 characters

    Each surviving token is SHA-256 hashed and returned as a hex string.
    Stemmed variants are prefixed with "stem:" before hashing.
    The returned array is deduplicated.
    """
    # Match TS: /[^\p{L}\p{N}\s]/gu
    # Python \w includes underscore but TS \p{L}\p{N} does not,
    # so we strip non-word chars then replace underscores with spaces.
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    cleaned = cleaned.replace("_", " ")
    tokens = [t for t in cleaned.split() if len(t) >= 2]

    seen: set[str] = set()
    indices: list[str] = []

    for token in tokens:
        # Exact word hash
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if h not in seen:
            seen.add(h)
            indices.append(h)

        # Stemmed word hash (prefixed with "stem:" to avoid collisions)
        stem = _stemmer.stemWord(token)
        if len(stem) >= 2 and stem != token:
            stem_hash = hashlib.sha256(f"stem:{stem}".encode("utf-8")).hexdigest()
            if stem_hash not in seen:
                seen.add(stem_hash)
                indices.append(stem_hash)

    return indices


# ---------------------------------------------------------------------------
# Content Fingerprint (Dedup)
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """Normalize for deterministic fingerprinting.

    NFC normalize, lowercase, collapse whitespace, trim.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).lower()).strip()


def generate_content_fingerprint(plaintext: str, dedup_key: bytes) -> str:
    """HMAC-SHA256 content fingerprint. Returns 64-char hex string."""
    normalized = _normalize_text(plaintext)
    return hmac_mod.new(
        dedup_key, normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# Embedding Encryption
# ---------------------------------------------------------------------------


def encrypt_embedding(embedding: list[float], encryption_key: bytes) -> str:
    """Encrypt embedding: pack as LE float32 array -> base64 -> AES encrypt."""
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
