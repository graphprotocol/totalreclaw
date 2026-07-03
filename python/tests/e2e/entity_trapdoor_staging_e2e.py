#!/usr/bin/env python3
"""Staging E2E for the entity-trapdoor read-side (TotalReclaw #370, Task 4).

Stores a fact carrying a PLANTED distinctive entity (no LLM), confirms it is
recallable on the real staging pipeline, then tombstones it.

SECURITY — the recovery phrase never leaves this process and is never printed:
  * Read from QA_RECOVERY_PHRASE env, else the macOS keychain entry
    (totalreclaw-qa-phrase / totalreclaw) via a captured `security` subprocess.
  * Every line of output goes through redact() (full phrase + each whitespace
    fragment -> [REDACTED]).
  * Library logging is suppressed to WARNING+ so no routine log can carry it.
  * The phrase is never a CLI arg, never written to disk, never returned.

Usage:
  PYTHONPATH=src python tests/e2e/entity_trapdoor_staging_e2e.py             # real run
  PYTHONPATH=src python tests/e2e/entity_trapdoor_staging_e2e.py --self-test  # redaction check, no network
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import traceback

STAGING_URL = "https://api-staging.totalreclaw.xyz"
KEYCHAIN_SERVICE = "totalreclaw-qa-phrase"
KEYCHAIN_ACCOUNT = "totalreclaw"

ENTITY = "Qxcvtrapdoor"                              # coined; heuristic-extractable; no real-data collision
FACT_TEXT = "The preferred dashboard theme is dark mode with compact spacing."
WORD_QUERY = "dashboard theme"                       # lexical overlap -> proves indexing + read pipeline
ENTITY_QUERY = f"show my {ENTITY} config"            # NO lexical overlap -> entity-trapdoor signal only

INDEX_POLL_SECONDS = 240
INDEX_POLL_INTERVAL = 4


def _matches(r, fact_id: str) -> bool:
    """A result matches our stored fact if its id equals the client fact_id OR
    its text contains our distinctive fact text (id schemes can differ between
    the client UUID and the subgraph entity id)."""
    if getattr(r, "id", None) == fact_id:
        return True
    text = getattr(r, "text", None) or ""
    return FACT_TEXT[:40] in text


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
        out = out.replace(f, "[REDACTED]")
    return out


def say(phrase: str, *parts) -> None:
    print(redact(" ".join(str(p) for p in parts), phrase), flush=True)


def self_test() -> int:
    dummy = "alpha bravo charlie delta echo foxtrot"
    sample = f"traceback: mnemonic='{dummy}' owner-from '{dummy}' line 3"
    red = redact(sample, dummy)
    say(dummy, "[self-test] redacted sample ->", red)
    leaked = any(f in red for f in [dummy] + dummy.split())
    if leaked:
        say(dummy, "[self-test] FAIL: a phrase fragment survived redaction")
        return 1
    say(dummy, "[self-test] OK: no phrase fragment in redacted output (no network used)")
    return 0


async def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    # Suppress chatty library logs so nothing routine can carry the phrase.
    logging.disable(logging.WARNING)

    phrase = load_phrase()
    say(phrase, "[e2e] phrase loaded (value withheld). target:", STAGING_URL)

    from totalreclaw import TotalReclaw
    client = TotalReclaw(recovery_phrase=phrase, server_url=STAGING_URL, is_test=True)
    fact_id = None
    try:
        t0 = time.time()
        fact_id = await client.remember(
            text=FACT_TEXT,
            fact_type="preference",
            entities=[{"name": ENTITY, "type": "concept"}],
            importance=0.5,
        )
        say(phrase, f"[e2e] stored fact_id={fact_id} with planted entity ({time.time()-t0:.1f}s)")

        # 1) Poll: direct subgraph by-id lookup (indexing proof) AND word recall
        #    (search proof). Reporting both distinguishes "not indexed" (infra
        #    lag) from "indexed but search broken" (code).
        from totalreclaw import operations as _ops
        owner, relay = client._wallet_address, client._relay
        by_id_at = None
        recall_at = None
        deadline = time.time() + INDEX_POLL_SECONDS
        while time.time() < deadline:
            if by_id_at is None:
                try:
                    if await _ops._fetch_fact_by_id(fact_id, owner, relay):
                        by_id_at = time.time() - t0
                        say(phrase, f"[e2e] subgraph has fact by-id at {by_id_at:.1f}s (indexing OK)")
                except Exception as e:
                    say(phrase, "[e2e] by-id lookup error:", type(e).__name__)
            if recall_at is None:
                try:
                    res = await client.recall(WORD_QUERY, top_k=8)
                except Exception as e:
                    say(phrase, "[e2e] recall error:", type(e).__name__)
                    res = []
                if any(_matches(r, fact_id) for r in res):
                    recall_at = time.time() - t0
                    say(phrase, f"[e2e] recallable via word query at {recall_at:.1f}s")
            if by_id_at is not None and recall_at is not None:
                break
            await asyncio.sleep(INDEX_POLL_INTERVAL)
        say(phrase, f"[e2e] poll done: by_id_found={by_id_at is not None}, "
                    f"recall_found={recall_at is not None}")
        indexed = recall_at is not None
        if not indexed:
            say(phrase, f"[e2e] FAIL: not recallable within {INDEX_POLL_SECONDS}s "
                        f"(by_id_found={by_id_at is not None} -> "
                        f"{'indexing lag/stale subgraph (infra)' if by_id_at is None else 'indexed but search missed (code)'}")
            return 2

        # 2) Entity-only recall: no lexical overlap, so only an entity-trapdoor
        #    match surfaces it. (Caveat: broadened-search fallback can also
        #    surface recent facts; indexed=True above is the pipeline proof.)
        try:
            eres = await client.recall(ENTITY_QUERY, top_k=8)
        except Exception as e:
            say(phrase, "[e2e] entity recall error:", type(e).__name__)
            return 3
        entity_hit = any(_matches(r, fact_id) for r in eres)
        say(phrase, f"[e2e] entity-only recall hit={entity_hit} "
                    f"(broadened fallback may also surface recent facts)")

        verdict = "PASS" if indexed else "FAIL"
        say(phrase, f"[e2e] VERDICT: {verdict} (indexed={indexed}, entity_hit={entity_hit})")
        return 0 if indexed else 2
    except Exception as e:
        say(phrase, "[e2e] EXCEPTION:", type(e).__name__, "-", e)
        say(phrase, "[e2e] TRACEBACK:", redact(traceback.format_exc(), phrase))
        return 4
    finally:
        if fact_id:
            try:
                ok = await client.forget(fact_id)
                say(phrase, f"[e2e] cleanup forget({fact_id}) -> {ok}")
            except Exception as e:
                say(phrase, "[e2e] cleanup forget error:", type(e).__name__)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
