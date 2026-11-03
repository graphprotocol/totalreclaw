"""PRD-04 F8 / DEP-6 -- outer-protobuf calldata parity (Python leg).

Loads the shared fixture ``tests/parity/fixtures/outer-protobuf-v1.json``
(Rust-generated) and asserts, for the same inputs:

1. the pure-Python encoder (``totalreclaw.protobuf.encode_fact_protobuf``,
   the Hermes write path) and the PyO3 core encoder
   (``totalreclaw_core.encode_fact_protobuf``) are byte-identical to the
   fixture;
2. the SimpleAccount calldata the Python client builds
   (``encode_execute_calldata_for_data_edge`` /
   ``encode_execute_batch_calldata_for_data_edge``) is byte-identical to the
   fixture; and
3. ABI-decoding that calldata yields outer protobufs WITHOUT field 9
   (``source``) or field 11 (``agent_id``), addressed to the fixture DataEdge.

The legacy vectors (``fixtures/legacy/outer-protobuf-mcp-pre-dep6.json``) are
a negative control: the same walker MUST find fields 9 and 11 there.

Siblings: ``tests/parity/outer-protobuf-parity.test.ts`` (TS: MCP + WASM),
``rust/totalreclaw-core/tests/outer_protobuf_parity.rs`` (Rust),
``mcp/tests/protobuf-outer-fields.test.ts`` (MCP unit guard).

Run (CI ``python-tests`` job, local core wheel installed)::

    cd python && python -m pytest ../tests/parity/test_outer_protobuf_parity.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import totalreclaw_core

from totalreclaw.protobuf import FactPayload, encode_fact_protobuf
from totalreclaw.userop import (
    encode_execute_batch_calldata_for_data_edge,
    encode_execute_calldata_for_data_edge,
)

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixtures" / "outer-protobuf-v1.json").read_text())
LEGACY = json.loads(
    (HERE / "fixtures" / "legacy" / "outer-protobuf-mcp-pre-dep6.json").read_text()
)
FORBIDDEN = set(FIXTURE["meta"]["forbidden_fields"])
DATA_EDGE = FIXTURE["meta"]["data_edge_address"]
VECTORS = {v["name"]: v for v in FIXTURE["vectors"]}

SELECTOR_EXECUTE = bytes.fromhex("b61d27f6")
SELECTOR_EXECUTE_BATCH = bytes.fromhex("47e1da2a")


# ---------------------------------------------------------------------------
# Protobuf field walker
# ---------------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def field_numbers(buf: bytes) -> list[int]:
    """Return the outer protobuf field numbers of ``buf`` in wire order."""
    out: list[int] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        out.append(key >> 3)
        wire_type = key & 0x07
        if wire_type == 0:
            _, pos = _read_varint(buf, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            pos += length
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
    assert pos == len(buf), "protobuf walk overran the buffer"
    return out


# ---------------------------------------------------------------------------
# Minimal ABI decoders for SimpleAccount.execute / executeBatch
# ---------------------------------------------------------------------------


def _word(calldata: bytes, offset: int) -> bytes:
    return calldata[4 + offset : 4 + offset + 32]


def _uint(calldata: bytes, offset: int) -> int:
    return int.from_bytes(_word(calldata, offset), "big")


def _address(calldata: bytes, offset: int) -> str:
    word = _word(calldata, offset)
    assert word[:12] == bytes(12), "address word must be left-padded with zeros"
    return "0x" + word[12:].hex()


def _bytes_at(calldata: bytes, offset: int) -> bytes:
    length = _uint(calldata, offset)
    return calldata[4 + offset + 32 : 4 + offset + 32 + length]


def decode_execute(calldata: bytes) -> tuple[str, bytes]:
    """Decode ``execute(address dest, uint256 value, bytes func)``."""
    assert calldata[:4] == SELECTOR_EXECUTE, calldata[:4].hex()
    assert _uint(calldata, 32) == 0, "value must be 0"
    return _address(calldata, 0), _bytes_at(calldata, _uint(calldata, 64))


def decode_execute_batch(calldata: bytes) -> list[tuple[str, bytes]]:
    """Decode ``executeBatch(address[] dest, uint256[] value, bytes[] func)``."""
    assert calldata[:4] == SELECTOR_EXECUTE_BATCH, calldata[:4].hex()
    dest_off, value_off, func_off = (_uint(calldata, k) for k in (0, 32, 64))
    count = _uint(calldata, dest_off)
    assert _uint(calldata, value_off) == count
    assert _uint(calldata, func_off) == count
    calls: list[tuple[str, bytes]] = []
    for k in range(count):
        assert _uint(calldata, value_off + 32 + 32 * k) == 0, "value must be 0"
        element = _uint(calldata, func_off + 32 + 32 * k)
        calls.append(
            (
                _address(calldata, dest_off + 32 + 32 * k),
                _bytes_at(calldata, func_off + 32 + element),
            )
        )
    return calls


def _python_payload(inp: dict) -> FactPayload:
    return FactPayload(
        id=inp["id"],
        timestamp=inp["timestamp"],
        owner=inp["owner"],
        encrypted_blob=inp["encrypted_blob_hex"],
        blind_indices=list(inp["blind_indices"]),
        decay_score=inp["decay_score"],
        source=inp["source"],
        content_fp=inp["content_fp"],
        agent_id=inp["agent_id"],
        encrypted_embedding=inp["encrypted_embedding"],
        version=inp["version"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_python_encoder_matches_fixture_and_omits_fields_9_11(name: str) -> None:
    vector = VECTORS[name]
    encoded = encode_fact_protobuf(_python_payload(vector["input"]))
    assert encoded.hex() == vector["expected_protobuf_hex"]
    fields = field_numbers(encoded)
    assert not FORBIDDEN & set(fields), fields
    assert fields == vector["expected_field_numbers"]


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_pyo3_core_encoder_matches_fixture(name: str) -> None:
    vector = VECTORS[name]
    encoded = bytes(totalreclaw_core.encode_fact_protobuf(json.dumps(vector["input"])))
    assert encoded.hex() == vector["expected_protobuf_hex"]


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_execute_calldata_matches_fixture_and_decodes_clean(name: str) -> None:
    vector = VECTORS[name]
    protobuf = encode_fact_protobuf(_python_payload(vector["input"]))
    calldata_hex = encode_execute_calldata_for_data_edge(protobuf, DATA_EDGE)
    assert calldata_hex.startswith("0x")
    assert calldata_hex[2:] == vector["expected_execute_calldata_hex"]
    dest, inner = decode_execute(bytes.fromhex(calldata_hex[2:]))
    assert dest == DATA_EDGE.lower()
    assert inner == protobuf
    assert not FORBIDDEN & set(field_numbers(inner))


def test_batch_calldata_matches_fixture_and_decodes_clean() -> None:
    names = FIXTURE["batch"]["vector_names"]
    protobufs = [encode_fact_protobuf(_python_payload(VECTORS[n]["input"])) for n in names]
    calldata_hex = encode_execute_batch_calldata_for_data_edge(protobufs, DATA_EDGE)
    assert calldata_hex[2:] == FIXTURE["batch"]["expected_calldata_hex"]
    calls = decode_execute_batch(bytes.fromhex(calldata_hex[2:]))
    assert len(calls) == len(names)
    for (dest, inner), expected in zip(calls, protobufs):
        assert dest == DATA_EDGE.lower()
        assert inner == expected
        assert not FORBIDDEN & set(field_numbers(inner))


def test_legacy_negative_control_contains_fields_9_and_11() -> None:
    for vector in LEGACY["vectors"]:
        fields = field_numbers(bytes.fromhex(vector["protobuf_hex"]))
        assert FORBIDDEN <= set(fields), (vector["name"], fields)
        assert fields == vector["expected_field_numbers"]
