"""Keyed protobuf encoders vs frozen vectors and core (internal#573, spec 4.2)."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import totalreclaw_core

from totalreclaw.protobuf import (
    FactPayload,
    encode_fact_protobuf_keyed,
    encode_tombstone_protobuf_keyed,
)

FX = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "parity" / "fixtures"
     / "keyed-blind-indices-v1.json").read_text()
)
WIRE = FX["wire"]


def _payload_from_input(i: dict) -> FactPayload:
    return FactPayload(
        id=i["id"], timestamp=i["timestamp"], owner=i["owner"],
        encrypted_blob=i["encrypted_blob_hex"], blind_indices=list(i["blind_indices"]),
        decay_score=i.get("decay_score", 1.0), content_fp=i.get("content_fp", ""),
        encrypted_embedding=i.get("encrypted_embedding"), version=i.get("version", 4),
    )


def _read_varint(b, i):
    v = s = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7F) << s
        if not c & 0x80:
            return v, i
        s += 7


def _fields(b: bytes):
    out, i = [], 0
    while i < len(b):
        key, i = _read_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _read_varint(b, i)
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        else:
            n, i = _read_varint(b, i)
            v, i = b[i:i + n], i + n
        out.append((f, v))
    return out


def test_wire_fact_byte_exact():
    got = encode_fact_protobuf_keyed(_payload_from_input(WIRE["fact"]["input"]))
    assert got.hex() == WIRE["fact"]["expected_hex"]


def test_wire_tombstone_byte_exact():
    t = WIRE["tombstone"]
    got = encode_tombstone_protobuf_keyed(t["fact_id"], t["owner"], timestamp=t["timestamp"])
    assert got.hex() == t["expected_hex"]


def test_core_tombstone_prefix_suffix_and_client_equal():
    t = WIRE["tombstone"]
    core_b = bytes(totalreclaw_core.encode_tombstone_protobuf_keyed(t["fact_id"], t["owner"]))
    h = core_b.hex()
    assert h.startswith(t["prefix_hex"][:len(t["prefix_hex"])])
    assert h.endswith(t["suffix_hex"])
    # same layout at the same timestamp: compare field-wise except timestamp
    mine = encode_tombstone_protobuf_keyed(t["fact_id"], t["owner"])
    a = [(f, v) for f, v in _fields(core_b) if f != 2]
    b = [(f, v) for f, v in _fields(mine) if f != 2]
    assert a == b


def _rand_hex_tag(r):
    return "".join(r.choice("0123456789abcdef") for _ in range(64))


def test_random_payloads_equal_core():
    r = random.Random(573)
    for _ in range(200):
        pool = [_rand_hex_tag(r) for _ in range(r.randint(0, 12))]
        tags = pool + r.sample(pool, k=min(len(pool), r.randint(0, 3)))
        r.shuffle(tags)
        blob = "".join(r.choice("0123456789abcdefABCDEF") for _ in range(2 * r.randint(0, 40)))
        p = FactPayload(
            id="id-%d" % r.randint(0, 99999), timestamp="2026-11-25T12:00:00.000Z",
            owner="0x" + "ab" * 20, encrypted_blob=blob, blind_indices=tags,
            decay_score=r.choice([0.0, 1.0, 0.37, 5.5]),
            content_fp=r.choice(["", _rand_hex_tag(r)]),
            encrypted_embedding=r.choice([None, "", "Zm9v"]),
            version=r.choice([0, 3, 4, 9]),
        )
        core_in = {
            "id": p.id, "timestamp": p.timestamp, "owner": p.owner,
            "encrypted_blob_hex": blob.lower() if False else blob, "blind_indices": tags,
            "decay_score": p.decay_score, "source": "", "content_fp": p.content_fp,
            "agent_id": "", "encrypted_embedding": p.encrypted_embedding, "version": p.version,
        }
        want = bytes(totalreclaw_core.encode_fact_protobuf_keyed(json.dumps(core_in)))
        got = encode_fact_protobuf_keyed(p)
        assert got == want
        f5 = [v for f, v in _fields(got) if f == 5]
        assert f5 == sorted(set(f5)) and len(f5) == len(set(f5))
        fs = _fields(got)
        assert fs[-1] == (14, 1)
        assert dict((f, v) for f, v in fs)[8] == 4


BASE = dict(id="x", timestamp="t", owner="o", encrypted_blob="00")


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "a" * 65, "hello"])
def test_bad_tags_raise_without_echo(bad):
    with pytest.raises(ValueError) as ei:
        encode_fact_protobuf_keyed(FactPayload(**BASE, blind_indices=[bad]))
    assert bad not in str(ei.value)


@pytest.mark.parametrize("bad", ["abc", "zz", "00 11", "0g"])
def test_bad_blob_raises_without_echo(bad):
    with pytest.raises(ValueError) as ei:
        encode_fact_protobuf_keyed(FactPayload(**{**BASE, "encrypted_blob": bad}))
    assert bad not in str(ei.value)


def test_empty_blob_writes_empty_field4():
    b = encode_fact_protobuf_keyed(FactPayload(**{**BASE, "encrypted_blob": ""}))
    assert (4, b"") in _fields(b)


def test_tombstone_layout():
    fs = dict(_fields(encode_tombstone_protobuf_keyed("i", "o")))
    assert fs[4] == b"" and fs[7] == 0 and fs[8] == 4 and fs[14] == 1
    assert 5 not in fs and 10 not in fs and 13 not in fs
