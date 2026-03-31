"""
TotalReclaw LSH Hasher — Locality-Sensitive Hashing.

Byte-for-byte compatible with mcp/src/subgraph/lsh.ts.

Random Hyperplane LSH for server-blind semantic search. Generates deterministic
hyperplane matrices from a seed derived from the user's master key, so the same
embedding always hashes to the same buckets across sessions.

Pipeline:
  1. Seed (32 bytes from HKDF) -> HKDF per table -> random bytes
  2. Random bytes -> Box-Muller transform -> Gaussian-distributed hyperplanes
  3. Embedding dot hyperplane -> sign bit -> N-bit signature per table
  4. Signature -> `lsh_t{table}_{signature}` -> SHA-256 -> blind hash
"""
from __future__ import annotations

import hashlib
import math
import struct

from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_N_TABLES = 20
_DEFAULT_N_BITS = 32
_BYTES_PER_FLOAT = 8  # 2 x uint32 = 8 bytes per Box-Muller sample
_MAX_HKDF_OUTPUT = 255 * 32  # SHA-256 hash length = 32 -> max 8160 bytes


# ---------------------------------------------------------------------------
# LSHHasher
# ---------------------------------------------------------------------------


class LSHHasher:
    """Random Hyperplane LSH hasher.

    All state is deterministic from the seed -- no randomness at hash time.
    Construct once per session; call ``hash()`` for every store/search operation.
    """

    def __init__(
        self,
        seed: bytes,
        dims: int,
        n_tables: int = _DEFAULT_N_TABLES,
        n_bits: int = _DEFAULT_N_BITS,
    ):
        if len(seed) < 16:
            raise ValueError(
                f"LSH seed too short: expected >= 16 bytes, got {len(seed)}"
            )
        if dims < 1:
            raise ValueError(f"dims must be positive, got {dims}")
        if n_tables < 1:
            raise ValueError(f"n_tables must be positive, got {n_tables}")
        if n_bits < 1:
            raise ValueError(f"n_bits must be positive, got {n_bits}")

        self._dims = dims
        self._n_tables = n_tables
        self._n_bits = n_bits

        # Generate hyperplane matrices deterministically from the seed.
        self._hyperplanes: list[list[float]] = []
        for t in range(n_tables):
            self._hyperplanes.append(self._generate_table_hyperplanes(seed, t))

    # -----------------------------------------------------------------------
    # Hyperplane generation (deterministic from seed)
    # -----------------------------------------------------------------------

    def _derive_random_bytes(
        self, seed: bytes, base_info: str, length: int
    ) -> bytes:
        """Derive ``length`` pseudo-random bytes via chunked HKDF-SHA256.

        A single HKDF-SHA256 call can output at most 255 * 32 = 8160 bytes.
        For large embedding dimensions we need more, so we iterate over
        sub-block indices as part of the info string.
        """
        result = bytearray()
        block_index = 0
        while len(result) < length:
            remaining = length - len(result)
            chunk_len = min(remaining, _MAX_HKDF_OUTPUT)
            info = f"{base_info}_block_{block_index}".encode("utf-8")
            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=chunk_len,
                salt=b"",
                info=info,
            )
            chunk = hkdf.derive(seed)
            result.extend(chunk)
            block_index += 1
        return bytes(result[:length])

    def _generate_table_hyperplanes(
        self, seed: bytes, table_index: int
    ) -> list[float]:
        """Generate the hyperplane matrix for a single table.

        Each table gets a unique HKDF-derived byte stream. We consume 8 bytes
        per Gaussian sample (Box-Muller uses two uniform uint32 values).

        Hyperplanes are NOT normalised to unit length -- normalisation is
        unnecessary because we only care about the sign of the dot product,
        which is scale-invariant.
        """
        total_floats = self._dims * self._n_bits
        total_bytes = total_floats * _BYTES_PER_FLOAT
        random_bytes = self._derive_random_bytes(
            seed, f"lsh_table_{table_index}", total_bytes
        )

        matrix: list[float] = [0.0] * total_floats
        for i in range(total_floats):
            offset = i * _BYTES_PER_FLOAT
            u1_raw = struct.unpack_from("<I", random_bytes, offset)[0]
            u2_raw = struct.unpack_from("<I", random_bytes, offset + 4)[0]

            # Map to (0, 1] -- avoid exactly 0 for the log in Box-Muller.
            u1 = (u1_raw + 1) / (0xFFFFFFFF + 2)
            u2 = (u2_raw + 1) / (0xFFFFFFFF + 2)

            # Box-Muller transform (we only need one of the two outputs).
            matrix[i] = math.sqrt(-2 * math.log(u1)) * math.cos(
                2 * math.pi * u2
            )
        return matrix

    # -----------------------------------------------------------------------
    # Hash function
    # -----------------------------------------------------------------------

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
        if len(embedding) != self._dims:
            raise ValueError(
                f"Embedding dimension mismatch: expected {self._dims}, got {len(embedding)}"
            )

        results: list[str] = []
        for t in range(self._n_tables):
            matrix = self._hyperplanes[t]
            bits: list[str] = []
            for b in range(self._n_bits):
                base_offset = b * self._dims
                dot = 0.0
                for d in range(self._dims):
                    dot += matrix[base_offset + d] * embedding[d]
                bits.append("1" if dot >= 0 else "0")
            signature = "".join(bits)
            bucket_id = f"lsh_t{t}_{signature}"
            h = hashlib.sha256(bucket_id.encode("utf-8")).hexdigest()
            results.append(h)
        return results

    # -----------------------------------------------------------------------
    # Accessors
    # -----------------------------------------------------------------------

    @property
    def tables(self) -> int:
        """Number of hash tables."""
        return self._n_tables

    @property
    def bits(self) -> int:
        """Number of bits per table."""
        return self._n_bits

    @property
    def dimensions(self) -> int:
        """Embedding dimensionality."""
        return self._dims
