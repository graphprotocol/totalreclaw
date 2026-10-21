"""Generate the shared outer-protobuf parity fixtures (PRD-04 F8 / DEP-6).

Writes two files next to this script:

* ``outer-protobuf-v1.json`` -- the canonical vectors. Backs:
    - rust/totalreclaw-core/tests/outer_protobuf_parity.rs  (Rust leg)
    - tests/parity/test_outer_protobuf_parity.py            (Python leg)
    - tests/parity/outer-protobuf-parity.test.ts            (TS leg: MCP + WASM)
    - mcp/tests/protobuf-outer-fields.test.ts               (MCP unit guard)
  Every leg asserts byte-identity with ``expected_protobuf_hex`` /
  ``expected_execute_calldata_hex`` / ``batch.expected_calldata_hex`` and,
  after ABI-decoding the calldata, that the outer protobuf carries neither
  field 9 (``source``) nor field 11 (``agent_id``).
* ``legacy/outer-protobuf-mcp-pre-dep6.json`` -- what the MCP server wrote
  for the same inputs BEFORE DEP-6 (plaintext fields 9 and 11). Kept as a
  negative control so the "field absent" assertions are provably not vacuous.

Rationale (PRD-04 section 6: a fixture that encodes the defect is regenerated
with a recorded rationale and the old vector kept under ``legacy/``): core
(``rust/totalreclaw-core/src/protobuf.rs``) and the Python client
(``python/src/totalreclaw/protobuf.py``) dropped fields 9/11 from the wire in
v3, but ``mcp/src/subgraph/store.ts`` kept writing ``source``
("mcp_remember", "mcp_forget", ...) and ``agent_id`` ("mcp-server") as
plaintext calldata (PRD-04 F8). No pre-existing fixture encoded the MCP
defect, so nothing was overwritten; the pre-fix MCP output is frozen under
``legacy/`` instead.

Source of truth for ``outer-protobuf-v1.json``: ``rust/totalreclaw-core`` via
the PyO3 wheel (``totalreclaw_core.encode_fact_protobuf`` /
``encode_single_call`` / ``encode_batch_call``). The legacy file comes from
``_legacy_mcp_encode`` below, a FROZEN mirror of the pre-fix TS encoder at
public main 17ffb82 -- do not "fix" it.

Run from the repo root (needs ``totalreclaw_core`` >= 2.5.0 importable)::

    python tests/parity/fixtures/generate-outer-protobuf-fixture.py
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import totalreclaw_core

STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91"
FORBIDDEN_FIELDS = [9, 11]
OWNER = "0x2c0cf74b2b76110708ca431796367779e3738250"
TIMESTAMP = "2026-09-27T12:00:00.000Z"

# Two MCP-shaped payloads. Both carry NON-EMPTY source/agent_id on purpose:
# a correct encoder must drop them even when the caller supplies them.
INPUTS: list[dict] = [
    {
        "name": "mcp_remember_fact",
        "input": {
            "id": "7f3c2a10-6b1e-4c1f-9d2e-0000000000a1",
            "timestamp": TIMESTAMP,
            "owner": OWNER,
            "encrypted_blob_hex": "00ff11ee22dd33cc44bb55aa",
            "blind_indices": [
                "19dbea6d85339ebb338503f75c33e857bb414659a71e80fe5dff0c86dbf8af2b",
                "d00668b150ecec1d0dcfc459e7335c977ad3d6f1e232fa1567505c0e30e2ad05",
            ],
            "decay_score": 0.8,
            "source": "mcp_remember",
            "content_fp": "a08b832e470b9b3e9db90e6bba1fa46ddcc233feaa03e4b7d4e904b3a4438d5c",
            "agent_id": "mcp-server",
            "encrypted_embedding": "ZGVwNi1lbWJlZGRpbmc=",
            "version": 4,
        },
    },
    {
        "name": "mcp_forget_tombstone",
        "input": {
            "id": "7f3c2a10-6b1e-4c1f-9d2e-0000000000b2",
            "timestamp": TIMESTAMP,
            "owner": OWNER,
            "encrypted_blob_hex": "746f6d6273746f6e65",
            "blind_indices": [],
            "decay_score": 0.0,
            "source": "mcp_forget",
            "content_fp": "",
            "agent_id": "mcp-server",
            "encrypted_embedding": None,
            "version": 4,
        },
    },
]

# MCP's remember-with-supersede batch order: tombstone first, then the new fact.
BATCH_ORDER = ["mcp_forget_tombstone", "mcp_remember_fact"]


def field_numbers(buf: bytes) -> list[int]:
    """Walk protobuf wire format and return field numbers in order."""
    out: list[int] = []
    i = 0

    def varint(pos: int) -> tuple[int, int]:
        value, shift = 0, 0
        while True:
            byte = buf[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, pos
            shift += 7

    while i < len(buf):
        key, i = varint(i)
        out.append(key >> 3)
        wire_type = key & 0x07
        if wire_type == 0:
            _, i = varint(i)
        elif wire_type == 1:
            i += 8
        elif wire_type == 2:
            length, i = varint(i)
            i += length
        elif wire_type == 5:
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
    return out


# --- FROZEN mirror of mcp/src/subgraph/store.ts encodeFactProtobuf @ 17ffb82 ---


def _varint(value: int) -> bytes:
    out = bytearray()
    v = value & 0xFFFFFFFF
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _string(field: int, value: str) -> bytes:
    if not value:
        return b""
    data = value.encode("utf-8")
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def _legacy_mcp_encode(inp: dict) -> bytes:
    blob = bytes.fromhex(inp["encrypted_blob_hex"])
    parts = [
        _string(1, inp["id"]),
        _string(2, inp["timestamp"]),
        _string(3, inp["owner"]),
        _varint((4 << 3) | 2) + _varint(len(blob)) + blob,
    ]
    parts += [_string(5, idx) for idx in inp["blind_indices"]]
    parts += [
        _varint((6 << 3) | 1) + struct.pack("<d", inp["decay_score"]),
        _varint((7 << 3) | 0) + _varint(1),
        _varint((8 << 3) | 0) + _varint(4),
        _string(9, inp["source"]),
        _string(10, inp["content_fp"]),
        _string(11, inp["agent_id"]),
    ]
    if inp["encrypted_embedding"]:
        parts.append(_string(13, inp["encrypted_embedding"]))
    return b"".join(parts)


# --- end frozen mirror ---


def build() -> tuple[dict, dict]:
    vectors: list[dict] = []
    legacy_vectors: list[dict] = []
    protobufs: dict[str, bytes] = {}
    for item in INPUTS:
        pb = bytes(totalreclaw_core.encode_fact_protobuf(json.dumps(item["input"])))
        fields = field_numbers(pb)
        assert not set(fields) & set(FORBIDDEN_FIELDS), (item["name"], fields)
        protobufs[item["name"]] = pb
        calldata = bytes(totalreclaw_core.encode_single_call(pb, STAGING_DATA_EDGE))
        vectors.append(
            {
                "name": item["name"],
                "input": item["input"],
                "expected_field_numbers": fields,
                "expected_protobuf_hex": pb.hex(),
                "expected_execute_calldata_hex": calldata.hex(),
            }
        )
        legacy = _legacy_mcp_encode(item["input"])
        legacy_fields = field_numbers(legacy)
        assert set(FORBIDDEN_FIELDS) <= set(legacy_fields), (item["name"], legacy_fields)
        legacy_vectors.append(
            {
                "name": item["name"],
                "expected_field_numbers": legacy_fields,
                "protobuf_hex": legacy.hex(),
            }
        )

    batch = bytes(
        totalreclaw_core.encode_batch_call(
            [protobufs[name] for name in BATCH_ORDER], STAGING_DATA_EDGE
        )
    )
    fixture = {
        "meta": {
            "version": 1,
            "description": (
                "Outer protobuf + SimpleAccount calldata parity for PRD-04 F8 / "
                "DEP-6. Every client encoder (Rust core, Python, TS/WASM, MCP) "
                "MUST produce expected_protobuf_hex for each input, and the "
                "ABI-decoded calldata MUST NOT contain outer fields 9 (source) "
                "or 11 (agent_id). Source of truth: "
                "rust/totalreclaw-core::protobuf::encode_fact_protobuf + "
                "userop::encode_single_call_to / encode_batch_call_to. "
                "Regenerate via: python "
                "tests/parity/fixtures/generate-outer-protobuf-fixture.py"
            ),
            "data_edge_address": STAGING_DATA_EDGE,
            "forbidden_fields": FORBIDDEN_FIELDS,
        },
        "vectors": vectors,
        "batch": {
            "vector_names": BATCH_ORDER,
            "expected_calldata_hex": batch.hex(),
        },
    }
    legacy_fixture = {
        "meta": {
            "version": 1,
            "frozen_from": (
                "mcp/src/subgraph/store.ts encodeFactProtobuf at public main "
                "17ffb82 (before PRD-04 F8 / DEP-6)"
            ),
            "rationale": (
                "Pre-DEP-6 MCP output for the inputs in ../outer-protobuf-v1.json. "
                "It carries plaintext outer fields 9 (source) and 11 (agent_id) -- "
                "the defect. Used ONLY as a negative control: every parity leg "
                "asserts its field walker DOES find 9 and 11 here, so the "
                "absence assertions on the canonical vectors are not vacuous. "
                "Never use these bytes as an expected encoder output."
            ),
        },
        "vectors": legacy_vectors,
    }
    return fixture, legacy_fixture


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    here = Path(__file__).parent
    fixture, legacy_fixture = build()
    _write(here / "outer-protobuf-v1.json", fixture)
    _write(here / "legacy" / "outer-protobuf-mcp-pre-dep6.json", legacy_fixture)
