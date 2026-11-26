"""Parity tests for TotalReclaw Python crypto against TypeScript fixtures."""
import json
import math
from pathlib import Path

import pytest

from totalreclaw.crypto import (
    derive_keys_from_mnemonic,
    compute_auth_key_hash,
    derive_lsh_seed,
    encrypt,
    decrypt,
    encrypt_embedding,
    decrypt_embedding,
)
from totalreclaw.embedding import EMBEDDING_DIMS


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "crypto_vectors.json").read_text()
)


class TestKeyDerivation:
    def test_salt_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert keys.salt.hex() == FIXTURES["derived"]["salt_hex"]

    def test_auth_key_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert keys.auth_key.hex() == FIXTURES["derived"]["auth_key_hex"]

    def test_encryption_key_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert keys.encryption_key.hex() == FIXTURES["derived"]["encryption_key_hex"]

    def test_dedup_key_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert keys.dedup_key.hex() == FIXTURES["derived"]["dedup_key_hex"]

    def test_auth_key_hash_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert (
            compute_auth_key_hash(keys.auth_key)
            == FIXTURES["derived"]["auth_key_hash_hex"]
        )

    def test_lsh_seed_parity(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        lsh_seed = derive_lsh_seed(FIXTURES["mnemonic"], keys.salt)
        assert lsh_seed.hex() == FIXTURES["derived"]["lsh_seed_hex"]


class TestEncryption:
    def test_decrypt_typescript_ciphertext(self):
        """Python must decrypt ciphertext produced by TypeScript."""
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        encrypted = FIXTURES["encryption"]["encrypted_base64"]
        expected = FIXTURES["encryption"]["plaintext"]
        assert decrypt(encrypted, keys.encryption_key) == expected

    def test_encrypt_decrypt_roundtrip(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        for text in ["hello world", "", "caf\u00e9 \U0001f600", "A" * 10000]:
            assert (
                decrypt(encrypt(text, keys.encryption_key), keys.encryption_key)
                == text
            )

    def test_invalid_key_length(self):
        with pytest.raises(ValueError, match="expected 32 bytes"):
            encrypt("test", b"short")
        with pytest.raises(ValueError, match="expected 32 bytes"):
            decrypt("dGVzdA==", b"short")

    def test_embedding_roundtrip(self):
        """f16 packing is lossy — assert fidelity, not exact f32 equality.

        Uses a realistic 640-dim unit-normalized vector (the production
        embedding shape). encrypt_embedding writes half-precision; the
        round-trip must preserve cosine >= 0.9999 and each element within
        2e-3.
        """
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        # Deterministic 640-dim unit-normalized vector (no pytest-randomly
        # dependence; values in ~[-1, 1] like a real Harrier embedding).
        raw = [math.sin(i * 0.0137) * math.cos(i * 0.0079) for i in range(EMBEDDING_DIMS)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        emb = [x / norm for x in raw]
        decrypted = decrypt_embedding(
            encrypt_embedding(emb, keys.encryption_key), keys.encryption_key
        )
        assert len(decrypted) == EMBEDDING_DIMS
        assert _cosine(emb, decrypted) >= 0.9999
        for a, b in zip(emb, decrypted):
            assert abs(a - b) <= 2e-3


class TestKeyedTags:
    """The client-side unkeyed ``generate_blind_indices`` / fp v1 wrappers are
    gone (internal#573); keyed parity lives in ``test_keyed_tags_573.py``."""

    def test_empty_string(self):
        from totalreclaw import blind_tags

        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert blind_tags.fact_tags(keys, "") == []

    def test_dedup_and_sorted(self):
        from totalreclaw import blind_tags

        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        tags = blind_tags.fact_tags(keys, "hello hello hello")
        assert tags == sorted(set(tags))

    def test_wrappers_removed(self):
        import totalreclaw.crypto as c

        assert not hasattr(c, "generate_blind_indices")
        assert not hasattr(c, "generate_content_fingerprint")


class TestContentFingerprintV2:
    def test_whitespace_normalization(self):
        from totalreclaw import blind_tags

        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        fp1 = blind_tags.content_fp(keys, "  Hello   world  \n  test  ")
        fp2 = blind_tags.content_fp(keys, "Hello world test")
        assert fp1 == fp2

    def test_case_insensitive(self):
        from totalreclaw import blind_tags

        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        assert blind_tags.content_fp(keys, "Hello World") == blind_tags.content_fp(keys, "hello world")
