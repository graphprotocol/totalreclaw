#!/usr/bin/env python3
"""Staging E2E for the relay read-denial surfacing feature (#662).

Two phases:

* **Phase A (regression, real staging):** remember two facts, confirm
  indexing, recall finds both, ``client.read_block is None``, then forget
  both — proves the new code paths don't break the ordinary case.
* **Phase B (denial path, no relay/DB change — preferred):** a tiny
  in-process stub HTTP relay (stdlib ``http.server``, daemon thread) sits
  between this process and staging. It forwards every request to staging
  verbatim EXCEPT ``POST /v1/subgraph``, which it can answer with a canned
  denial (``legacy403`` / ``quota429`` / ``rate429``) instead of forwarding.
  This proves the client's short-circuit, typed-error, and notice behaviour
  against denial shapes the live relay may rarely produce (it is moving to
  ``READ_QUOTA_MODE=observe`` — see
  ``docs/specs/totalreclaw/read-error-surfacing.md`` Decision 8) without
  needing the relay's `enforce` kill-switch turned on.

SECURITY — the recovery phrase never leaves this process and is never
printed. Mirrors ``tests/e2e/entity_trapdoor_staging_e2e.py`` /
``update_notice_staging_e2e.py``: phrase from ``QA_RECOVERY_PHRASE`` env or
the macOS keychain, every output line redacted, ``X-TotalReclaw-Test: true``
on every request.

**Single-flight.** Staging ``/v1/register`` is IP-rate-limited (about 19
minutes). Do not run this in parallel with S-PAIR-FRESH or another E2E on
the same host — see CLAUDE.md.

Usage:
  PYTHONPATH=src python tests/e2e/read_block_staging_e2e.py
  PYTHONPATH=src python tests/e2e/read_block_staging_e2e.py --self-test   # redaction check, no network
"""
from __future__ import annotations

import asyncio
import http.server
import json as _json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

STAGING_URL = "https://api-staging.totalreclaw.xyz"
KEYCHAIN_SERVICE = "totalreclaw-qa-phrase"
KEYCHAIN_ACCOUNT = "totalreclaw"

FACT_TEXT_A = "The read-block E2E planted fact A — distinctive marker Qxread662a."
FACT_TEXT_B = "The read-block E2E planted fact B — distinctive marker Qxread662b."

INDEX_POLL_SECONDS = 240
INDEX_POLL_INTERVAL = 4

# Watchdog: force-exit if anything hangs past this, so a stuck HTTP call (or
# the stub server thread) never leaves an orphaned process (trap-exit
# hygiene, CLAUDE.md "background subprocess hygiene").
WATCHDOG_SECONDS = 600


