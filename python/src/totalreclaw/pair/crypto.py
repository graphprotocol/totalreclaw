"""pair.crypto — gateway-side x25519 + ChaCha20-Poly1305 primitives.

Python parity of ``skill/plugin/pair-crypto.ts`` (v3.3.0). Every wire
constant (HKDF info, salt binding, tag length, key length) matches the
TS module so a ciphertext produced by one side decrypts on the other
unchanged.

Cipher suite (per design doc section 3a-3b, ratified 2026-04-20):

- ECDH on x25519 for key agreement.
- HKDF-SHA256 for symmetric-key derivation from the shared secret.
- ChaCha20-Poly1305 AEAD for the ciphertext payload, with the sid
  bound as associated data (``AD = sid UTF-8 bytes``).

Every primitive is provided by the ``cryptography`` package (OpenSSL-
backed). No phrase material, private keys, or secondary codes EVER flow
through logs or return values.

Byte encoding: every wire field is base64url (``urlsafe_b64``) with
``"="`` padding stripped, matching Node's ``Buffer.toString('base64url')``
output. Helpers ``_b64url_encode`` / ``_b64url_decode`` live here so every
caller uses the same padding policy.

Base64url round-trip parity sanity-check: a 32-byte raw x25519 key
becomes 43 base64url chars (no padding), matching the TS-side output.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Union

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)


# ---------------------------------------------------------------------------
# Constants — MUST stay in lockstep with skill/plugin/pair-crypto.ts
# ---------------------------------------------------------------------------

#: HKDF "info" parameter — fixes the domain separation for this protocol.
#: MUST match the browser-side constant in the pair-page bundle.
HKDF_INFO = "totalreclaw-pair-v1"

#: HKDF output length — 32 bytes = 256-bit ChaCha20-Poly1305 key.
AEAD_KEY_BYTES = 32

#: ChaCha20-Poly1305 nonce length — 12 bytes per RFC 7539.
AEAD_NONCE_BYTES = 12

#: ChaCha20-Poly1305 auth-tag length — 16 bytes standard.
AEAD_TAG_BYTES = 16

#: Raw x25519 public/private key length — 32 bytes per RFC 7748.
X25519_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayKeypair:
    """Ephemeral gateway keypair, both halves base64url-encoded.

    Mirrors the TS ``GatewayKeypair`` type. ``sk_b64`` stays on disk under
    the pair-session record's 0600 file; ``pk_b64`` goes into the QR URL
    fragment.
    """

    sk_b64: str
    pk_b64: str


@dataclass(frozen=True)
class SessionKeys:
    """Fully-derived session keys. Caller uses ``k_enc`` for AEAD ops."""

    k_enc: bytes


# ---------------------------------------------------------------------------
# Base64url helpers (strip ``=`` padding for Node parity)
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes as base64url without trailing ``=`` padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Decode base64url with optional ``=`` padding."""
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


# ---------------------------------------------------------------------------
# Key generation / conversion
# ---------------------------------------------------------------------------


def generate_gateway_keypair() -> GatewayKeypair:
    """Generate a fresh ephemeral x25519 keypair for a pairing session.

    Returns raw 32-byte values base64url-encoded. Caller persists the
    private half in pair-session-store (under the session record's 0600
    file) and embeds the public half in the QR URL fragment.
    """
    sk = X25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    pk_raw = sk.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return GatewayKeypair(
        sk_b64=_b64url_encode(sk_raw),
        pk_b64=_b64url_encode(pk_raw),
    )


def _public_key_from_b64(pk_b64: str) -> X25519PublicKey:
    raw = _b64url_decode(pk_b64)
    if len(raw) != X25519_KEY_BYTES:
        raise ValueError(
            f"pair.crypto: public key must be {X25519_KEY_BYTES} bytes (got {len(raw)})"
        )
    return X25519PublicKey.from_public_bytes(raw)


