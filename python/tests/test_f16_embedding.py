"""f16 (half-precision) embedding quantization — write + length-inferred read.

encrypt_embedding now packs the 640-dim embedding as IEEE-754 half-precision
(struct 'e'), cutting the embedding portion of the stored blob from 2560B
(f32) to 1280B (f16). decrypt_embedding infers dtype from the decrypted byte
length, keyed on the fixed production dim:

  - len(buf) == EMBEDDING_DIMS * 2  -> f16 unpack (new writes, 640-dim)
  - otherwise                       -> f32 unpack (legacy blobs + any
                                       non-640-dim vector, e.g. the 1024-dim
                                       test fixture)

This back-compat scheme guarantees every pre-existing f32 embedding on-chain
still decodes, while new writes are f16. A byte marker/header is deliberately
NOT added (it would break legacy f32 decode).
"""
import base64
import math
import struct

import pytest

from totalreclaw.crypto import (
    decrypt,
    decrypt_embedding,
    derive_keys_from_mnemonic,
    encrypt,
    encrypt_embedding,
)
from totalreclaw.embedding import EMBEDDING_DIMS

F32_BYTES = EMBEDDING_DIMS * 4  # 2560
F16_BYTES = EMBEDDING_DIMS * 2  # 1280


def _keys():
    return derive_keys_from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )


def _unit_vector(seed: int) -> list[float]:
    raw = [math.sin((seed + i) * 0.0137) * math.cos((seed - i) * 0.0079) for i in range(EMBEDDING_DIMS)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class TestF16WriteShape:
    def test_640_dim_writes_1280_bytes(self):
        """A 640-dim embedding must pack to exactly EMBEDDING_DIMS*2 bytes (f16)."""
        keys = _keys()
        emb = _unit_vector(1)
        encrypted = encrypt_embedding(emb, keys.encryption_key)

        # Peel the XChaCha20 layer to inspect the raw packed buffer length.
        buf = base64.b64decode(decrypt(encrypted, keys.encryption_key))
        assert len(buf) == F16_BYTES


class TestBackcompatLegacyF32:
    def test_legacy_f32_blob_decodes_via_new_decrypt(self):
        """An embedding encrypted with the OLD f32 packing still decodes.

        Manually pack <640f -> base64 -> encrypt (the pre-change write path),
        then decode through the new length-inferred decrypt_embedding. Length
        2560 must route to the f32 branch and reproduce the original values.
        """
        keys = _keys()
        original = _unit_vector(7)
        legacy_buf = struct.pack(f"<{EMBEDDING_DIMS}f", *original)
        assert len(legacy_buf) == F32_BYTES  # sanity: this is the f32 shape
        legacy_blob = encrypt(base64.b64encode(legacy_buf).decode("ascii"), keys.encryption_key)

        decoded = decrypt_embedding(legacy_blob, keys.encryption_key)
        assert len(decoded) == EMBEDDING_DIMS
        # f32 is exact; legacy blobs must round-trip bit-for-bit.
        for a, b in zip(original, decoded):
            assert abs(a - b) < 1e-6

    def test_legacy_f32_blob_is_not_misread_as_f16(self):
        """2560 bytes is neither EMBEDDING_DIMS*2 (1280) nor ambiguous, so it
        must hit the f32 branch. Guard: a value like 0.5 is representable in
        both f16 and f32, so decoding would 'look plausible' if mis-routed —
        verify by a value (1e-3-ish magnitude differences) and length."""
        keys = _keys()
        original = [0.5 if i % 2 == 0 else -0.25 for i in range(EMBEDDING_DIMS)]
        legacy_buf = struct.pack(f"<{EMBEDDING_DIMS}f", *original)
        legacy_blob = encrypt(base64.b64encode(legacy_buf).decode("ascii"), keys.encryption_key)
        decoded = decrypt_embedding(legacy_blob, keys.encryption_key)
        assert decoded == pytest.approx(original, abs=1e-6)


class TestFidelity:
    @pytest.mark.parametrize("seed", range(60))
    def test_p99_cosine_fidelity(self, seed):
        """Over >=50 realistic 640-dim unit vectors, p99 cosine(original,
        roundtrip) >= 0.9999 (matches the PoC finding)."""
        keys = _keys()
        emb = _unit_vector(seed)
        decoded = decrypt_embedding(
            encrypt_embedding(emb, keys.encryption_key), keys.encryption_key
        )
        assert _cosine(emb, decoded) >= 0.9999


class TestNonProductionDims:
    def test_1024_dim_f32_still_decodes_as_f32(self):
        """A non-640-dim (e.g. 1024-dim test fixture) f32-packed vector must
        NOT be mis-read as f16. 1024 f32 = 4096B, which is != EMBEDDING_DIMS*2,
        so it hits the else (f32) branch."""
        keys = _keys()
        dim = 1024
        original = [math.sin(i * 0.01) * 0.5 for i in range(dim)]
        # Pack as f32 directly (bypass encrypt_embedding, which would now emit
        # f16 for 640 only — for 1024 it would also emit f16, but we want to
        # prove the READ side tolerates a legacy-style f32 1024-dim blob).
        legacy_buf = struct.pack(f"<{dim}f", *original)
        assert len(legacy_buf) == 4096
        blob = encrypt(base64.b64encode(legacy_buf).decode("ascii"), keys.encryption_key)

        decoded = decrypt_embedding(blob, keys.encryption_key)
        assert len(decoded) == dim
        for a, b in zip(original, decoded):
            assert abs(a - b) < 1e-6
