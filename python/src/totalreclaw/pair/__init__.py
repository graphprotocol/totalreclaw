"""TotalReclaw Hermes QR-pair flow (2.3.1rc4).

Browser-side crypto handoff that keeps the recovery phrase out of the
LLM context. The gateway publishes an ephemeral x25519 public key via a
local-loopback HTTP endpoint; the user's browser does x25519 ECDH +
ChaCha20-Poly1305 AEAD encryption of the phrase against that key and
POSTs the ciphertext back. The gateway decrypts, writes
``~/.totalreclaw/credentials.json`` (0600), and the agent never sees the
plaintext at any point.

Parity with the TypeScript plugin's ``skill/plugin/pair-*.ts`` modules
(3.3.0). Cipher-suite wire format is identical:

- x25519 ECDH key agreement (32-byte raw keys).
- HKDF-SHA256 with ``salt = sid`` (UTF-8 bytes) and
  ``info = "totalreclaw-pair-v1"`` (UTF-8) -> 32-byte AEAD key.
- ChaCha20-Poly1305 AEAD, 12-byte nonce, 16-byte tag, ``AD = sid`` bytes.
- Base64url encoding for every wire field.

ML-KEM hybrid parity (per ``project_phrase_safety_rule.md``) is DEFERRED
to rc.5 — pure x25519 ships in rc.4 so Hermes gains *some* phrase-safe
setup path without blocking on the hybrid port. The TS plugin is
currently also pure x25519; adding hybrid in rc.5 will land in both
stacks together.
"""
from __future__ import annotations

from .crypto import (
    AEAD_KEY_BYTES,
    AEAD_NONCE_BYTES,
    AEAD_TAG_BYTES,
    HKDF_INFO,
    X25519_KEY_BYTES,
    GatewayKeypair,
    compute_shared_secret,
    decrypt_pairing_payload,
    derive_aead_key_from_ecdh,
    derive_session_keys,
    generate_gateway_keypair,
)
from .session_store import (
    DEFAULT_PAIR_TTL_MS,
    MAX_SECONDARY_CODE_ATTEMPTS,
    PairSession,
    PairSessionStatus,
    consume_pair_session,
    create_pair_session,
    default_pair_sessions_path,
    get_pair_session,
    register_failed_secondary_code,
    reject_pair_session,
    transition_pair_session,
)
from .http_server import PairHttpServer, build_pair_http_server
from .pair_page import render_pair_page

__all__ = [
    "AEAD_KEY_BYTES",
    "AEAD_NONCE_BYTES",
    "AEAD_TAG_BYTES",
    "HKDF_INFO",
    "X25519_KEY_BYTES",
    "DEFAULT_PAIR_TTL_MS",
    "MAX_SECONDARY_CODE_ATTEMPTS",
    "GatewayKeypair",
    "PairSession",
    "PairSessionStatus",
    "PairHttpServer",
    "build_pair_http_server",
    "compute_shared_secret",
    "consume_pair_session",
    "create_pair_session",
    "decrypt_pairing_payload",
    "default_pair_sessions_path",
    "derive_aead_key_from_ecdh",
    "derive_session_keys",
    "generate_gateway_keypair",
    "get_pair_session",
    "register_failed_secondary_code",
    "reject_pair_session",
    "render_pair_page",
    "transition_pair_session",
]
