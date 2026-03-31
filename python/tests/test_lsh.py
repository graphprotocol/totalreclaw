"""Parity tests for TotalReclaw LSH hasher."""
import json
import re
from pathlib import Path

import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic, derive_lsh_seed
from totalreclaw.lsh import LSHHasher

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "crypto_vectors.json").read_text()
)


class TestLSHParity:
    @pytest.fixture
    def hasher(self):
        keys = derive_keys_from_mnemonic(FIXTURES["mnemonic"])
        lsh_seed = derive_lsh_seed(FIXTURES["mnemonic"], keys.salt)
        return LSHHasher(
            lsh_seed,
            FIXTURES["lsh"]["embedding_dims"],
            FIXTURES["lsh"]["n_tables"],
            FIXTURES["lsh"]["n_bits"],
        )

    def test_bucket_hashes_parity(self, hasher):
        """LSH bucket hashes must match TypeScript byte-for-byte."""
        embedding = FIXTURES["lsh"]["embedding"]
        expected_buckets = FIXTURES["lsh"]["expected_buckets"]
        actual_buckets = hasher.hash(embedding)
        assert actual_buckets == expected_buckets, (
            f"First mismatch at index "
            f"{next(i for i, (a, e) in enumerate(zip(actual_buckets, expected_buckets)) if a != e)}"
        )

    def test_correct_count(self, hasher):
        """Number of buckets must equal n_tables."""
        embedding = FIXTURES["lsh"]["embedding"]
        buckets = hasher.hash(embedding)
        assert len(buckets) == FIXTURES["lsh"]["n_tables"]

    def test_valid_hex_format(self, hasher):
        """Each bucket hash must be a 64-char lowercase hex string."""
        embedding = FIXTURES["lsh"]["embedding"]
        buckets = hasher.hash(embedding)
        for b in buckets:
            assert re.match(r"^[0-9a-f]{64}$", b)

    def test_deterministic(self, hasher):
        """Same embedding must always produce same hashes."""
        embedding = FIXTURES["lsh"]["embedding"]
        assert hasher.hash(embedding) == hasher.hash(embedding)

    def test_dimension_mismatch_raises(self, hasher):
        """Wrong embedding dimension must raise ValueError."""
        with pytest.raises(ValueError, match="dimension mismatch"):
            hasher.hash([0.1, 0.2, 0.3])

    def test_seed_too_short_raises(self):
        """Seed shorter than 16 bytes must raise ValueError."""
        with pytest.raises(ValueError, match="too short"):
            LSHHasher(b"short", 1024)

    def test_properties(self, hasher):
        """Accessor properties must reflect constructor args."""
        assert hasher.tables == 20
        assert hasher.bits == 32
        assert hasher.dimensions == 1024
