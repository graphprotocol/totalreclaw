"""Parity tests for TotalReclaw Python crypto against TypeScript fixtures."""
import json
from pathlib import Path

import pytest

from totalreclaw.crypto import (
    derive_keys_from_mnemonic,
    compute_auth_key_hash,
    derive_lsh_seed,
    encrypt,
    decrypt,
    generate_blind_indices,
    generate_content_fingerprint,
    encrypt_embedding,
    decrypt_embedding,
)

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
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        emb = [0.1, 0.2, 0.3, -0.5, 1.0]
        decrypted = decrypt_embedding(
            encrypt_embedding(emb, keys.encryption_key), keys.encryption_key
        )
        for a, b in zip(emb, decrypted):
            assert abs(a - b) < 1e-6


class TestBlindIndices:
    def test_parity_with_typescript(self):
        indices = generate_blind_indices(FIXTURES["blind_indices"]["input_text"])
        assert indices == FIXTURES["blind_indices"]["expected"]

    def test_empty_string(self):
        assert generate_blind_indices("") == []

    def test_short_tokens_filtered(self):
        indices = generate_blind_indices("I a b cc dd")
        # "I" and "a" and "b" are < 2 chars, filtered. "cc" and "dd" remain.
        assert len(indices) >= 2

    def test_dedup(self):
        indices = generate_blind_indices("hello hello hello")
        # Same word repeated -- should dedup
        unique_hashes = set(indices)
        assert len(indices) == len(unique_hashes)


class TestContentFingerprint:
    def test_parity_with_typescript(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        fp = generate_content_fingerprint(
            FIXTURES["content_fingerprint"]["input_text"],
            keys.dedup_key,
        )
        assert fp == FIXTURES["content_fingerprint"]["expected_hex"]

    def test_whitespace_normalization(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        fp1 = generate_content_fingerprint(
            "  Hello   world  \n  test  ", keys.dedup_key
        )
        fp2 = generate_content_fingerprint("Hello world test", keys.dedup_key)
        assert fp1 == fp2

    def test_case_insensitive(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        fp1 = generate_content_fingerprint("Hello World", keys.dedup_key)
        fp2 = generate_content_fingerprint("hello world", keys.dedup_key)
        assert fp1 == fp2
