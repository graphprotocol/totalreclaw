"""A fake ERC-4337 world for the write-path tests (internal #646 / #650).

One in-memory ``World`` models, on a controllable clock:

* chain state per sender (code, EntryPoint nonce) with block history, replica
  lag and reorg injection, plus a ``UserOperationEvent`` log per executed op;
* the Alto mempool in its predecessor states (outstanding, submitted, mined),
  with the exact Alto ``main`` error strings the client classifies on;
* Pimlico's paymaster simulation (``pm_sponsorUserOperation``) with an optional
  one-block simulator lag;
* faults: drop, revert, receipt lag, transport timeout at send (accepted or
  not), HTTP errors without a JSON-RPC body, foreign ops (another process),
  fee-bump replacement, malformed ``eth_call`` replies, public RPC outage;
* a relay quota ledger mirroring ``proxy.ts`` in three modes.

``World`` has no dependency on httpx or asyncio: the unit tier wraps it in
``FakeAsyncClient`` (virtual clock), the local tier serves the same object
over HTTP (real clock).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
from eth_hash.auto import keccak

import totalreclaw.userop as userop

SENDER_DEFAULT = "0x1111111111111111111111111111111111111111"

EXECUTE_SELECTOR = "b61d27f6"        # execute(address,uint256,bytes)
EXECUTE_BATCH_SELECTOR = "47e1da2a"  # executeBatch(address[],uint256[],bytes[])
ACCOUNT_DEPLOYED_TOPIC = "0x" + keccak(
    b"AccountDeployed(bytes32,address,address,address)"
).hex()


class RpcError(Exception):
    """A JSON-RPC error answered with HTTP 200 (the way the relay passes
    Pimlico errors through)."""

    def __init__(self, message: str, code: int = -32500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class HttpFault(Exception):
    """An HTTP error without a JSON-RPC body (or a transport failure)."""

    def __init__(self, status: int, text: str = "upstream error") -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.text = text


class TransportFault(Exception):
    """The connection failed (timeout, reset)."""


# ---------------------------------------------------------------------------
# calldata decoding (to track payload execution and fact counts)
# ---------------------------------------------------------------------------


def _word(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 32], "big")


def decode_payloads(call_data_hex: str) -> list[bytes]:
    """Inner protobuf payloads of ``execute`` / ``executeBatch`` calldata."""
    raw = bytes.fromhex(call_data_hex[2:])
    sel, body = raw[:4].hex(), raw[4:]
    if sel == EXECUTE_SELECTOR:
        off = _word(body, 64)
        ln = _word(body, off)
        return [body[off + 32: off + 32 + ln]]
    if sel == EXECUTE_BATCH_SELECTOR:
        datas_off = _word(body, 64)
        n = _word(body, datas_off)
        base = datas_off + 32
        out = []
        for i in range(n):
            rel = _word(body, base + 32 * i)
            p = base + rel
            ln = _word(body, p)
            out.append(body[p + 32: p + 32 + ln])
        return out
    raise ValueError(f"unknown selector {sel}")


# ---------------------------------------------------------------------------
# the world
# ---------------------------------------------------------------------------


@dataclass
class Op:
    hash: str
    sender: str
    nonce: int
    factory: bool
    call_data: str
    max_fee: int
    prio: int
    payloads: list[bytes]
    status: str = "outstanding"   # outstanding|submitted|included|dropped|replaced|reorged
    owner: str = "ours"           # ours|foreign
    accepted_t: float = 0.0
    submitted_t: float = 0.0
    mined_t: float = 0.0
    mined_block: int = 0
    success: Optional[bool] = None
    revert: bool = False          # fault: revert on execution
    hide_receipt_until: float = 0.0
    billed: int = 0               # quota units still held by the relay for this op
    refunded_gap_b: bool = False


@dataclass
class Acct:
    code_history: list = field(default_factory=lambda: [(0, False)])
    nonce_history: list = field(default_factory=lambda: [(0, 0)])


class World:
    BASE_BLOCK = 1000

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        mine_delay: float = 6.0,
        bundle_delay: float = 1.0,
        block_time: float = 5.0,
        ledger_mode: str = "today",     # today | pr87 | pr87_drops
        chain_id: int = 100,
    ) -> None:
        self._clock = clock
        self._t = 0.0
        self.mine_delay = mine_delay
        self.bundle_delay = bundle_delay
        self.block_time = block_time
        self.ledger_mode = ledger_mode
        self.chain_id = chain_id
        self.accts: dict[str, Acct] = {}
        self.ops: dict[str, Op] = {}
        self.events: list[dict] = []      # UserOperationEvent / AccountDeployed
        self.executed_payloads: list[bytes] = []
        self.quota_used = 0
        self.gas_price = 2_000_000_000
        # ---- faults / knobs ----
        self.sim_lag_blocks = 0          # paymaster simulator sees chain this many blocks behind
        self.replica_lag_reads = 0       # next N public reads return block-1 state
        self.receipt_lag_s = 0.0
        self.rpc_down = False
        self.malformed_eth_call = 0      # next N eth_call replies carry no result
        self.send_faults: list[str] = []  # popped one per eth_sendUserOperation
        self.sponsor_faults: list[str] = []
        self.revert_next = 0             # next N accepted ops revert on execution
        self.drop_next = 0               # next N accepted ops are dropped at bundle time
        self.status_overrides: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []   # (method, kind)
        self.log: list[str] = []
        self.refund_http_errors = False  # relay Gap A fixed?
        self.status_lies: dict[str, int] = {}   # hash or '*' -> remaining not_found answers
        self.before_send: list[Callable[['World'], None]] = []
        self.after_accept: list[Callable[['World', 'Op'], None]] = []
        self.sponsor_errs: list[str] = []
        self.send_errs: list[str] = []
        self.send_count = 0
        self.clock_started = (clock or (lambda: 0.0))()

    # ---- time ----
    def now(self) -> float:
        if self._clock is not None:
            return self._clock() - self.clock_started
        return self._t

    def advance(self, dt: float) -> None:
        """Virtual clock only: step in small slices so ordering is right."""
        assert self._clock is None, "advance() is for the virtual clock"
        end = self._t + dt
        step = 0.25
        while self._t < end:
            self._t = min(self._t + step, end)
            self.tick()

    @property
    def block(self) -> int:
        return self.BASE_BLOCK + int(self.now() // self.block_time)

    # ---- chain views ----
    def acct(self, sender: str) -> Acct:
        return self.accts.setdefault(sender.lower(), Acct())

    def _at(self, history: list, block: int):
        val = history[0][1]
        for b, v in history:
            if b <= block:
                val = v
        return val

    def chain_nonce(self, sender: str, block: Optional[int] = None) -> int:
        return self._at(self.acct(sender).nonce_history, self.block if block is None else block)

    def has_code(self, sender: str, block: Optional[int] = None) -> bool:
        return self._at(self.acct(sender).code_history, self.block if block is None else block)

    def _write_nonce(self, sender: str, value: int) -> None:
        self.acct(sender).nonce_history.append((self.block, value))

    def _write_code(self, sender: str, value: bool) -> None:
        self.acct(sender).code_history.append((self.block, value))

    # ---- mempool helpers ----
    def active(self, sender: str, nonce: Optional[int] = None) -> list[Op]:
        return [
            o for o in self.ops.values()
            if o.sender.lower() == sender.lower()
            and o.status in ("outstanding", "submitted")
            and (nonce is None or o.nonce == nonce)
        ]

    def tick(self) -> None:
        """Bundle and mine whatever is due."""
        t = self.now()
        changed = True
        while changed:
            changed = False
            for sender in {o.sender.lower() for o in self.ops.values()}:
                nonce = self.chain_nonce(sender, 10**9)
                subs = [o for o in self.active(sender) if o.status == "submitted"]
                if not subs:
                    nxt = [o for o in self.active(sender, nonce) if o.status == "outstanding"]
                    if nxt and t >= nxt[0].accepted_t + self.bundle_delay:
                        op = nxt[0]
                        if self.drop_next and op.owner == "ours":
                            self.drop_next -= 1
                            op.status = "dropped"
                            changed = True
                            continue
                        op.status = "submitted"
                        op.submitted_t = max(t, op.accepted_t + self.bundle_delay)
                        op.mined_t = op.submitted_t + self.mine_delay
                        changed = True
                for op in sorted(subs, key=lambda o: o.mined_t):
                    if t >= op.mined_t:
                        self._execute(op)
                        changed = True

    def _execute(self, op: Op) -> None:
        sender = op.sender
        cur = self.chain_nonce(sender, 10**9)
        blk = self.BASE_BLOCK + int(op.mined_t // self.block_time)
        if op.nonce != cur or (op.factory and self.has_code(sender, 10**9)):
            op.status = "dropped"
            return
        if op.factory:
            self.acct(sender).code_history.append((blk, True))
            self.events.append({"kind": "deploy", "hash": op.hash, "sender": sender, "block": blk})
        self.acct(sender).nonce_history.append((blk, cur + 1))
        op.status = "included"
        op.mined_block = blk
        op.success = not (op.revert)
        op.hide_receipt_until = op.mined_t + self.receipt_lag_s
        self.events.append({
            "kind": "uop", "hash": op.hash, "sender": sender, "nonce": op.nonce,
            "success": op.success, "block": blk,
        })
        if op.success:
            self.executed_payloads.extend(op.payloads)

    def reorg(self, blocks: int) -> None:
        """Roll back the last ``blocks`` blocks: executed ops there are lost."""
        cut = self.block - blocks
        for acct in self.accts.values():
            acct.nonce_history = [(b, v) for b, v in acct.nonce_history if b <= cut] or [(0, 0)]
            acct.code_history = [(b, v) for b, v in acct.code_history if b <= cut] or [(0, False)]
        keep = []
        for e in self.events:
            if e["block"] > cut:
                op = self.ops.get(e["hash"])
                if op and e["kind"] == "uop":
                    op.status = "reorged"
                    for p in op.payloads:
                        if p in self.executed_payloads:
                            self.executed_payloads.remove(p)
                continue
            keep.append(e)
        self.events = keep

    # ---- paymaster / bundler ----
    def _sim_view_block(self) -> int:
        return self.block - self.sim_lag_blocks

    def simulate(self, op_dict: dict) -> None:
        """Pimlico's simulation. Raises RpcError like the real service."""
        sender = op_dict["sender"]
        nonce = int(op_dict["nonce"], 16)
        has_factory = bool(op_dict.get("factory"))
        vb = self._sim_view_block()
        pre = "UserOperation reverted during simulation with reason: "
        if not self.has_code(sender, vb) and not has_factory:
            raise RpcError(pre + "AA20 account not deployed")
        if self.has_code(sender, vb) and has_factory:
            raise RpcError(pre + "AA10 sender already constructed")
        expected = self.chain_nonce(sender, vb)
        outstanding = {o.nonce for o in self.active(sender) if o.status == "outstanding"}
        base = expected
        while expected in outstanding:
            expected += 1
        # a same-nonce op may REPLACE an outstanding one (fee bump), so it passes
        if nonce != expected and not (base <= nonce < expected):
            raise RpcError(pre + "AA25 invalid account nonce")

    def _hash(self, op_dict: dict) -> str:
        return "0x" + userop.compute_user_op_hash(op_dict, userop.ENTRYPOINT_V07, self.chain_id).hex()

    # ---- relay endpoints ----
    def relay_rpc(self, method: str, params: list) -> dict:
        """Returns the JSON-RPC body, or raises HttpFault / TransportFault."""
        self.tick()
        self.calls.append((method, "relay"))
        if method == "pimlico_getUserOperationGasPrice":
            f = hex(self.gas_price)
            return {"result": {"fast": {"maxFeePerGas": f, "maxPriorityFeePerGas": hex(self.gas_price - 500_000_000)}}}
        if method == "pm_sponsorUserOperation":
            if self.sponsor_faults:
                fault = self.sponsor_faults.pop(0)
                if fault == "http504":
                    raise HttpFault(504)
                if fault == "timeout":
                    raise TransportFault()
                return {"error": {"code": -32500, "message": fault}}
            try:
                self.simulate(params[0])
            except RpcError as e:
                self.sponsor_errs.append(e.message)
                return {"error": {"code": e.code, "message": e.message}}
            return {"result": {
                "callGasLimit": hex(100000), "verificationGasLimit": hex(200000),
                "preVerificationGas": hex(50000),
                "paymaster": "0x2222222222222222222222222222222222222222",
                "paymasterData": "0x", "paymasterVerificationGasLimit": hex(50000),
                "paymasterPostOpGasLimit": hex(30000),
            }}
        if method == "eth_sendUserOperation":
            return self._send(params[0])
        if method == "eth_getUserOperationReceipt":
            op = self.ops.get(params[0])
            if op and op.status == "included" and self.now() >= op.hide_receipt_until:
                return {"result": {"userOpHash": op.hash, "success": op.success, "nonce": hex(op.nonce)}}
            return {"result": None}
        if method == "pimlico_getUserOperationStatus":
            h = params[0]
            for k in (h, "*"):
                if self.status_lies.get(k, 0) > 0:
                    self.status_lies[k] -= 1
                    return {"result": {"status": "not_found"}}
            if h in self.status_overrides:
                return {"result": {"status": self.status_overrides[h]}}
            op = self.ops.get(h)
            st = {
                None: "not_found", "outstanding": "not_submitted", "submitted": "submitted",
                "included": "included", "dropped": "rejected", "replaced": "not_found",
                "reorged": "not_found",
            }[op.status if op else None]
            return {"result": {"status": st}}
        raise AssertionError(f"unexpected relay method {method}")

    def _n_facts(self, op_dict: dict) -> int:
        return len(decode_payloads(op_dict["callData"]))

    def _send(self, op_dict: dict) -> dict:
        if self.before_send:
            self.before_send.pop(0)(self)
        fault = self.send_faults.pop(0) if self.send_faults else None
        self.send_count += 1
        n = self._n_facts(op_dict)
        h = self._hash(op_dict)
        if fault in ("http429", "http502"):
            # Relay Gap A: an upstream HTTP error with no JSON-RPC body is NOT
            # refunded; the debit was taken before the forward.
            self.quota_used += n
            self._gap_a_unrefunded = getattr(self, "_gap_a_unrefunded", 0) + n
            if self.refund_http_errors:
                self.quota_used -= n
                self._gap_a_unrefunded -= n
            raise HttpFault(429 if fault == "http429" else 502)
        # debit before the forward, as proxy.ts does
        self.quota_used += n
        try:
            if fault and fault not in (
                "timeout_accept", "http504_accept", "timeout_reject", "http504_reject"
            ):
                raise RpcError(fault)       # a raw JSON-RPC error text
            self.simulate(op_dict)
            res = self._mempool_accept(op_dict, h, n)
        except RpcError as e:
            self.quota_used -= n
            self.send_errs.append(e.message)
            return {"error": {"code": e.code, "message": e.message}}
        if self.after_accept:
            self.after_accept.pop(0)(self, self.ops[h])
        if fault in ("timeout_accept", "http504_accept"):
            op = self.ops[h]
            # relay's fetch timed out: it refunds although Pimlico accepted (Gap B)
            self.quota_used -= n
            op.billed = 0
            op.refunded_gap_b = True
            raise HttpFault(504) if fault == "http504_accept" else TransportFault()
        if fault in ("timeout_reject", "http504_reject"):
            # not accepted: undo the accept and refund
            self.quota_used -= n
            op = self.ops.pop(h)
            raise HttpFault(504) if fault == "http504_reject" else TransportFault()
        return {"result": res}

    def _mempool_accept(self, op_dict: dict, h: str, n: int) -> str:
        sender, nonce = op_dict["sender"], int(op_dict["nonce"], 16)
        max_fee = int(op_dict["maxFeePerGas"], 16)
        prio = int(op_dict["maxPriorityFeePerGas"], 16)
        has_factory = bool(op_dict.get("factory"))
        bump = ", bump the gas price by minimum 10%"
        if self.chain_nonce(sender, 10**9) > nonce:
            raise RpcError("AA25 invalid account nonce: nonce too low")
        existing = self.ops.get(h)
        if existing and existing.status in ("outstanding", "submitted"):
            raise RpcError("Already known")
        same = self.active(sender, nonce)
        if same:
            old = same[0]
            if old.status == "submitted":
                raise RpcError(
                    "AA25 invalid account nonce: Another UserOperation with same "
                    "sender and nonce is already being processed"
                )
            if max_fee * 100 >= old.max_fee * 110 and prio * 100 >= old.prio * 110:
                old.status = "replaced"
            else:
                raise RpcError("AA25 invalid account nonce: User operation already present in mempool" + bump)
        if has_factory:
            for o in self.active(sender):
                if o.factory and o.nonce != nonce:
                    if o.status == "submitted":
                        raise RpcError(
                            "AA10 invalid account deployment: Another deployment operation for "
                            "this sender is already being processed"
                        )
                    raise RpcError(
                        "AA10 sender already constructed: A conflicting userOperation with "
                        "initCode for this sender is already in the mempool" + bump
                    )
        op = Op(
            hash=h, sender=sender, nonce=nonce, factory=has_factory,
            call_data=op_dict["callData"], max_fee=max_fee, prio=prio,
            payloads=decode_payloads(op_dict["callData"]), accepted_t=self.now(),
            billed=n,
        )
        if self.revert_next:
            self.revert_next -= 1
            op.revert = True
        prev = self.ops.get(h)
        if prev is not None:
            op.billed += prev.billed     # same hash re-sent after a drop: both debits stay on the book
        self.ops[h] = op
        return h

    # ---- foreign actors ----
    def inject_foreign(
        self, sender: str, nonce: int, *, fee_mult: float = 1.0, factory: bool = False,
        tag: Optional[bytes] = None, revert: bool = False,
    ) -> Op:
        """Another process submits an op for the account."""
        self._foreign_n = getattr(self, "_foreign_n", 0) + 1
        tag = tag or b"foreign-%d" % self._foreign_n
        self.tick()
        fee = int(self.gas_price * fee_mult)
        od = {
            "sender": sender, "nonce": hex(nonce), "callData": _calldata_for([tag]),
            "maxFeePerGas": hex(fee), "maxPriorityFeePerGas": hex(max(fee - 500_000_000, 1)),
            "signature": "0x", "factory": userop.SIMPLE_ACCOUNT_FACTORY if factory else None,
            "callGasLimit": "0x1", "verificationGasLimit": "0x1", "preVerificationGas": "0x1",
        }
        od = {k: v for k, v in od.items() if v is not None}
        h = "0x" + keccak(json.dumps(od, sort_keys=True).encode() + tag).hex()
        if factory:
            od["factoryData"] = "0x"
        op = Op(
            hash=h, sender=sender, nonce=nonce, factory=factory, call_data=od["callData"],
            max_fee=fee, prio=max(fee - 500_000_000, 1), payloads=[tag], owner="foreign",
            accepted_t=self.now(), billed=0,
        )
        op.revert = revert
        for old in self.active(sender, nonce):
            if fee * 100 >= old.max_fee * 110:
                old.status = "replaced"
            else:
                raise RpcError("foreign op rejected")
        self.ops[h] = op
        return op

    # ---- public RPC ----
    def public_rpc(self, method: str, params: list) -> dict:
        self.tick()
        self.calls.append((method, "public"))
        if self.rpc_down:
            raise TransportFault()
        read_block = self.block
        if self.replica_lag_reads > 0 and method in ("eth_call", "eth_getCode", "eth_blockNumber"):
            self.replica_lag_reads -= 1
            read_block -= 1
        if method == "eth_blockNumber":
            return {"result": hex(read_block)}
        if method == "eth_call":
            if self.malformed_eth_call:
                self.malformed_eth_call -= 1
                return {"jsonrpc": "2.0", "id": 1}
            call, blk = params
            data = call["data"]
            sender = "0x" + data[2 + 8 + 24: 2 + 8 + 64]
            b = read_block if blk == "latest" else int(blk, 16)
            return {"result": "0x" + format(self.chain_nonce(sender, b), "064x")}
        if method == "eth_getCode":
            b = read_block
            return {"result": "0x6080" if self.has_code(params[0], b) else "0x"}
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            topics = f["topics"]
            out = []
            for e in self.events:
                if e["kind"] != "uop" or not (lo <= e["block"] <= hi):
                    continue
                if topics[0] != userop.USER_OPERATION_EVENT_TOPIC:
                    continue
                if topics[1] and e["hash"].lower() not in [x.lower() for x in topics[1]]:
                    continue
                data = (
                    format(e["nonce"], "064x") + format(1 if e["success"] else 0, "064x")
                    + format(1, "064x") + format(1, "064x")
                )
                out.append({"topics": [topics[0], e["hash"]], "data": "0x" + data,
                            "blockNumber": hex(e["block"])})
            return {"result": out}
        raise AssertionError(f"unexpected public method {method}")

    # ---- derived facts for assertions ----
    def op_count_executed(self, sender: str) -> int:
        return sum(1 for e in self.events if e["kind"] == "uop" and e["sender"].lower() == sender.lower())

    def deploy_events(self, sender: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == "deploy" and e["sender"].lower() == sender.lower()]

    def sweep_refunds(self) -> None:
        """Relay PR #87 sweeper: refund reverts once per hash; drops only in
        ``pr87_drops`` mode. ``today`` never refunds accepted ops."""
        if self.ledger_mode == "today":
            return
        for op in self.ops.values():
            if op.owner != "ours" or op.billed == 0:
                continue
            if op.status == "included" and op.success is False:
                self.quota_used -= op.billed
                op.billed = 0
            elif self.ledger_mode == "pr87_drops" and op.status in ("dropped", "reorged"):
                self.quota_used -= op.billed
                op.billed = 0

    def expected_quota(self) -> int:
        """Units the relay legitimately holds, by op state and ledger mode."""
        total = getattr(self, "_gap_a_unrefunded", 0)
        for op in self.ops.values():
            if op.owner != "ours":
                continue
            total += op.billed
        return total

    def assert_invariants(self) -> None:
        # 1. each payload executes at most once
        seen = set()
        for p in self.executed_payloads:
            if p in seen:
                raise AssertionError(f"payload executed twice: {p[:16]!r}")
            seen.add(p)
        # 2. quota ledger agrees with op states for the active mode
        self.sweep_refunds()
        assert self.quota_used == self.expected_quota(), (
            f"quota {self.quota_used} != expected {self.expected_quota()}"
        )