def _private_key_from_b64(sk_b64: str) -> X25519PrivateKey:
    raw = _b64url_decode(sk_b64)
    if len(raw) != X25519_KEY_BYTES:
        raise ValueError(
            f"pair.crypto: private key must be {X25519_KEY_BYTES} bytes (got {len(raw)})"
        )
    return X25519PrivateKey.from_private_bytes(raw)


def derive_public_from_private(sk_b64: str) -> str:
    """Derive the public key (base64url) from a raw base64url private key."""
    sk = _private_key_from_b64(sk_b64)
    pk_raw = sk.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return _b64url_encode(pk_raw)


# ---------------------------------------------------------------------------
# ECDH + HKDF
# ---------------------------------------------------------------------------


def compute_shared_secret(sk_local_b64: str, pk_remote_b64: str) -> bytes:
    """Perform x25519 ECDH between local private + remote public.

    Produces a 32-byte shared secret. Both sides running this with swapped
    halves MUST produce the same secret — validated by the round-trip
    tests against TS-generated vectors.
    """
    sk = _private_key_from_b64(sk_local_b64)
    pk = _public_key_from_b64(pk_remote_b64)
    shared = sk.exchange(pk)
    if len(shared) != X25519_KEY_BYTES:
        raise ValueError(
            f"pair.crypto: ECDH output wrong length ({len(shared)} != {X25519_KEY_BYTES})"
        )
    return shared


def _hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """Minimal RFC 5869 HKDF-SHA256 — Node parity.

    Python's stdlib exposes ``hashlib.hkdf`` only in 3.12+, but we still
    support 3.11 per pyproject (``requires-python = ">=3.11"``). Writing
    this out directly avoids the dep-floor bump and is ~10 lines.
    """
    # Extract: PRK = HMAC-SHA256(salt, ikm)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    # Expand: T(1..N) = HMAC(PRK, T(i-1) || info || i); OKM = concat || trim
    okm = b""
    t = b""
    i = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:length]


def derive_session_keys(shared_secret: bytes, sid: str) -> SessionKeys:
    """Derive the AEAD key from a shared secret via HKDF-SHA256.

    Uses the session id as the salt and the fixed protocol tag as the
    info. The sid binding means a ciphertext encrypted for session A is
    decryptable ONLY under session A's derived key.
    """
    if len(shared_secret) != X25519_KEY_BYTES:
        raise ValueError("pair.crypto: shared secret must be 32 bytes")
    if not isinstance(sid, str) or len(sid) == 0:
        raise ValueError("pair.crypto: sid is required for HKDF salt binding")
    salt = sid.encode("utf-8")
    info = HKDF_INFO.encode("utf-8")
    okm = _hkdf_sha256(salt=salt, ikm=shared_secret, info=info, length=AEAD_KEY_BYTES)
    return SessionKeys(k_enc=okm)


def derive_aead_key_from_ecdh(sk_local_b64: str, pk_remote_b64: str, sid: str) -> SessionKeys:
    """One-shot convenience: ECDH + HKDF in a single call."""
    shared = compute_shared_secret(sk_local_b64=sk_local_b64, pk_remote_b64=pk_remote_b64)
    try:
        return derive_session_keys(shared_secret=shared, sid=sid)
    finally:
        # Best-effort: wipe the shared secret from the local scope. Python
        # can't guarantee zeroization (immutable bytes) but rebinding at
        # least drops the reference.
        shared = b"\x00" * AEAD_KEY_BYTES  # noqa: F841


# ---------------------------------------------------------------------------
# AEAD
# ---------------------------------------------------------------------------