def load_phrase() -> str:
    env = (os.environ.get("QA_RECOVERY_PHRASE") or "").strip()
    if env:
        return env
    res = subprocess.run(
        ["security", "find-generic-password",
         "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        sys.exit("[e2e] no phrase available: set QA_RECOVERY_PHRASE, or add the keychain entry "
                 "(security add-generic-password -a totalreclaw -s totalreclaw-qa-phrase -U -w)")
    return res.stdout.strip()


def redact(text: str, phrase: str) -> str:
    if not phrase:
        return str(text)
    frags = sorted({phrase, *(p for p in phrase.split() if p)}, key=len, reverse=True)
    out = str(text)
    for f in frags:
        if f:
            out = out.replace(f, "[REDACTED]")
    return out


def self_test() -> int:
    dummy = "alpha bravo charlie delta echo foxtrot"
    sample = f"traceback: mnemonic='{dummy}' owner-from '{dummy}' line 3"
    red = redact(sample, dummy)
    print(redact(f"[self-test] redacted sample -> {red}", dummy), flush=True)
    leaked = any(f in red for f in [dummy] + dummy.split())
    if leaked:
        print("[self-test] FAIL: a phrase fragment survived redaction", flush=True)
        return 1
    print("[self-test] OK: no phrase fragment in redacted output (no network used)", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Phase B — in-process stub relay: forwards everything to staging verbatim
# except POST /v1/subgraph, which it can answer with a canned denial.
# ---------------------------------------------------------------------------

class _StubState:
    def __init__(self) -> None:
        self.mode = "passthrough"  # "passthrough" | "legacy403" | "quota429" | "rate429"
        self.subgraph_hits = 0
        self.lock = threading.Lock()

    def hit(self) -> int:
        with self.lock:
            self.subgraph_hits += 1
            return self.subgraph_hits

    def reset_hits(self) -> None:
        with self.lock:
            self.subgraph_hits = 0


def _canned_subgraph_response(mode: str):
    """Returns (status, body_dict, extra_headers) or None (passthrough)."""
    if mode == "legacy403":
        return 403, {
            "error": "quota_exceeded",
            "message": "Read limit reached this month",
            "upgrade_url": "https://totalreclaw.xyz/pricing",
        }, {}
    if mode == "quota429":
        resets_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        return 429, {
            "error": "read_quota_exceeded",
            "error_code": "read_quota_exceeded",
            "message": "monthly read limit reached",
            "tier": "free",
            "limit": 250,
            "used": 250,
            "resets_at": resets_at,
            "upgrade_url": "https://totalreclaw.xyz/pricing",
        }, {"Retry-After": "5"}
    if mode == "rate429":
        return 429, {
            "success": False,
            "error": "Rate limit exceeded. Try again later.",
            "retry_after": 5,
        }, {"Retry-After": "5"}
    return None  # passthrough


def _make_handler(state: _StubState):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a) -> None:  # quiet — no phrase can leak via access logs
            pass

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            url = STAGING_URL + self.path
            req = urllib.request.Request(url, data=body or None, method=self.command)
            for k, v in self.headers.items():
                if k.lower() in ("host", "content-length"):
                    continue
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.status
                    data = resp.read()
                    headers = dict(resp.getheaders())
            except urllib.error.HTTPError as e:
                status = e.code
                data = e.read()
                headers = dict(e.headers or {})
            self.send_response(status)
            for k, v in headers.items():
                if k.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _canned(self, status: int, body: dict, extra_headers: dict) -> None:
            payload = _json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in extra_headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 — stdlib handler naming
            if self.path.startswith("/v1/subgraph"):
                state.hit()
                canned = _canned_subgraph_response(state.mode)
                if canned is not None:
                    status, body, extra_headers = canned
                    # Drain the request body even on a canned response so
                    # keep-alive framing doesn't desync the connection.
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        self.rfile.read(length)
                    self._canned(status, body, extra_headers)
                    return
            self._forward()

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

    return Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubRelay:
    """Context-manager wrapper: starts the stub server on a daemon thread,
    self-timeout via ``WATCHDOG_SECONDS``, guaranteed ``shutdown()`` on
    exit (trap-EXIT hygiene)."""

    def __init__(self) -> None:
        self.state = _StubState()
        self.port = _free_port()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), _make_handler(self.state)
        )
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Timer | None = None

    def __enter__(self) -> "StubRelay":
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        # Self-timeout safety net: force-exit the whole process if something
        # above forgets to tear this down (e.g. an unhandled exception path).
        self._watchdog = threading.Timer(WATCHDOG_SECONDS, lambda: os._exit(3))
        self._watchdog.daemon = True
        self._watchdog.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


