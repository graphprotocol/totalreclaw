"""Test 6 at the wire boundary (internal#573, spec 7).

Drive the real client write and read paths with the network replaced by
capturing mock transports, then prove no unkeyed hash of any word, stem,
entity, digest marker or LSH bucket (and no fp v1) is in anything the client
sent. Raw 64-hex and the hex-of-ascii form used inside UserOp calldata are
both scanned, with positive controls through the same pipeline and helper.

Fixture keys and the public test mnemonic come from the frozen-vector file;
nothing here prints a key, tag or query body.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import totalreclaw_core as core

from totalreclaw import operations, retype_setscope, userop
from totalreclaw.bundle import derive_bundle_from_mnemonic
from totalreclaw.claims_helper import build_canonical_claim_v1
from totalreclaw.client import TotalReclaw
from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt
from totalreclaw.relay import INDEX_SCHEME_HEADER, RelayClient

FX = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "parity" / "fixtures"
     / "keyed-blind-indices-v1.json").read_text()
)
MN = FX["meta"]["mnemonic"]
KEYS = derive_keys_from_mnemonic(MN)
DEDUP = bytes.fromhex(FX["meta"]["dedup_key_hex"])
LSH_SEED = bytes.fromhex(FX["meta"]["lsh_seed_hex"])
SA = "0x2c0cf74b2b76110708ca431796367779e3738250"
RELAY = "https://relay.example"
DIMS = 640

TEXTS = [v["text"] for v in FX["stem_vectors"] if v["text"].strip()] + [
    "User prefers PostgreSQL over MySQL at Acme in 2024",
    "Café meeting moved to İstanbul",
]
ENTITY_NAMES = [v["name"] for v in FX["entity_vectors"]] + ["PostgreSQL", "MySQL", "Acme"]
RNG = random.Random(573)
STUB_EMB = [RNG.uniform(-1, 1) for _ in range(DIMS)]
_norm = sum(x * x for x in STUB_EMB) ** 0.5
STUB_EMB = [x / _norm for x in STUB_EMB]


def _banned() -> set[str]:
    b: set[str] = set()
    lg = FX["legacy_sha256"]
    for k in ("tokens", "stems", "entities", "lsh"):
        b.update(lg[k])
    b.add(lg["marker"])
    for t in TEXTS:
        b.update(core.generate_blind_indices(t))
        b.add(core.generate_content_fingerprint(t, DEDUP))
    for n in ENTITY_NAMES:
        b.add(hashlib.sha256(b"entity:" + core.normalize_entity_name(n).encode()).hexdigest())
    b.add(hashlib.sha256(b"type:digest").hexdigest())
    b.update(core.LshHasher(LSH_SEED, DIMS, 20, 32).hash(STUB_EMB))
    return {h.lower() for h in b}


BANNED = _banned()
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN = re.compile(r"[0-9a-f]{128,}")


def scan(bodies: list[str]) -> list[tuple[str, str]]:
    """Every banned hash found in the captured bodies, as (hash, "raw"|"hex")."""
    hits: list[tuple[str, str]] = []
    for body in bodies:
        low = body.lower()
        for h in set(_HEX64.findall(low)) & BANNED:
            hits.append((h, "raw"))
        for run in _RUN.findall(low):
            for off in (0, 1):
                r = run[off:]
                r = r[: len(r) - len(r) % 2]
                try:
                    ascii_ = bytes.fromhex(r).decode("latin-1")
                except ValueError:
                    continue
                for h in set(_HEX64.findall(ascii_)) & BANNED:
                    hits.append((h, "hex"))
    return hits


# ---------------------------------------------------------------------------
# Mock transports
# ---------------------------------------------------------------------------

_VALID_SPONSOR = {"result": {
    "callGasLimit": "0x186a0", "verificationGasLimit": "0x30d40", "preVerificationGas": "0xc350",
    "paymaster": "0x0000000000000000000000000000000000000000", "paymasterData": "0x",
    "paymasterVerificationGasLimit": "0x30d40", "paymasterPostOpGasLimit": "0x30d40"}}


class World:
    """State shared by the mock subgraph and the mock bundler."""

    def __init__(self):
        self.subgraph: list[dict] = []   # {headers, body}
        self.bundler: list[dict] = []    # {headers, body, method}
        self.inactive: set[str] = set()
        self.pinned_blobs: set[str] = set()

    def _blob(self, pinned: bool) -> str:
        class F:
            pass

        f = F()
        f.text, f.type, f.source, f.scope = TEXTS[0], "preference", "user", "unspecified"
        f.reasoning = f.volatility = None
        f.entities = None
        f.confidence = 0.9
        claim = build_canonical_claim_v1(
            f, importance=7, created_at="2026-11-01T00:00:00.000Z", claim_id="x",
            pin_status="pinned" if pinned else None)
        import base64

        return "0x" + base64.b64decode(encrypt(claim, KEYS.encryption_key)).hex()

    def subgraph_handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.subgraph.append({"headers": dict(request.headers), "body": body})
        j = json.loads(body)
        q, v = j["query"], j.get("variables", {})
        if "FactIndexScheme" in q:
            return httpx.Response(200, json={"data": {"fact": {"id": v["id"], "owner": SA, "indexScheme": 1}}})
        if "blockNumber" in q and "isActive" in q and "fact(id" in q:
            return httpx.Response(200, json={"data": {"fact": {
                "id": v["id"], "isActive": v["id"] not in self.inactive, "blockNumber": "1"}}})
        if "fact(id" in q:
            return httpx.Response(200, json={"data": {"fact": {
                "id": v["id"], "owner": SA, "encryptedBlob": self._blob(v["id"] in self.pinned_blobs),
                "encryptedEmbedding": None, "decayScore": "1", "timestamp": "1", "createdAt": "1",
                "isActive": True, "contentFp": "c"}}})
        return httpx.Response(200, json={"data": {"blindIndexes": [], "facts": []}})

    def bundler_handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        j = json.loads(body)
        self.bundler.append({"headers": dict(request.headers), "body": body, "method": j["method"]})
        m = j["method"]
        if m == "pimlico_getUserOperationGasPrice":
            return httpx.Response(200, json={"result": {"fast": {
                "maxFeePerGas": "0x77359400", "maxPriorityFeePerGas": "0x59682f00"}}})
        if m == "pm_sponsorUserOperation":
            return httpx.Response(200, json=_VALID_SPONSOR)
        if m == "eth_sendUserOperation":
            return httpx.Response(200, json={"result": "0xhash"})
        if m == "eth_getUserOperationReceipt":
            return httpx.Response(200, json={"result": {"success": True, "receipt": {"transactionHash": "0xabc"}}})
        if m == "pimlico_getUserOperationStatus":
            return httpx.Response(200, json={"result": {"status": "submitted"}})
        raise AssertionError("unexpected relay method")


def _attach_subgraph(client: TotalReclaw, world: World) -> None:
    transport = httpx.MockTransport(world.subgraph_handler)

    async def _get_http():
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    client._relay._get_http = _get_http  # type: ignore[assignment]


@pytest.fixture
def env(monkeypatch):
    world = World()
    real_cls = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(world.bundler_handler)
        return real_cls(*a, **kw)

    proxy = types.SimpleNamespace(**{k: getattr(httpx, k) for k in dir(httpx) if not k.startswith("__")})
    proxy.AsyncClient = factory
    monkeypatch.setattr(userop, "httpx", proxy)
    monkeypatch.setattr(userop, "get_nonce", AsyncMock(return_value=5))
    monkeypatch.setattr(userop, "_eth_get_code", AsyncMock(return_value="0x01"))
    monkeypatch.setattr(userop, "_eth_block_number", AsyncMock(return_value=1000))
    clock = {"t": 1000.0}

    async def _vsleep(s):
        clock["t"] += max(s, 0.001)

    monkeypatch.setattr(userop, "_now", lambda: clock["t"])
    monkeypatch.setattr(userop, "_sleep", _vsleep)
    monkeypatch.setattr(operations, "get_embedding", lambda t: list(STUB_EMB))
    monkeypatch.setattr(retype_setscope, "get_embedding", lambda t: list(STUB_EMB))
    for fn in ("_reset_deployed_cache_for_tests", "_reset_nonce_cache_for_tests", "_reset_sender_locks_for_tests"):
        getattr(userop, fn)()
    yield world
    for fn in ("_reset_deployed_cache_for_tests", "_reset_nonce_cache_for_tests", "_reset_sender_locks_for_tests"):
        getattr(userop, fn)()


def _phrase_client(world: World) -> TotalReclaw:
    c = TotalReclaw(mnemonic=MN, relay_url=RELAY, wallet_address=SA)
    c._ensure_registered = AsyncMock()
    c._ensure_chain_id = AsyncMock(return_value=100)
    _attach_subgraph(c, world)
    return c


def _bundle_client(world: World) -> TotalReclaw:
    bundle = derive_bundle_from_mnemonic(MN, 100, "local-migration", SA)
    c = TotalReclaw.from_bundle(bundle, server_url=RELAY)
    c._ensure_chain_id = AsyncMock(return_value=100)
    _attach_subgraph(c, world)
    return c


def _sub_bodies(world, start=0):
    return [c["body"] for c in world.subgraph[start:]]


def _bun_calldata_bodies(world, start=0):
    return [c["body"] for c in world.bundler[start:] if '"callData"' in c["body"]]


# ---------------------------------------------------------------------------
# Calldata walker
# ---------------------------------------------------------------------------


def _u(cd: bytes, off: int) -> int:
    return int.from_bytes(cd[4 + off: 4 + off + 32], "big")


def _bytes_at(cd: bytes, off: int) -> bytes:
    n = _u(cd, off)
    return cd[4 + off + 32: 4 + off + 32 + n]


def decode_inner_payloads(call_data_hex: str) -> list[bytes]:
    cd = bytes.fromhex(call_data_hex[2:])
    sel = cd[:4].hex()
    if sel == "b61d27f6":
        return [_bytes_at(cd, _u(cd, 64))]
    assert sel == "47e1da2a", sel
    d_off, v_off, f_off = (_u(cd, k) for k in (0, 32, 64))
    n = _u(cd, f_off)
    return [_bytes_at(cd, f_off + 32 + _u(cd, f_off + 32 + 32 * k)) for k in range(n)]


def parse_fields(b: bytes):
    out, i = [], 0

    def vint(i):
        v = s = 0
        while True:
            c = b[i]; i += 1
            v |= (c & 0x7F) << s
            if not c & 0x80:
                return v, i
            s += 7

    while i < len(b):
        key, i = vint(i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = vint(i)
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        else:
            n, i = vint(i)
            v, i = b[i:i + n], i + n
        out.append((f, v))
    return out


def sent_payloads(world: World, start: int = 0) -> list[bytes]:
    out = []
    for c in world.bundler[start:]:
        if c["method"] == "eth_sendUserOperation":
            op = json.loads(c["body"])["params"][0]
            out.extend(decode_inner_payloads(op["callData"]))
    return out


def check_payload(p: bytes) -> None:
    fs = parse_fields(p)
    assert fs[-1] == (14, 1) and [f for f, _ in fs].count(14) == 1
    d = dict(fs)
    assert d[8] == 4
    tags = [v for f, v in fs if f == 5]
    assert tags == sorted(set(tags))
    if d[4] == b"" or d[7] == 0:  # tombstone
        assert d[4] == b"" and d[6] == b"\x00" * 8 and d[7] == 0
        assert 5 not in d and 10 not in d and 13 not in d


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

ENT = [{"name": "PostgreSQL", "type": "tool"}, {"name": "Acme", "type": "company"}]


async def _drive(c: TotalReclaw, world: World, log: dict, *, full: bool):
    def mark(op):
        log[op] = (len(world.subgraph), len(world.bundler))

    s0, b0 = len(world.subgraph), len(world.bundler)
    mark("start")
    fid = await c.remember(TEXTS[-2], embedding=list(STUB_EMB), entities=ENT)
    log["remember"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))

    s0, b0 = len(world.subgraph), len(world.bundler)
    await c.recall("What does the user prefer PostgreSQL for?", query_embedding=list(STUB_EMB))
    log["recall_emb"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))
    s0, b0 = len(world.subgraph), len(world.bundler)
    await c.recall("Where is Acme based?")
    log["recall_noemb"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))
    if not full:
        return fid

    s0, b0 = len(world.subgraph), len(world.bundler)
    facts = [{"text": t, "embedding": list(STUB_EMB), "entities": ENT} for t in TEXTS]
    await c.remember_batch(facts)
    log["remember_batch"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))

    s0, b0 = len(world.subgraph), len(world.bundler)
    await c.find_duplicate_texts(TEXTS[:5])
    log["find_dup"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))

    # pin / retype / set_scope on a fact this client wrote (guard skipped),
    # and one seeded only in the mock (guard query sent)
    for label, fn in (
        ("pin_written", lambda: c.pin_fact(fid)),
        ("pin_seeded", lambda: c.pin_fact("seeded-1")),
        ("retype_seeded", lambda: c.retype("seeded-2", "claim")),
        ("scope_seeded", lambda: c.set_scope("seeded-3", "health")),
    ):
        s0, b0 = len(world.subgraph), len(world.bundler)
        res = await fn()
        assert res["success"] is True
        log[label] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))
    world.pinned_blobs.add("seeded-pinned")
    s0, b0 = len(world.subgraph), len(world.bundler)
    res = await c.unpin_fact("seeded-pinned")
    assert res["success"] is True
    log["unpin_seeded"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))

    s0, b0 = len(world.subgraph), len(world.bundler)
    world.inactive.add("to-forget")
    assert await c.forget("to-forget")
    log["forget"] = ((s0, len(world.subgraph)), (b0, len(world.bundler)))
    return fid


@pytest.mark.asyncio
async def test_no_unkeyed_hash_on_the_wire(env):
    world = env
    c = _phrase_client(world)
    log: dict = {}
    await _drive(c, world, log, full=True)

    # bundle (enclave) client: remember and recall
    cb = _bundle_client(world)
    blog: dict = {}
    s_phrase_end, b_phrase_end = len(world.subgraph), len(world.bundler)
    await _drive(cb, world, blog, full=False)

    # 1. nothing banned anywhere
    all_sub = _sub_bodies(world)
    all_bun = _bun_calldata_bodies(world)
    assert scan(all_sub + all_bun) == []

    # per-op minimum captures
    def counts(op):
        (s0, s1), (b0, b1) = log[op]
        sub = len(world.subgraph[s0:s1])
        bun = len([x for x in world.bundler[b0:b1] if '"callData"' in x["body"]])
        return sub, bun

    sub, bun = counts("remember"); assert sub == 0 and bun >= 1
    sub, bun = counts("remember_batch"); assert sub == 0 and bun >= 1
    for op in ("recall_emb", "recall_noemb", "find_dup"):
        sub, bun = counts(op); assert sub >= 1 and bun == 0, op
    for op in ("pin_seeded", "retype_seeded", "scope_seeded", "unpin_seeded"):
        sub, bun = counts(op); assert sub >= 2 and bun >= 1, op  # fetch + guard query
    sub, bun = counts("pin_written"); assert sub >= 1 and bun >= 1
    sub, bun = counts("forget"); assert sub >= 1 and bun >= 1
    # no banned hash in any single op's captures either
    for op in log:
        if op == "start":
            continue
        (s0, s1), (b0, b1) = log[op]
        assert scan([x["body"] for x in world.subgraph[s0:s1]]
                    + [x["body"] for x in world.bundler[b0:b1] if '"callData"' in x["body"]]) == [], op

    # 2. every relay request carries the scheme header
    for cap in world.subgraph + world.bundler:
        assert cap["headers"][INDEX_SCHEME_HEADER.lower()] == "1"

    # 3. every inner payload is scheme 1
    payloads = sent_payloads(world)
    assert len(payloads) >= 10
    for p in payloads:
        check_payload(p)
    assert any(dict(parse_fields(p))[7] == 0 for p in payloads)  # at least one tombstone (pin/forget)

    # 4. only the guard query mentions indexScheme
    for cap in world.subgraph:
        has = "indexScheme" in cap["body"]
        assert has == ("FactIndexScheme" in cap["body"])
    # the written-here pin sent no guard query; the seeded ones did
    (s0, s1), _ = log["pin_written"]
    assert not any("FactIndexScheme" in x["body"] for x in world.subgraph[s0:s1])
    (s0, s1), _ = log["pin_seeded"]
    assert any("FactIndexScheme" in x["body"] for x in world.subgraph[s0:s1])

    # 5. phrase path and bundle path produce the same tag set for the same remember
    def tags_of(client_log, start_b, label):
        (b0, b1) = client_log["remember"][1]
        ps = sent_payloads(world, b0)
        # first payload sent after b0 belongs to the remember
        return [v for f, v in parse_fields(ps[0]) if f == 5]

    t_phrase = tags_of(log, 0, "remember")
    (b0, _b1) = blog["remember"][1]
    t_bundle = [v for f, v in parse_fields(sent_payloads(world, b0)[0]) if f == 5]
    assert t_phrase == t_bundle and len(t_phrase) > 20


# ---------------------------------------------------------------------------
# Positive controls (separate worlds and clients, same pipeline and helper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_control_raw_through_subgraph_transport(env):
    world = env
    c = _phrase_client(world)
    h = sorted(BANNED)[0]
    await c._relay.query_subgraph(operations.SEARCH_QUERY, {"trapdoors": [h], "owner": SA, "first": 5})
    assert (h, "raw") in scan(_sub_bodies(world))


@pytest.mark.asyncio
async def test_positive_control_hex_through_userop_transport(env):
    world = env
    legacy = FX["legacy_sha256"]["tokens"][:2]
    payload = bytes(core.encode_fact_protobuf(json.dumps({
        "id": "ctl", "timestamp": "2026-11-25T12:00:00.000Z", "owner": SA,
        "encrypted_blob_hex": "00ff", "blind_indices": legacy, "decay_score": 1.0,
        "source": "", "content_fp": "", "agent_id": "", "version": 4,
    })))
    await userop.build_and_send_userop(
        sender=SA, eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
        eoa_private_key=_eoa_key(), protobuf_payload=payload, relay_url=RELAY,
        auth_key_hex="aa", wallet_address=SA, chain_id=100,
    )
    hits = scan(_bun_calldata_bodies(world))
    for h in legacy:
        assert (h, "hex") in hits and (h, "raw") not in hits


def _eoa_key() -> bytes:
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    return bytes(Account.from_mnemonic(MN, account_path="m/44'/60'/0'/0/0").key)


@pytest.mark.asyncio
async def test_a_half_ported_write_is_caught_by_the_scan(env):
    """If a call site leaked one unkeyed tag, the real write path would show it."""
    from totalreclaw import blind_tags

    world = env
    c = _phrase_client(world)
    leaked = FX["legacy_sha256"]["tokens"][0]
    real = blind_tags.fact_tags

    def leaky(*a, **kw):
        return sorted(set(real(*a, **kw) + [leaked]))

    with patch.object(blind_tags, "fact_tags", leaky):
        await c.remember(TEXTS[0], embedding=list(STUB_EMB))
    assert (leaked, "hex") in scan(_bun_calldata_bodies(world))
