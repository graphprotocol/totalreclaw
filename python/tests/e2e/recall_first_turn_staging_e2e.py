#!/usr/bin/env python3
"""Staging E2E for first-turn auto-recall injection (internal#486 follow-up).

Proves the proactive-recall contract against the LIVE staging relay:

  1. Write one uniquely-tokened fact through the real client (`remember`).
  2. Poll explicit `recall` until the staging subgraph has indexed it.
  3. Build a fresh, configured ``PluginState`` and drive the REAL
     ``pre_llm_call`` hook with ``is_first_turn=True`` — assert the injected
     context contains the fact (the "proactively fetch memories at session
     start" behaviour users rely on).
  4. Sanity-check the fail-loud path: with recall forced to return empty on
     a vault that HAS writes, the hook's shared recall entry point injects
     the ``[totalreclaw]`` memory-health warning instead of staying silent.
  5. Tombstone the test fact (cleanup).

SECURITY — the recovery phrase never leaves this process and is never
printed. Mirrors tests/e2e/update_notice_staging_e2e.py: phrase read from
QA_RECOVERY_PHRASE env or the macOS keychain, every output line redacted.

Usage:
  PYTHONPATH=src python tests/e2e/recall_first_turn_staging_e2e.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from unittest.mock import patch

STAGING_URL = "https://api-staging.totalreclaw.xyz"
KEYCHAIN_SERVICE = "totalreclaw-qa-phrase"
KEYCHAIN_ACCOUNT = "totalreclaw"

INDEX_POLL_SECONDS = 10
INDEX_POLL_ATTEMPTS = 12  # up to ~2 min for subgraph indexing


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
        sys.exit("[e2e] no phrase: set QA_RECOVERY_PHRASE or add the keychain entry.")
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


class _clean_env:
    """Build a PluginState with clean env + no on-disk credentials so the
    constructor doesn't auto-configure from a real vault."""

    def __enter__(self):
        self._p1 = patch.dict(os.environ, {}, clear=True)
        self._p2 = patch.object(Path, "exists", return_value=False)
        self._p1.start()
        self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()
        return False


async def main() -> int:
    logging.disable(logging.CRITICAL)  # no library log can carry the phrase
    phrase = load_phrase()

    def log(msg: str) -> None:
        print(redact(msg, phrase), flush=True)

    from totalreclaw import TotalReclaw
    from totalreclaw import update_notice as un
    from totalreclaw.hermes import hooks
    from totalreclaw.hermes.state import PluginState

    # Isolate the 24h notice sentinels so the fail-loud step is deterministic
    # and the real ~/.totalreclaw is never touched.
    tmp = Path(tempfile.mkdtemp(prefix="tr-recall-e2e-"))
    un._STATE_DIR = tmp / ".totalreclaw"
    os.environ.pop("TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE", None)

    token = f"zephyrite-{uuid.uuid4().hex[:8]}"
    fact_text = f"The user's favourite test mineral is {token}."
    fact_id = None
    client = TotalReclaw(recovery_phrase=phrase, server_url=STAGING_URL, is_test=True)
    try:
        # --- 1. Write a uniquely-tokened fact ---
        fact_id = await client.remember(fact_text, fact_type="claim")
        log(f"[e2e] stored test fact (id={fact_id}) token={token}")

        # --- 2. Poll explicit recall until indexed ---
        found = False
        for attempt in range(INDEX_POLL_ATTEMPTS):
            results = await client.recall(f"favourite test mineral {token}", top_k=8)
            if any(token in str(getattr(r, "text", "")) for r in results):
                found = True
                log(f"[e2e] fact indexed + recallable after ~{attempt * INDEX_POLL_SECONDS}s")
                break
            await asyncio.sleep(INDEX_POLL_SECONDS)
        if not found:
            log("[e2e] FAIL: fact never became recallable (indexing or read path broken).")
            return 1

        # --- 3. REAL first-turn hook injects the memory ---
        with _clean_env():
            state = PluginState()
        # Attach the already-built staging client directly — avoids
        # ``state.configure`` writing credentials.json on the QA host.
        state._client = client
        result = hooks.pre_llm_call(
            state,
            is_first_turn=True,
            user_message=f"what is my favourite test mineral {token}?",
        )
        ctx = (result or {}).get("context", "")
        if token not in ctx:
            log(f"[e2e] FAIL: first-turn context missing the fact. context={ctx[:400]!r}")
            return 1
        log("[e2e] first-turn auto-recall injected the fact — proactive recall works.")

        # --- 4. Fail-loud path: empty recall on a non-empty vault warns ---
        state.set_billing_cache({"free_writes_used": 42})
        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "empty")
        ):
            notice = hooks.recall_for_query(state, "anything")
        if not notice or "[totalreclaw]" not in notice:
            log(f"[e2e] FAIL: expected memory-health warning, got {notice!r}")
            return 1
        log("[e2e] fail-loud notice fired on empty-recall-with-writes.")

        log("[e2e] PASS: write -> index -> first-turn injection -> fail-loud all green.")
        return 0
    finally:
        try:
            if fact_id:
                await client.forget(fact_id)
                log(f"[e2e] cleanup: tombstoned {fact_id}")
        except Exception as exc:
            log(f"[e2e] cleanup warning: forget failed: {exc}")
        await client._relay.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
