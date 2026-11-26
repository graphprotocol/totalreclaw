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
    Construct once per session; call ``hash_keyed()`` for every store/search operation.
    """

    def __init__(
        self,
        seed: bytes,
        dims: int,
        n_tables: int = 20,
        n_bits: int = 32,
    ):
        self._inner = totalreclaw_core.LshHasher(seed, dims, n_tables, n_bits)

    def hash_keyed(self, embedding: list[float]) -> list[str]:
        """Keyed LSH bucket tags, one per table, in table order (not sorted).

        Keyed by the vault's LSH bucket key inside core; no unkeyed variant
        exists on this class.

        Args:
            embedding: The embedding vector (must have ``dims`` elements).
        """
        return self._inner.hash_keyed(embedding)

    @property
    def core(self):
        """The underlying core hasher (for core keyed-trapdoor generation)."""
        return self._inner

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