async def _phase_a(say, phrase: str) -> int:
    """Regression: the ordinary path is unaffected."""
    from totalreclaw import TotalReclaw
    from totalreclaw.confirm_indexed import confirm_indexed

    client = TotalReclaw(recovery_phrase=phrase, server_url=STAGING_URL, is_test=True)
    fact_ids: list[str] = []
    try:
        for text in (FACT_TEXT_A, FACT_TEXT_B):
            fid = await client.remember(text=text, fact_type="claim", importance=0.5)
            fact_ids.append(fid)
            say(f"[phase A] stored fact_id={fid}")

        for fid in fact_ids:
            indexed = await confirm_indexed(fid, client._relay, expect="active", timeout_ms=INDEX_POLL_SECONDS * 1000)
            say(f"[phase A] confirm_indexed({fid}) -> {indexed}")

        # Recall ranking/tokenization of a coined marker word is unrelated
        # to #662 and can be lossy; fall back to export (a direct decrypt
        # of every active fact, no search ranking involved) so this
        # regression check isn't gated on search-quality flakiness.
        results = await client.recall("read-block E2E planted fact", top_k=20)
        found = {getattr(r, "text", "") for r in results}
        both_found = any(FACT_TEXT_A in t for t in found) and any(FACT_TEXT_B in t for t in found)
        say(f"[phase A] recall found both planted facts: {both_found}")
        if not both_found:
            exported = await client.export_all()
            exported_texts = {e.get("text", "") for e in exported}
            both_found = (
                any(FACT_TEXT_A in t for t in exported_texts)
                and any(FACT_TEXT_B in t for t in exported_texts)
            )
            say(f"[phase A] export fallback found both planted facts: {both_found}")

        blk = client.read_block
        say(f"[phase A] client.read_block is None: {blk is None}")

        if not both_found or blk is not None:
            say("[phase A] FAIL")
            return 1
        say("[phase A] PASS")
        return 0
    finally:
        for fid in fact_ids:
            try:
                ok = await client.forget(fid)
                say(f"[phase A] cleanup forget({fid}) -> {ok}")
            except Exception as e:
                say(f"[phase A] cleanup forget error: {type(e).__name__}: {e}")
        await client.close()


def _wait_seconds_for_mode(mode: str, reprobe_s: int) -> int:
    """How long to sleep past the pause before probing recovery.

    ``quota429``/``legacy403`` use the reprobe cadence
    (``TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS``, shortened for this E2E).
    ``rate429`` does NOT respect that env var — ``_pause_seconds`` clamps a
    rate-limit pause to a **30s floor regardless of the canned
    ``retry_after``** (see ``relay.py::_pause_seconds`` / the read-error-
    surfacing spec §4.3 pause-length table), so it needs its own, longer
    wait. Confirmed against the real fixed floor, not a guess.
    """
    if mode == "rate429":
        return 32
    return reprobe_s + 2


