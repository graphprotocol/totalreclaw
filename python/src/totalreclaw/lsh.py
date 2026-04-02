"""
TotalReclaw LSH Hasher — Locality-Sensitive Hashing.

Delegates to the totalreclaw_core Rust/PyO3 module for all LSH operations.
Maintains the same Python API for backward compatibility.

Random Hyperplane LSH for server-blind semantic search. Generates deterministic
hyperplane matrices from a seed derived from the user's master key, so the same
embedding always hashes to the same buckets across sessions.
"""
from __future__ import annotations

import totalreclaw_core


class LSHHasher:
    """Random Hyperplane LSH hasher.

    All state is deterministic from the seed -- no randomness at hash time.
    Construct once per session; call ``hash()`` for every store/search operation.
    """

    def __init__(
        self,
        seed: bytes,
        dims: int,
        n_tables: int = 20,
        n_bits: int = 32,
    ):
        self._inner = totalreclaw_core.LshHasher(seed, dims, n_tables, n_bits)

    def hash(self, embedding: list[float]) -> list[str]:
        """Hash an embedding vector to an array of blind-hashed bucket IDs.

        For each table:
          1. Compute the N-bit signature (sign of dot product with each hyperplane).
          2. Build the bucket string: ``lsh_t{tableIndex}_{binarySignature}``.
          3. SHA-256 the bucket string to produce a blind hash (hex).

        Args:
            embedding: The embedding vector (must have ``dims`` elements).

        Returns:
            List of ``n_tables`` hex strings (one blind hash per table).
        """
        return self._inner.hash(embedding)

    # -----------------------------------------------------------------------
    # Accessors
    # -----------------------------------------------------------------------

    @property
    def tables(self) -> int:
        """Number of hash tables."""
        return self._inner.tables

    @property
    def bits(self) -> int:
        """Number of bits per table."""
        return self._inner.bits

    @property
    def dimensions(self) -> int:
        """Embedding dimensionality."""
        return self._inner.dimensions
