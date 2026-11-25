#!/usr/bin/env python3
"""Staging E2E for the write-path fix (internal #646 AA25, #650 AA20).

Drives the real Python client against the STAGING relay and proves, on-chain,
that every write executed exactly once. STAGING ONLY: the relay URL is fixed
to https://api-staging.totalreclaw.xyz and the script refuses any other.

Recovery phrases (hard safety rail) - every account uses a THROWAWAY phrase:
  * generated in memory (`mnemonic` dev dependency) per account;
  * never printed, never logged, never written to disk, never in argv; child
    processes (S6, S8) receive it on STDIN through a pipe;
  * a redacting logging filter (full phrase and each word) is installed before
    any client exists, and every print goes through ``redact()``;
  * HOME, XDG_* and the TotalReclaw state directory point at a fresh temp
    directory. At the end it is scanned: the run FAILS if any 3 consecutive
    phrase words appear in any file there;
  * ``--self-test`` proves the redaction offline, with no network.

Scenarios (see the plan, section 6.3): S0 preflight, S1 15 sequential writes,
S1b 5 receipt-mode writes, S2 warm-up then 8 concurrent, S3/S4 first write then
an immediate second, S5 single then immediate batch(5), S6 three processes on one
undeployed account, S7 504 after the bundler accepted, S8 kill -9 after accept
then write again (deployed and undeployed), S9 soak (N fresh accounts, first
write then an immediate second).

On-chain verification per account (after the subgraph catches up):
  1 EntryPoint.getNonce == accepted sends reported by the client
  2 exactly one UserOperationEvent per reported hash, all success, none extra
  3 exactly one AccountDeployed, its userOpHash = the nonce-0 op
  4 subgraph facts(owner) ids == returned ids, distinct contentFp == fact count
  5 quota delta (free_writes_used) == fact count

Usage (from python/, with the dev venv):
  PYTHONPATH=src:. python tests/e2e/userop_write_path_staging_e2e.py --self-test
  PYTHONPATH=src:. python tests/e2e/userop_write_path_staging_e2e.py --scenarios S1,S2,S3,S4,S5,S6,S7,S8
  PYTHONPATH=src:. python tests/e2e/userop_write_path_staging_e2e.py --soak 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

STAGING_URL = "https://api-staging.totalreclaw.xyz"
INDEX_POLL_INTERVAL_S = 15
INDEX_POLL_MAX_S = 45 * 60
_PHRASES: set = set()          # every phrase this process (or its children) ever used


# ---------------------------------------------------------------------------
# redaction (installed before anything else)
# ---------------------------------------------------------------------------


def _frags() -> list:
    out = set()
    for p in _PHRASES:
        out.add(p)
        out.update(w for w in p.split() if len(w) >= 3)
    return sorted(out, key=len, reverse=True)


def redact(text: Any) -> str:
    out = str(text)
    for f in _frags():
        out = out.replace(f, "[REDACTED]")
    return out


def say(*parts: Any) -> None:
    print(redact(" ".join(str(p) for p in parts)), flush=True)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("totalreclaw.userop").setLevel(logging.INFO)


def isolate_env(label: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=f"tr-e2e-{label}-"))
    os.environ["HOME"] = str(tmp)
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        os.environ[k] = str(tmp / k.lower())
    os.environ["TOTALRECLAW_STATE_DIR"] = str(tmp / "state")
    os.environ["HERMES_HOME"] = str(tmp / "hermes")
    os.environ["TOTALRECLAW_NO_KEYCHAIN"] = "1"
    os.environ.pop("TOTALRECLAW_CREDENTIALS_PATH", None)
    os.environ.setdefault("TOTALRECLAW_SESSION_ID", f"d23-e2e-{int(time.time())}")
    return tmp


def scan_for_phrase(root: Path) -> list:
    """Files under ``root`` carrying 3 consecutive words of any used phrase."""
    hits = []
    triples = []
    for p in _PHRASES:
        w = p.split()
        triples += [" ".join(w[i:i + 3]) for i in range(len(w) - 2)]
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            data = f.read_text(errors="ignore")
        except Exception:
            continue
        if any(t in data for t in triples):
            hits.append(str(f.relative_to(root)))
    return hits


def self_test() -> int:
    from mnemonic import Mnemonic

    phrase = Mnemonic("english").generate(128)
    _PHRASES.add(phrase)
    words = phrase.split()
    sample = f"traceback mnemonic='{phrase}' words {words[0]} {words[1]} {words[2]} and a URL"
    red = redact(sample)
    leaked = [w for w in words if len(w) >= 3 and w in red]
    install_logging()
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "boom %s", (phrase,), None)
    RedactingFilter().filter(rec)
    leaked += [w for w in words if len(w) >= 3 and w in rec.getMessage()]
    tmp = isolate_env("selftest")
    (tmp / "state").mkdir()
    (tmp / "state" / "innocent.txt").write_text("nothing here")
    clean = scan_for_phrase(tmp) == []
    (tmp / "state" / "leak.txt").write_text(f"oops {words[3]} {words[4]} {words[5]}")
    caught = scan_for_phrase(tmp) == ["state/leak.txt"]
    shutil.rmtree(tmp, ignore_errors=True)
    ok = not leaked and clean and caught
    say("[self-test]", "OK: no phrase word survives redaction; temp-dir scan works" if ok else
        f"FAIL leaked={len(leaked)} clean={clean} caught={caught}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# accounts, spy, verification
# ---------------------------------------------------------------------------


class Account:
    def __init__(self, label: str, *, server_url: str = STAGING_URL) -> None:
        from mnemonic import Mnemonic
        from totalreclaw import TotalReclaw

        self.label = label
        self._phrase = Mnemonic("english").generate(128)
        _PHRASES.add(self._phrase)
        self.client = TotalReclaw(recovery_phrase=self._phrase, server_url=server_url, is_test=True)
        self.sa: str = ""
        self.chain_id: int = 100
        self.start_block: int = 0
        self.ids: list = []
        self.texts: list = []
        self.write_secs: list = []
        self.errors: list = []
        self.quota_before: Optional[int] = None
        self.child_sends: list = []     # sends reported by child processes

    @property
    def phrase(self) -> str:        # only for the stdin pipe of a child
        return self._phrase

    async def prepare(self) -> None:
        await self.client._ensure_address()
        # /v1/register allows 50 per IP per hour: wait out a 429, never skip.
        for attempt in range(70):
            try:
                await self.client._ensure_registered()
                break
            except Exception as e:  # noqa: BLE001
                if "429" not in str(e) or attempt == 69:
                    raise
                say(f"[{self.label}] register rate-limited; waiting 60 s")
                await asyncio.sleep(60)
        self.chain_id = await self.client._ensure_chain_id()
        self.sa = self.client._wallet_address
        from totalreclaw import userop
        async with userop.httpx.AsyncClient(timeout=30) as http:
            self.start_block = await block_number(http, userop._rpc_url_for_chain(self.chain_id))
        self.quota_before = await self.quota_used()

    async def quota_used(self) -> int:
        relay = self.client._relay
        http = await relay._get_http()
        resp = await http.get(
            f"{relay._relay_url}/v1/billing/status", headers=relay._base_headers(),
            params={"wallet_address": self.sa},
        )
        resp.raise_for_status()
        d = resp.json()
        return int(d.get("free_writes_used", 0)) + int(d.get("topup_writes_used", 0) or 0)

    def text(self, tag: str) -> str:
        t = f"d23 {self.label} {tag} {uuid.uuid4()}"
        self.texts.append(t)
        return t

    async def timed(self, coro, *, count_fact: bool = True):
        t0 = time.monotonic()
        try:
            res = await coro
            dt = time.monotonic() - t0
            self.write_secs.append(dt)
            if count_fact:
                if isinstance(res, list):
                    self.ids.extend(res)
                elif isinstance(res, str):
                    self.ids.append(res)
            return res
        except Exception as e:  # noqa: BLE001
            self.write_secs.append(time.monotonic() - t0)
            self.errors.append(f"{type(e).__name__}: {redact(e)[:240]}")
            return None


class Spy:
    """Observation only: counts sponsor / send calls and errors per sender and
    records every sent op (hash computed locally, as the client does)."""

    def __init__(self) -> None:
        from totalreclaw import userop

        self.userop = userop
        self.sponsor_errors: dict = {}      # sender -> [messages]
        self.sends: list = []               # dicts
        self.warnings: list = []
        self.calls: dict = {}               # relay JSON-RPC method -> count (all senders)
        self._orig = userop._relay_rpc
        userop._relay_rpc = self._wrapped
        self._h = _Collect(self.warnings)
        logging.getLogger("totalreclaw.userop").addHandler(self._h)

    async def _wrapped(self, http, relay_url, headers, method, params, rpc_id=1):
        sender = None
        self.calls[method] = self.calls.get(method, 0) + 1
        if method in ("pm_sponsorUserOperation", "eth_sendUserOperation") and params:
            sender = str(params[0].get("sender", "")).lower()
        try:
            res = await self._orig(http, relay_url, headers, method, params, rpc_id)
        except Exception as e:  # noqa: BLE001
            if method == "eth_sendUserOperation":
                self.sends.append(self._send_row(params[0], accepted=None, error=type(e).__name__))
            raise
        err = res.get("error") if isinstance(res, dict) else None
        if method == "pm_sponsorUserOperation" and err:
            self.sponsor_errors.setdefault(sender, []).append(str(err)[:240])
        if method == "eth_sendUserOperation":
            self.sends.append(self._send_row(params[0], accepted=not err, error=str(err)[:240] if err else None))
        return res

    def _send_row(self, op: dict, *, accepted, error) -> dict:
        h = "0x" + self.userop.compute_user_op_hash(op, self.userop.ENTRYPOINT_V07, self.chain_guess).hex()
        return {
            "sender": str(op["sender"]).lower(), "hash": h, "nonce": int(op["nonce"], 16),
            "factory": bool(op.get("factory")), "accepted": accepted, "error": error,
            "t": time.time(),
        }

    chain_guess = 100

    def for_sender(self, sa: str) -> dict:
        sa = sa.lower()
        errs = self.sponsor_errors.get(sa, [])
        sends = [s for s in self.sends if s["sender"] == sa]
        return {
            "aa25": sum("AA25" in m for m in errs) + sum("AA25" in (s["error"] or "") for s in sends),
            "aa20": sum("AA20" in m for m in errs) + sum("AA20" in (s["error"] or "") for s in sends),
            "sponsor_errors": len(errs),
            "sends": sends,
        }


class _Collect(logging.Handler):
    def __init__(self, sink: list) -> None:
        super().__init__(logging.WARNING)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record.getMessage()[:300])      # in memory only, never printed raw


async def block_number(http, rpc: str) -> int:
    r = await http.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    return int(r.json()["result"], 16)


def pct(xs: list, p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def summarize_times(xs: list) -> dict:
    return {"n": len(xs), "p50": round(pct(xs, 0.5), 1), "p95": round(pct(xs, 0.95), 1),
            "max": round(max(xs), 1) if xs else 0.0, "total": round(sum(xs), 1)}


# ---- on-chain verifier ----------------------------------------------------

def _topic(sig: str) -> str:
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode()).hex()


UOP_EVENT = _topic("UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)")
ACCOUNT_DEPLOYED = _topic("AccountDeployed(bytes32,address,address,address)")


async def _logs(http, rpc, topics, lo, hi) -> list:
    from totalreclaw.userop import ENTRYPOINT_V07
    out, start = [], lo
    while start <= hi:
        end = min(start + 4999, hi)
        r = await http.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [{
            "address": ENTRYPOINT_V07, "fromBlock": hex(start), "toBlock": hex(end), "topics": topics}]})
        body = r.json()
        if "result" not in body:
            raise RuntimeError(f"eth_getLogs failed: {str(body)[:200]}")
        out += body["result"]
        start = end + 1
    return out


async def verify_account(acct: Account, spy: Spy, *, expect_extra_sends: Optional[list] = None) -> dict:
    """Items 1-5 of the on-chain checks for one account. Waits (up to 45 min)
    for the subgraph, then reads once."""
    from totalreclaw import userop

    res: dict = {"label": acct.label, "sa": acct.sa, "checks": {}, "notes": []}
    sends = [s for s in spy.for_sender(acct.sa)["sends"]] + list(acct.child_sends)
    accepted = [s for s in sends if s["accepted"]]
    unknown = [s for s in sends if s["accepted"] is None]
    reported_hashes = {s["hash"].lower() for s in accepted}
    res["accepted_sends"] = len(accepted)
    res["unknown_sends"] = len(unknown)
    rpc = userop._rpc_url_for_chain(acct.chain_id)
    padded = "0x" + acct.sa.lower().replace("0x", "").zfill(64)
    async with userop.httpx.AsyncClient(timeout=30) as http:
        # wait for the subgraph to show every returned id (or give up)
        want = set(acct.ids)
        deadline = time.monotonic() + INDEX_POLL_MAX_S
        seen: dict = {}
        while True:
            try:
                data = await acct.client._relay.query_subgraph(
                    "query($o:String!){facts(where:{owner:$o},first:1000){id contentFp isActive}}",
                    {"o": acct.sa.lower()})
                rows = (data.get("data") or {}).get("facts") or []
                seen = {r["id"]: r for r in rows}
                if want <= set(seen):
                    break
            except Exception as e:  # noqa: BLE001
                res["notes"].append(f"subgraph error {type(e).__name__}")
            if time.monotonic() > deadline:
                res["notes"].append("subgraph wait timed out")
                break
            await asyncio.sleep(INDEX_POLL_INTERVAL_S)
        res["indexed_secs"] = round(time.monotonic() - (deadline - INDEX_POLL_MAX_S), 1)
        head = await block_number(http, rpc)
        nonce = await userop.get_nonce(http, acct.sa, acct.chain_id)
        events = await _logs(http, rpc, [UOP_EVENT, None, padded], max(acct.start_block - 2, 0), head)
        deploys = await _logs(http, rpc, [ACCOUNT_DEPLOYED, None, padded], max(acct.start_block - 2, 0), head)
    ev_hashes = [e["topics"][1].lower() for e in events]
    ev_success = [int(e["data"][2 + 64:2 + 128], 16) != 0 for e in events]
    c = res["checks"]
    c["1_nonce_equals_accepted"] = (nonce == len(accepted), f"nonce={nonce} accepted={len(accepted)}")
    extra = set(ev_hashes) - reported_hashes
    missing = reported_hashes - set(ev_hashes)
    c["2_one_event_per_hash_all_success"] = (
        not extra and not missing and len(ev_hashes) == len(set(ev_hashes)) and all(ev_success),
        f"events={len(ev_hashes)} reported={len(reported_hashes)} extra={len(extra)} missing={len(missing)} "
        f"reverted={ev_success.count(False)}")
    nonce0 = [s for s in accepted if s["nonce"] == 0]
    dep_ok = len(deploys) == 1 and (not nonce0 or deploys[0]["topics"][1].lower() == nonce0[0]["hash"].lower())
    c["3_one_deploy_in_nonce0_op"] = (dep_ok, f"deploys={len(deploys)}")
    fps = {seen[i]["contentFp"] for i in want if i in seen}
    c["4_subgraph_ids_and_fp"] = (
        want <= set(seen) and len(fps) == len(want) and all(seen[i]["isActive"] for i in want if i in seen),
        f"returned={len(want)} indexed={len(want & set(seen))} distinct_fp={len(fps)} owner_rows={len(seen)}")
    await asyncio.sleep(35)                 # PR #87 sweeper window; harmless otherwise
    used = await acct.quota_used()
    delta = used - (acct.quota_before or 0)
    c["5_quota_delta"] = (delta == len(seen), f"delta={delta} facts_on_subgraph={len(seen)} returned={len(want)}")
    res["ok"] = all(v[0] for v in c.values())
    return res


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


async def s1(spy: Spy, accts: list) -> dict:
    a = Account("S1"); accts.append(a); await a.prepare()
    for i in range(15):
        await a.timed(a.client.remember(a.text(f"seq{i}"), importance=7))
    f = spy.for_sender(a.sa)
    return {"scenario": "S1 15 sequential writes (accept + gate)", "ids": len(a.ids), "errors": a.errors,
            "aa25": f["aa25"], "aa20": f["aa20"], "accepted_sends": sum(1 for s in f["sends"] if s["accepted"]),
            "latency": summarize_times(a.write_secs)}


async def s1b(spy: Spy, accts: list, a: Optional[Account] = None) -> dict:
    a = a or Account("S1b")
    if a not in accts:
        accts.append(a)
    if not a.sa:
        await a.prepare()
    t = []
    for i in range(5):
        r = await a.timed(a.client.remember(a.text(f"tool{i}"), importance=7, confirm="receipt", profile="interactive"))
    f = spy.for_sender(a.sa)
    return {"scenario": "S1b 5 receipt-mode (tool path) writes", "ids": len(a.ids), "errors": a.errors,
            "aa25": f["aa25"], "aa20": f["aa20"], "latency": summarize_times(a.write_secs)}


async def s2(spy: Spy, accts: list) -> dict:
    a = Account("S2"); accts.append(a); await a.prepare()
    await a.timed(a.client.remember(a.text("warm"), importance=7))
    await asyncio.gather(*[a.timed(a.client.remember(a.text(f"par{i}"), importance=7)) for i in range(8)])
    f = spy.for_sender(a.sa)
    return {"scenario": "S2 warm-up then 8 concurrent", "ids": len(a.ids), "errors": a.errors,
            "aa25": f["aa25"], "aa20": f["aa20"], "latency": summarize_times(a.write_secs)}


async def s3s4(spy: Spy, accts: list, confirm: str = "accept") -> dict:
    a = Account("S3S4"); accts.append(a); await a.prepare()
    kw = {"confirm": confirm, "profile": "interactive"} if confirm == "receipt" else {}
    await a.timed(a.client.remember(a.text("first"), importance=7, **kw))
    first_has_factory = any(s["factory"] for s in spy.for_sender(a.sa)["sends"] if s["accepted"])
    await a.timed(a.client.remember(a.text("second-immediate"), importance=7, **kw))
    f = spy.for_sender(a.sa)
    return {"scenario": f"S3/S4 first write then an immediate second ({confirm})", "ids": len(a.ids),
            "errors": a.errors, "first_op_carried_factory": first_has_factory,
            "aa25": f["aa25"], "aa20": f["aa20"], "transient_warnings": [w for w in spy.warnings if "transient" in w][:3],
            "latency": summarize_times(a.write_secs)}


async def s5(spy: Spy, accts: list) -> dict:
    a = Account("S5"); accts.append(a); await a.prepare()
    await a.timed(a.client.remember(a.text("single"), importance=7))
    facts = [{"text": a.text(f"b{i}"), "importance": 7, "fact_type": "claim"} for i in range(5)]
    await a.timed(a.client.remember_batch(facts))
    f = spy.for_sender(a.sa)
    return {"scenario": "S5 single then immediate batch(5)", "ids": len(a.ids), "errors": a.errors,
            "aa25": f["aa25"], "aa20": f["aa20"],
            "ops": sum(1 for s in f["sends"] if s["accepted"]), "latency": summarize_times(a.write_secs)}


async def spawn_child(a: Account, texts: list, *, mode: str = "single", print_accepted: bool = False):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "PYTHONPATH", "LANG", "VIRTUAL_ENV")}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "--worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    payload = json.dumps({"phrase": a.phrase, "texts": texts, "mode": mode, "print_accepted": print_accepted})
    proc.stdin.write(payload.encode() + b"\n")
    await proc.stdin.drain()
    proc.stdin.close()
    return proc


async def finish_child(proc, a: Account, *, timeout: float = 900) -> dict:
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    result: dict = {}
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[7:])
    a.child_sends.extend(result.get("sends", []))
    a.ids.extend(result.get("ids", []))
    a.errors.extend(result.get("errors", []))
    a.write_secs.extend(result.get("secs", []))
    if proc.returncode not in (0, None) and not result:
        a.errors.append(f"child exited {proc.returncode}: {redact(err.decode(errors='replace'))[-200:]}")
    return result


async def s6(spy: Spy, accts: list) -> dict:
    a = Account("S6"); accts.append(a); await a.prepare()
    procs = [await spawn_child(a, [a.text(f"p{p}-{i}") for i in range(5)]) for p in range(3)]
    results = await asyncio.gather(*[finish_child(p, a) for p in procs])
    f = spy.for_sender(a.sa)
    child_aa = sum(len([e for e in r.get("sponsor_errors", [])]) for r in results)
    return {"scenario": "S6 three processes on one undeployed account (5 facts each)", "ids": len(a.ids),
            "errors": a.errors, "child_sponsor_errors": child_aa,
            "child_aa25": sum(r.get("aa25", 0) for r in results), "child_aa20": sum(r.get("aa20", 0) for r in results),
            "latency": summarize_times(a.write_secs)}


class _Proxy504(ThreadingHTTPServer):
    daemon_threads = True


def start_504_proxy(upstream: str, drop_after_send: int = 1):
    """Local relay in front of staging: forwards everything; for the first
    ``drop_after_send`` ``eth_sendUserOperation`` calls it forwards (Pimlico
    accepts) and then answers the client 504."""
    import httpx

    state = {"dropped": 0}
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def _do(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
            r = httpx.request(self.command, upstream + self.path, content=body, headers=hdrs, timeout=60)
            is_send = b"eth_sendUserOperation" in body
            with lock:
                drop = is_send and state["dropped"] < drop_after_send
                if drop:
                    state["dropped"] += 1
            if drop:
                self.send_response(504)
                self.end_headers()
                self.wfile.write(b"upstream timeout")
                return
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("content-length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)

        do_GET = do_POST = _do

        def log_message(self, *a):
            pass

    srv = _Proxy504(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


async def s7(spy: Spy, accts: list) -> dict:
    srv, st = start_504_proxy(STAGING_URL)
    try:
        a = Account("S7", server_url=f"http://127.0.0.1:{srv.server_address[1]}")
        accts.append(a)
        await a.prepare()
        await a.timed(a.client.remember(a.text("accepted-then-504"), importance=7))
        await a.timed(a.client.remember(a.text("after"), importance=7))
    finally:
        srv.shutdown()
        # the on-chain verifier reads the subgraph through this client later
        a.client._relay._relay_url = STAGING_URL
    f = spy.for_sender(a.sa)
    return {"scenario": "S7 bundler accepted, relay answered 504", "ids": len(a.ids), "errors": a.errors,
            "dropped_responses": st["dropped"], "accepted_sends": sum(1 for s in f["sends"] if s["accepted"]),
            "unknown_sends": sum(1 for s in f["sends"] if s["accepted"] is None),
            "aa25": f["aa25"], "aa20": f["aa20"], "latency": summarize_times(a.write_secs)}


async def s8(spy: Spy, accts: list, deployed: bool) -> dict:
    a = Account("S8g" if deployed else "S8h"); accts.append(a); await a.prepare()
    if deployed:
        await a.timed(a.client.remember(a.text("warm"), importance=7, confirm="receipt", profile="interactive"))
    p1 = await spawn_child(a, [a.text("killed-1"), a.text("killed-2")], print_accepted=True)
    killed_at = None
    t0 = time.monotonic()
    while True:
        line = await asyncio.wait_for(p1.stdout.readline(), timeout=300)
        if not line:
            break
        if line.startswith(b"ACCEPTED "):
            rows = line.decode().split()
            a.child_sends.append({"sender": a.sa.lower(), "hash": rows[1], "nonce": int(rows[2]),
                                  "factory": rows[3] == "True", "accepted": True, "error": None})
            os.kill(p1.pid, signal.SIGKILL)
            killed_at = time.monotonic() - t0
            break
    await p1.wait()
    p2 = await spawn_child(a, [a.text("after-restart-1"), a.text("after-restart-2")])
    res = await finish_child(p2, a)
    # a fact whose op was accepted before the kill is on-chain but its id was never returned: count it from the subgraph side
    return {"scenario": f"S8 kill -9 after accept then write again ({'deployed' if deployed else 'undeployed'})",
            "killed_after_s": round(killed_at or -1, 1), "ids": len(a.ids), "errors": a.errors,
            "child2_aa25": res.get("aa25", 0), "child2_aa20": res.get("aa20", 0),
            "note": "the killed child's first fact is on-chain but its id was never returned"}


async def s9(spy: Spy, accts: list, n: int) -> dict:
    async def one(i: int) -> Account:
        a = Account(f"S9-{i:02d}")
        accts.append(a)
        await a.prepare()
        await a.timed(a.client.remember(a.text("first"), importance=7))
        await a.timed(a.client.remember(a.text("second-immediate"), importance=7))
        return a

    sem = asyncio.Semaphore(5)

    async def guarded(i):
        async with sem:
            return await one(i)

    accounts = await asyncio.gather(*[guarded(i) for i in range(n)])
    aa20 = sum(spy.for_sender(a.sa)["aa20"] for a in accounts)
    aa25 = sum(spy.for_sender(a.sa)["aa25"] for a in accounts)
    errs = sum(len(a.errors) for a in accounts)
    ids = sum(len(a.ids) for a in accounts)
    trans = [w for w in spy.warnings if "transient" in w]
    return {"scenario": f"S9 soak: {n} fresh accounts, first write then an immediate second",
            "accounts": n, "writes_returned": ids, "tool_errors": errs, "aa20": aa20, "aa25": aa25,
            "transient_warnings": len(trans),
            "latency": summarize_times([x for a in accounts for x in a.write_secs])}


# ---------------------------------------------------------------------------
# worker (child process): phrase arrives on stdin only
# ---------------------------------------------------------------------------


async def worker_main() -> int:
    cfg = json.loads(sys.stdin.readline())
    _PHRASES.add(cfg["phrase"])
    install_logging()
    isolate_env("worker")
    from totalreclaw import TotalReclaw, userop

    spy = Spy()
    spy.chain_guess = 100
    printed = set()

    if cfg.get("print_accepted"):
        class P(logging.Handler):
            def emit(self, record):
                m = re.match(r"userop accepted hash=(\S+) nonce=(\d+) factory=(\w+)", record.getMessage())
                if m:
                    print(f"ACCEPTED {m.group(1)} {m.group(2)} {m.group(3)}", flush=True)
        logging.getLogger("totalreclaw.userop").addHandler(P())
    client = TotalReclaw(recovery_phrase=cfg["phrase"], server_url=STAGING_URL, is_test=True)
    ids, errors, secs = [], [], []
    for t in cfg["texts"]:
        t0 = time.monotonic()
        try:
            if cfg["mode"] == "batch":
                r = await client.remember_batch([{"text": t, "importance": 7, "fact_type": "claim"}])
                ids.extend(list(r))
            else:
                ids.append(await client.remember(t, importance=7))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {redact(e)[:240]}")
        secs.append(time.monotonic() - t0)
    sa = (client._wallet_address or "").lower()
    f = spy.for_sender(sa)
    print("RESULT " + json.dumps({"ids": ids, "errors": errors, "secs": secs, "sends": f["sends"],
                                  "sponsor_errors": spy.sponsor_errors.get(sa, []),
                                  "aa25": f["aa25"], "aa20": f["aa20"]}), flush=True)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def preflight() -> dict:
    import httpx
    out = {}
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(f"{STAGING_URL}/health")
        out["health"] = r.status_code
    a = Account("S0")
    await a.prepare()
    st = await a.client.status()
    out["tier"] = st.tier
    out["chain_id"] = a.chain_id
    out["data_edge"] = bool(getattr(a.client, "_data_edge_address", None))
    from totalreclaw import userop
    async with userop.httpx.AsyncClient(timeout=30) as http:
        out["block"] = await block_number(http, userop._rpc_url_for_chain(a.chain_id))
    return out


async def main_async(args) -> int:
    install_logging()
    tmp = isolate_env("main")
    from totalreclaw import userop
    if "api-staging.totalreclaw.xyz" not in STAGING_URL or "api.totalreclaw.xyz" == STAGING_URL.split("//")[1]:
        sys.exit("refusing: staging only")
    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "relay": STAGING_URL,
                    "scenarios": [], "verify": []}
    spy = Spy()
    accts: list = []
    wanted = [s.strip().upper() for s in (args.scenarios or "").split(",") if s.strip()]
    pre = await preflight()
    report["preflight"] = pre
    say("[S0]", json.dumps(pre))
    if pre.get("health") != 200 or pre.get("tier") != "pro":
        say("[S0] FAIL: preflight")
        return 2
    runners = {
        "S1": lambda: s1(spy, accts), "S1B": lambda: s1b(spy, accts), "S2": lambda: s2(spy, accts),
        "S3": lambda: s3s4(spy, accts, "accept"), "S4": lambda: s3s4(spy, accts, "receipt"),
        "S5": lambda: s5(spy, accts), "S6": lambda: s6(spy, accts), "S7": lambda: s7(spy, accts),
        "S8": None,
    }
    def delta(before: dict) -> dict:
        return {k: v - before.get(k, 0) for k, v in spy.calls.items() if v - before.get(k, 0)}

    for name in wanted:
        if name == "S8":
            for dep in (True, False):
                before = dict(spy.calls)
                r = await s8(spy, accts, dep)
                r["relay_calls"] = delta(before)
                report["scenarios"].append(r)
                say("[S8]", json.dumps(r))
            continue
        if name in runners:
            before = dict(spy.calls)
            r = await runners[name]()
            r["relay_calls"] = delta(before)
            report["scenarios"].append(r)
            say(f"[{name}]", json.dumps(r))
    if args.soak:
        before = dict(spy.calls)
        r = await s9(spy, accts, args.soak)
        r["relay_calls"] = delta(before)
        report["scenarios"].append(r)
        say("[S9]", json.dumps(r))
    report["relay_calls_total"] = dict(spy.calls)
    say("[relay calls total]", json.dumps(spy.calls))
    # on-chain verification (waits for the subgraph)
    say(f"[verify] {len(accts)} accounts; waiting for the subgraph (up to {INDEX_POLL_MAX_S // 60} min)")
    results = await asyncio.gather(*[verify_account(a, spy) for a in accts], return_exceptions=True)
    for a, v in zip(accts, results):
        if isinstance(v, Exception):
            v = {"label": a.label, "ok": False, "error": f"{type(v).__name__}: {redact(v)[:200]}"}
        report["verify"].append(v)
        status = "PASS" if v.get("ok") else "FAIL"
        say(f"[verify {a.label}] {status}", json.dumps({k: (vv[1] if isinstance(vv, tuple) else vv) for k, vv in (v.get("checks") or {}).items()}))
    leaks = scan_for_phrase(tmp)
    report["phrase_scan_hits"] = leaks
    report["ok"] = all(v.get("ok") for v in report["verify"]) and not leaks
    say("[phrase scan]", "no phrase words in any file under the temp HOME" if not leaks else f"FAIL: {leaks}")
    say("[VERDICT]", "PASS" if report["ok"] else "FAIL")
    if args.report:
        Path(args.report).write_text(redact(json.dumps(report, indent=1, default=str)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if report["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--soak", type=int, default=0)
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.worker:
        return asyncio.run(worker_main())
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