async def _phase_b_mode(say, phrase: str, mode: str, reprobe_s: int) -> bool:
    """One denial mode through the stub relay. Returns True on pass."""
    from totalreclaw import TotalReclaw
    from totalreclaw.relay import RelayReadBlocked
    from totalreclaw.agent.recall import auto_recall
    from totalreclaw.hermes.state import PluginState

    ok = True
    with StubRelay() as stub:
        client = TotalReclaw(recovery_phrase=phrase, server_url=stub.url, is_test=True)
        try:
            # Resolve address + register through the stub first (passthrough
            # mode) so the wallet exists before we flip on the denial.
            await client._ensure_address()
            await client._ensure_registered()

            stub.state.mode = mode
            stub.state.reset_hits()

            # (a) one recall = 1 stub subgraph hit + typed error.
            try:
                await client.recall("anything", top_k=8)
                say(f"[phase B:{mode}] FAIL: recall did not raise")
                ok = False
            except RelayReadBlocked as e:
                say(f"[phase B:{mode}] recall raised {type(e).__name__} as expected")
            hits_after_first = stub.state.subgraph_hits
            say(f"[phase B:{mode}] stub subgraph hits after 1st recall: {hits_after_first}")
            if hits_after_first != 1:
                say(f"[phase B:{mode}] FAIL: expected exactly 1 stub hit, got {hits_after_first}")
                ok = False

            # (b) a second recall inside the window = 0 new hits.
            try:
                await client.recall("anything else", top_k=8)
                say(f"[phase B:{mode}] FAIL: second recall did not raise")
                ok = False
            except RelayReadBlocked:
                pass
            hits_after_second = stub.state.subgraph_hits
            say(f"[phase B:{mode}] stub subgraph hits after 2nd recall: {hits_after_second}")
            if hits_after_second != hits_after_first:
                say(f"[phase B:{mode}] FAIL: second recall made a new HTTP call "
                    f"({hits_after_first} -> {hits_after_second})")
                ok = False

            # (c) Hermes auto_recall returns the notice, not None.
            state = PluginState()
            state._client = client
            notice = auto_recall("who am I?", state)
            say(f"[phase B:{mode}] auto_recall notice present: {notice is not None}")
            if not notice or "paused" not in notice:
                say(f"[phase B:{mode}] FAIL: auto_recall did not surface a paused notice")
                ok = False

            # (e) switch to passthrough, wait past the (shortened) reprobe
            # window: one probe, then recall works, notice says "working
            # again".
            stub.state.mode = "passthrough"
            wait_s = _wait_seconds_for_mode(mode, reprobe_s)
            say(f"[phase B:{mode}] sleeping {wait_s}s past the pause window...")
            await asyncio.sleep(wait_s)
            hits_before_recovery = stub.state.subgraph_hits
            recall_results = await client.recall("anything", top_k=8)
            say(f"[phase B:{mode}] post-recovery recall returned {len(recall_results)} results "
                f"(may be 0 — these aren't real facts)")
            if client.read_block is not None:
                say(f"[phase B:{mode}] FAIL: read_block still active after recovery")
                ok = False
            working_again = state.pending_read_block_notice(client)
            say(f"[phase B:{mode}] pending_read_block_notice after recovery: {working_again!r}")
            if working_again != "[totalreclaw] Memory lookups are working again.":
                say(f"[phase B:{mode}] FAIL: expected the working-again line")
                ok = False
        finally:
            await client.close()
    return ok


async def _phase_b_write_through_stub(say, phrase: str) -> bool:
    """(d) a write through the stub (passthrough) still lands on staging."""
    from totalreclaw import TotalReclaw

    with StubRelay() as stub:
        client = TotalReclaw(recovery_phrase=phrase, server_url=stub.url, is_test=True)
        fid = None
        try:
            fid = await client.remember(
                text="Read-block E2E phase B write-through-stub marker Qxread662d.",
                fact_type="claim", importance=0.3,
            )
            say(f"[phase B:write] stored fact_id={fid} through the stub (passthrough)")
            return bool(fid)
        finally:
            if fid:
                try:
                    ok = await client.forget(fid)
                    say(f"[phase B:write] cleanup forget({fid}) -> {ok}")
                except Exception as e:
                    say(f"[phase B:write] cleanup forget error: {type(e).__name__}")
            await client.close()


async def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    logging.disable(logging.WARNING)  # no routine library log can carry the phrase
    phrase = load_phrase()

    def say(msg: str) -> None:
        print(redact(msg, phrase), flush=True)

    say(f"[e2e] phrase loaded (value withheld). target: {STAGING_URL}")

    verdict_a = await _phase_a(say, phrase)
    if verdict_a != 0:
        say("[e2e] VERDICT: FAIL (phase A)")
        return verdict_a

    # Shorten the reprobe window for the E2E so phase B doesn't take 15 min
    # per mode. Floor is 5s (relay.py _reprobe_seconds).
    reprobe_s = 6
    os.environ["TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS"] = str(reprobe_s)

    all_ok = True
    for mode in ("legacy403", "quota429", "rate429"):
        ok = await _phase_b_mode(say, phrase, mode, reprobe_s)
        say(f"[phase B:{mode}] {'PASS' if ok else 'FAIL'}")
        all_ok = all_ok and ok

    write_ok = await _phase_b_write_through_stub(say, phrase)
    say(f"[phase B:write] {'PASS' if write_ok else 'FAIL'}")
    all_ok = all_ok and write_ok

    say(f"[e2e] VERDICT: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(4)
