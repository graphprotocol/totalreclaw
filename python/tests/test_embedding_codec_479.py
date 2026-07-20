"""Universal embedding decode + prefer-core wiring (internal#479 Part B).

Locks the read-side parity fix: ``decrypt_embedding`` must read every format
ever written — canonical f16 (Hermes / core), legacy JSON array (TS plugin),
and legacy f32 binary (old Python / MCP) — so a mixed-client vault no longer
silently degrades foreign-format facts to word-index matching. Before this,
``decrypt_embedding`` was base64-only and choked on the TS plugin's JSON-array
payload (half of the parity bug).

Also exercises the prefer-core wiring: when the installed ``totalreclaw_core``
wheel exposes ``encode_embedding_canonical`` / ``decode_embedding_universal``,
``encrypt_embedding`` / ``decrypt_embedding`` delegate to them (monkeypatched
here, since the published 2.5.x wheel predates the codec).
"""
from __future__ import annotations

import base64
import json
import math
import struct

import pytest

import totalreclaw_core
from totalreclaw.crypto import (
    decrypt,
    decrypt_embedding,
    derive_keys_from_mnemonic,
    encrypt,
    encrypt_embedding,
)
from totalreclaw.embedding import EMBEDDING_DIMS


def _keys():
    return derive_keys_from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )


def _unit(seed: int = 1) -> list[float]:
    raw = [
        math.sin((seed + i) * 0.0137) * math.cos((seed - i) * 0.0079)
        for i in range(EMBEDDING_DIMS)
    ]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# decrypt_embedding reads all three formats (the actual parity fix).
# ---------------------------------------------------------------------------


class TestReadsAllThreeFormats:
    def test_reads_self_f16_write(self):
        """encrypt_embedding writes f16 (1280B); decrypt reads it back."""
        keys = _keys()
        vec = _unit(3)
        decoded = decrypt_embedding(encrypt_embedding(vec, keys.encryption_key), keys.encryption_key)
        assert len(decoded) == EMBEDDING_DIMS
        assert _cosine(decoded, vec) >= 0.9999

    def test_reads_json_array_payload_from_plugin(self):
        """NEW (#479): the TS plugin's JSON-array payload is now readable.

        Before this, decrypt_embedding was base64-only and choked on a JSON
        payload — that was half of the parity bug."""
        keys = _keys()
        vec = _unit(5)
        # Plugin write: encryptToHex(JSON.stringify(embedding)). On the python
        # side the ciphertext layer is base64, but the pre-encryption payload is
        # the JSON array string.
        blob = encrypt(json.dumps(vec), keys.encryption_key)
        decoded = decrypt_embedding(blob, keys.encryption_key)
        assert len(decoded) == EMBEDDING_DIMS
        # JSON is exact (no f16 rounding); the round-tripped floats equal vec.
        for a, b in zip(decoded, vec):
            assert a == pytest.approx(b)

    def test_reads_f32_binary_payload(self):
        """Old Python / MCP f32-binary payload (2560B) still decodes exactly."""
        keys = _keys()
        vec = _unit(9)
        legacy = base64.b64encode(struct.pack(f"<{EMBEDDING_DIMS}f", *vec)).decode("ascii")
        blob = encrypt(legacy, keys.encryption_key)
        decoded = decrypt_embedding(blob, keys.encryption_key)
        assert len(decoded) == EMBEDDING_DIMS
        for a, b in zip(decoded, vec):
            assert abs(a - b) < 1e-6


# ---------------------------------------------------------------------------
# Bad / ambiguous lengths must raise (never a silently wrong-dim vector).
# ---------------------------------------------------------------------------


class TestBadLength:
    def test_buffer_of_bad_length_raises(self):
        """3 bytes is neither 1280 (f16) nor %4 (f32) -> error."""
        keys = _keys()
        payload = base64.b64encode(b"\x00\x01\x02").decode("ascii")
        blob = encrypt(payload, keys.encryption_key)
        with pytest.raises(Exception):
            decrypt_embedding(blob, keys.encryption_key)


# ---------------------------------------------------------------------------
# Prefer-core wiring (exercised via monkeypatch).
# ---------------------------------------------------------------------------


class TestPreferCore:
    def test_decrypt_prefers_core_decode_when_present(self, monkeypatch):
        """When totalreclaw_core.decode_embedding_universal exists, decrypt_embedding
        routes the decrypted payload through it."""
        keys = _keys()
        seen: dict[str, str] = {}

        def fake_decode(payload: str):
            seen["payload"] = payload
            return [0.5, 0.25]

        monkeypatch.setattr(
            totalreclaw_core, "decode_embedding_universal", fake_decode, raising=False
        )
        blob = encrypt("PAYLOAD-MARKER", keys.encryption_key)
        out = decrypt_embedding(blob, keys.encryption_key)
        assert seen["payload"] == "PAYLOAD-MARKER"
        assert out == [0.5, 0.25]

    def test_encrypt_prefers_core_encode_when_present(self, monkeypatch):
        """When totalreclaw_core.encode_embedding_canonical exists, encrypt_embedding
        delegates to it (and does NOT pack the local f16 fallback)."""
        keys = _keys()
        seen: dict[str, list[float]] = {}

        def fake_encode(embedding):
            seen["vec"] = list(embedding)
            return "CORE-CANONICAL-PAYLOAD"

        monkeypatch.setattr(
            totalreclaw_core, "encode_embedding_canonical", fake_encode, raising=False
        )
        blob = encrypt_embedding([0.1, 0.2, 0.3], keys.encryption_key)
        # The encrypted blob must wrap exactly the core-produced payload.
        assert decrypt(blob, keys.encryption_key) == "CORE-CANONICAL-PAYLOAD"
        assert seen["vec"] == [0.1, 0.2, 0.3]

    def test_encrypt_core_fail_closed_propagates(self, monkeypatch):
        """Core's fail-closed validation (NaN/inf/overflow) must abort the store."""
        keys = _keys()

        def strict_encode(_embedding):
            raise ValueError("non-finite component (fail-closed)")

        monkeypatch.setattr(
            totalreclaw_core, "encode_embedding_canonical", strict_encode, raising=False
        )
        with pytest.raises(ValueError):
            encrypt_embedding([0.1, float("nan")], keys.encryption_key)


# ---------------------------------------------------------------------------
# Golden-vector byte parity with core's canonical f16 (Part A fixture).
# ---------------------------------------------------------------------------


class TestCoreGoldenVectorParity:
    """decrypt_embedding's local f16 path must reproduce core's golden unit
    vector (committed in rust/.../embedding_codec_vectors.json) at high cosine."""

    def test_canonical_f16_fixture_decodes(self):
        from pathlib import Path

        fixture = json.loads(
            Path(__file__).resolve().parents[2]
            .joinpath("rust", "totalreclaw-core", "tests", "fixtures", "embedding_codec_vectors.json")
            .read_text()
        )
        keys = _keys()
        # Wrap the canonical f16 base64 in the python ciphertext layer, the way a
        # Hermes/core-written fact would arrive.
        blob = encrypt(fixture["canonical_f16_base64"], keys.encryption_key)
        decoded = decrypt_embedding(blob, keys.encryption_key)
        assert len(decoded) == EMBEDDING_DIMS
        assert _cosine(decoded, fixture["unit_vector"]) >= 0.9999