def _calldata_for(payloads: list[bytes]) -> str:
    import totalreclaw_core
    if len(payloads) == 1:
        return "0x" + bytes(totalreclaw_core.encode_single_call(payloads[0])).hex()
    return "0x" + bytes(totalreclaw_core.encode_batch_call(payloads)).hex()


# ---------------------------------------------------------------------------
# httpx wrapper for the unit tier
# ---------------------------------------------------------------------------


class FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` inside ``userop`` (virtual clock)."""

    world: World

    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, json=None, headers=None, **kw):
        req = httpx.Request("POST", url)
        w = type(self).world
        try:
            if url.endswith("/v1/bundler"):
                body = w.relay_rpc(json["method"], json["params"])
            else:
                body = w.public_rpc(json["method"], json["params"])
        except HttpFault as f:
            return httpx.Response(f.status, text=f.text, request=req)
        except TransportFault:
            raise httpx.ConnectError("fake transport failure", request=req)
        body = {"jsonrpc": "2.0", "id": json.get("id", 1), **body} if "jsonrpc" not in body else body
        return httpx.Response(200, json=body, request=req)


class VirtualClock:
    """Installs the world's virtual clock into ``userop`` seams."""

    def __init__(self, monkeypatch, world: World) -> None:
        self.world = world
        FakeAsyncClient.world = world
        monkeypatch.setattr(userop.httpx, "AsyncClient", FakeAsyncClient)
        # raising=False: the same fake also drives the pre-fix baseline runs.
        monkeypatch.setattr(userop, "_now", world.now, raising=False)
        monkeypatch.setattr(userop, "_rand", lambda: 0.0, raising=False)

        async def _sleep(s: float) -> None:
            import asyncio
            await asyncio.sleep(0)
            world.advance(max(s, 0.0))

        monkeypatch.setattr(userop, "_sleep", _sleep, raising=False)
        for name in (
            "_reset_write_state_for_tests", "_reset_nonce_cache_for_tests",
            "_reset_deployed_cache_for_tests", "_reset_sender_locks_for_tests",
        ):
            getattr(userop, name, lambda: None)()
