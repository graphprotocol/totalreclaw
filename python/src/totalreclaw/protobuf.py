"""Minimal protobuf wire format encoder for TotalReclaw fact payloads.

Hand-rolled encoder matching server/proto/totalreclaw.proto field layout.
No external protobuf library dependency -- uses only stdlib.

Field numbers match TotalReclawFact:
  1: id (string), 2: timestamp (string), 3: owner (string),
  4: encrypted_blob (bytes), 5: blind_indices (repeated string),
  6: decay_score (double), 7: is_active (bool), 8: version (int32),
  9: (removed in v3 -- now encrypted inside field 4),
  10: content_fp (string),
  11: (removed in v3 -- now encrypted inside field 4),
  12: sequence_id (int64 -- assigned by subgraph, not client),
  13: encrypted_embedding (string)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Wire format primitives
# ---------------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf base-128 varint."""
    result = bytearray()
    v = value & 0xFFFFFFFF  # treat as unsigned 32-bit
    while v > 0x7F:
        result.append((v & 0x7F) | 0x80)
        v >>= 7
    result.append(v & 0x7F)
    return bytes(result)


def _write_string(parts: list[bytes], field_number: int, value: str) -> None:
    """Write a string field (wire type 2 = length-delimited)."""
    if not value:
        return
    data = value.encode("utf-8")
    key = (field_number << 3) | 2
    parts.append(encode_varint(key))
    parts.append(encode_varint(len(data)))
    parts.append(data)


def _write_bytes(parts: list[bytes], field_number: int, value: bytes) -> None:
    """Write a bytes field (wire type 2 = length-delimited)."""
    key = (field_number << 3) | 2
    parts.append(encode_varint(key))
    parts.append(encode_varint(len(value)))
    parts.append(value)


def _write_double(parts: list[bytes], field_number: int, value: float) -> None:
    """Write a double field (wire type 1 = 64-bit fixed)."""
    key = (field_number << 3) | 1
    parts.append(encode_varint(key))
    parts.append(struct.pack("<d", value))


def _write_varint_field(parts: list[bytes], field_number: int, value: int) -> None:
    """Write a varint field (wire type 0)."""
    key = (field_number << 3) | 0
    parts.append(encode_varint(key))
    parts.append(encode_varint(value))


# ---------------------------------------------------------------------------
# Fact payload dataclass
# ---------------------------------------------------------------------------


@dataclass
class FactPayload:
    """Client-side fact payload ready for protobuf encoding.

    Mirrors the TypeScript ``FactPayload`` interface in
    ``mcp/src/subgraph/store.ts``.
    """

    id: str
    timestamp: str
    owner: str
    encrypted_blob: str  # Hex-encoded XChaCha20-Poly1305 ciphertext
    blind_indices: list[str] = field(default_factory=list)
    decay_score: float = 1.0
    source: str = ""
    content_fp: str = ""
    agent_id: str = ""
    encrypted_embedding: Optional[str] = None


# ---------------------------------------------------------------------------
# Encoding functions
# ---------------------------------------------------------------------------


def encode_fact_protobuf(fact: FactPayload) -> bytes:
    """Encode a :class:`FactPayload` as minimal protobuf wire format.

    The output is byte-compatible with the TypeScript ``encodeFactProtobuf``
    in ``mcp/src/subgraph/store.ts`` and can be decoded by any standard
    protobuf parser using ``server/proto/totalreclaw.proto``.
    """
    parts: list[bytes] = []

    _write_string(parts, 1, fact.id)
    _write_string(parts, 2, fact.timestamp)
    _write_string(parts, 3, fact.owner)
    _write_bytes(parts, 4, bytes.fromhex(fact.encrypted_blob))

    for index in fact.blind_indices:
        _write_string(parts, 5, index)

    _write_double(parts, 6, fact.decay_score)
    _write_varint_field(parts, 7, 1)  # is_active = true
    _write_varint_field(parts, 8, 3)  # version = 3 (source/agent_id now encrypted inside field 4)
    # Fields 9 (source) and 11 (agent_id) removed in v3 -- now encrypted inside field 4
    _write_string(parts, 10, fact.content_fp)
    # Field 12 (sequence_id) is assigned by the subgraph mapping, not the client
    if fact.encrypted_embedding:
        _write_string(parts, 13, fact.encrypted_embedding)

    return b"".join(parts)


def encode_tombstone_protobuf(fact_id: str, owner: str) -> bytes:
    """Encode a tombstone payload for soft-deleting a fact on-chain.

    Sets ``decay_score=0`` and ``encrypted_blob`` to the bytes of
    ``"tombstone"`` (hex-encoded), matching the managed-service delete
    convention used by the TypeScript client.
    """
    tombstone = FactPayload(
        id=fact_id,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        owner=owner,
        encrypted_blob=b"tombstone".hex(),
        blind_indices=[],
        decay_score=0.0,
        source="python_forget",
        content_fp="",
        agent_id="python-client",
    )
    return encode_fact_protobuf(tombstone)