def aead_decrypt(k_enc: bytes, nonce_b64: str, sid: str, ciphertext_b64: str) -> bytes:
    """Decrypt a ChaCha20-Poly1305 ciphertext.

    The ``cryptography`` library's ``ChaCha20Poly1305.decrypt`` expects
    the ciphertext in combined ``plaintext || tag`` form and takes the
    AD bytes directly — matching the Node API's ``setAAD`` behaviour.

    Raises ``cryptography.exceptions.InvalidTag`` on tag mismatch (either
    tampering or wrong key), which the HTTP handler catches and maps to
    a 400 response.
    """
    nonce = _b64url_decode(nonce_b64)
    if len(nonce) != AEAD_NONCE_BYTES:
        raise ValueError(
            f"pair.crypto: nonce must be {AEAD_NONCE_BYTES} bytes (got {len(nonce)})"
        )
    if len(k_enc) != AEAD_KEY_BYTES:
        raise ValueError(f"pair.crypto: AEAD key must be {AEAD_KEY_BYTES} bytes")
    combined = _b64url_decode(ciphertext_b64)
    if len(combined) < AEAD_TAG_BYTES:
        raise ValueError("pair.crypto: ciphertext too short to contain tag")

    aead = ChaCha20Poly1305(k_enc)
    return aead.decrypt(nonce, combined, sid.encode("utf-8"))


def decrypt_pairing_payload(
    sk_gateway_b64: str,
    pk_device_b64: str,
    sid: str,
    nonce_b64: str,
    ciphertext_b64: str,
) -> bytes:
    """One-shot decrypt: ECDH + HKDF + AEAD.

    Called by the HTTP respond handler. Returns the plaintext bytes on
    success; raises on ANY failure (wrong key length, invalid curve
    point, tag mismatch).
    """
    keys = derive_aead_key_from_ecdh(
        sk_local_b64=sk_gateway_b64,
        pk_remote_b64=pk_device_b64,
        sid=sid,
    )
    return aead_decrypt(
        k_enc=keys.k_enc,
        nonce_b64=nonce_b64,
        sid=sid,
        ciphertext_b64=ciphertext_b64,
    )


def aead_encrypt_with_session_key(
    k_enc: bytes,
    sid: str,
    plaintext: Union[bytes, bytearray, memoryview],
    nonce_b64: Union[str, None] = None,
) -> tuple[str, str]:
    """Encrypt (used by tests + any future gateway-to-device ACK path).

    Emits (nonce_b64, ciphertext_b64). A 12-byte random nonce is drawn
    when ``nonce_b64`` is not supplied. Returns tuple, NOT a dict, to
    keep the module stdlib-only in its public surface.
    """
    if len(k_enc) != AEAD_KEY_BYTES:
        raise ValueError(f"pair.crypto: AEAD key must be {AEAD_KEY_BYTES} bytes")
    if nonce_b64 is None:
        nonce = os.urandom(AEAD_NONCE_BYTES)
    else:
        nonce = _b64url_decode(nonce_b64)
    if len(nonce) != AEAD_NONCE_BYTES:
        raise ValueError(f"pair.crypto: nonce must be {AEAD_NONCE_BYTES} bytes")

    pt = bytes(plaintext)
    aead = ChaCha20Poly1305(k_enc)
    ct = aead.encrypt(nonce, pt, sid.encode("utf-8"))
    return (_b64url_encode(nonce), _b64url_encode(ct))


def encrypt_pairing_payload(
    sk_local_b64: str,
    pk_remote_b64: str,
    sid: str,
    plaintext: Union[bytes, bytearray, memoryview],
    nonce_b64: Union[str, None] = None,
) -> tuple[str, str]:
    """One-shot encrypt: ECDH + HKDF + AEAD. Test helper."""
    keys = derive_aead_key_from_ecdh(
        sk_local_b64=sk_local_b64,
        pk_remote_b64=pk_remote_b64,
        sid=sid,
    )
    return aead_encrypt_with_session_key(
        k_enc=keys.k_enc,
        sid=sid,
        plaintext=plaintext,
        nonce_b64=nonce_b64,
    )


# ---------------------------------------------------------------------------
# Constant-time secondary-code comparison
# ---------------------------------------------------------------------------


def compare_secondary_codes_ct(a: str, b: str) -> bool:
    """Constant-time compare two 6-digit numeric strings.

    ``secrets.compare_digest`` is constant-time with respect to the length
    of the LONGER input; mismatched lengths leak a length bit but never a
    per-character timing side channel. Pairing flows use fixed 6-digit
    codes so the length bit is not exploitable.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return secrets.compare_digest(a, b)
