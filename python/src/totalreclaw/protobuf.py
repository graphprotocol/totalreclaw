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
  13: encrypted_embedding (string),
  14: index_scheme (varint, 1 = keyed scheme 1; written last)
"""

from __future__ import annotations

import re
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


#: Default outer protobuf schema version (v3, for legacy callers).
DEFAULT_PROTOBUF_VERSION: int = 3

#: Memory Taxonomy v1 outer protobuf schema version. Signals that the
#: inner encrypted blob is a v1 JSON payload with ``schema_version == "1.0"``.
PROTOBUF_VERSION_V4: int = 4


@dataclass
class FactPayload:
    """Client-side fact payload ready for protobuf encoding.

    Mirrors the TypeScript ``FactPayload`` interface in
    ``mcp/src/subgraph/store.ts``.

    As of ``totalreclaw`` 2.0.0, ``version`` carries the outer protobuf
    schema version — 3 for legacy writes, 4 for v1 taxonomy writes.
    A value of 0 (unset) is treated as ``DEFAULT_PROTOBUF_VERSION`` (3)
    so the existing v3 call sites keep working.
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
    version: int = 0  # 0 → default (v3) for back-compat


# ---------------------------------------------------------------------------
# Encoding functions (index scheme 1, internal#573)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_BLOB_RE = re.compile(r"^(?:[0-9a-fA-F]{2})*$")

#: Index scheme written to outer field 14 (1 = keyed scheme 1).
INDEX_SCHEME_KEYED: int = 1


def _validated_tags(tags: list[str]) -> list[str]:
    """Return the sorted, deduplicated tag list; reject any non 64-hex tag.

    The error message never echoes the offending tag.
    """
    for t in tags:
        if not isinstance(t, str) or not _TAG_RE.match(t):
            raise ValueError("blind index tag is not 64 lowercase hex characters")
    return sorted(set(tags))


def _blob_bytes(hex_blob: str) -> bytes:
    """Decode the encrypted blob hex; reject invalid hex without echoing it."""
    if not isinstance(hex_blob, str) or not _HEX_BLOB_RE.match(hex_blob):
        raise ValueError("encrypted blob is not valid hex")
    return bytes.fromhex(hex_blob)


def encode_fact_protobuf_keyed(fact: FactPayload) -> bytes:
    """Encode a :class:`FactPayload` as keyed-scheme-1 protobuf wire format.

    Byte-identical to core ``encode_fact_protobuf_keyed``: field 5 sorted
    ascending and deduplicated, version forced to 4, field 14 = 1 written
    last. Raises ``ValueError`` on a tag that is not 64 lowercase hex or on
    an invalid blob hex.
    """
    tags = _validated_tags(fact.blind_indices)
    blob = _blob_bytes(fact.encrypted_blob)
    parts: list[bytes] = []

    _write_string(parts, 1, fact.id)
    _write_string(parts, 2, fact.timestamp)
    _write_string(parts, 3, fact.owner)
    _write_bytes(parts, 4, blob)

    for index in tags:
        _write_string(parts, 5, index)

    _write_double(parts, 6, fact.decay_score)
    _write_varint_field(parts, 7, 1)  # is_active = true
    _write_varint_field(parts, 8, PROTOBUF_VERSION_V4)
    # Fields 9 (source) and 11 (agent_id) removed in v3 -- now encrypted inside field 4
    _write_string(parts, 10, fact.content_fp)
    # Field 12 (sequence_id) is assigned by the subgraph mapping, not the client
    if fact.encrypted_embedding:
        _write_string(parts, 13, fact.encrypted_embedding)
    _write_varint_field(parts, 14, INDEX_SCHEME_KEYED)

    return b"".join(parts)


def encode_tombstone_protobuf_keyed(
    fact_id: str, owner: str, timestamp: Optional[str] = None
) -> bytes:
    """Encode a keyed-scheme-1 tombstone (soft delete) payload.

    Layout: empty blob, ``decay_score = 0``, ``is_active = 0``, version 4,
    field 14 = 1; no fields 5, 10 or 13. ``timestamp`` defaults to now.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    parts: list[bytes] = []
    _write_string(parts, 1, fact_id)
    _write_string(parts, 2, timestamp)
    _write_string(parts, 3, owner)
    _write_bytes(parts, 4, b"")
    _write_double(parts, 6, 0.0)
    _write_varint_field(parts, 7, 0)  # is_active = false
    _write_varint_field(parts, 8, PROTOBUF_VERSION_V4)
    _write_varint_field(parts, 14, INDEX_SCHEME_KEYED)
    return b"".join(parts)
