#!/usr/bin/env python3
"""Staging E2E for the re-crystallize backfill write path (TotalReclaw #438).

Seeds a fresh THROWAWAY vault with a deliberately "mixed" Crystal + a set of
facts collapsed under ONE bad session_id (mimicking the pre-#441 Hermes
session-collapse bug), then runs the backfill (plan -> execute) against the LIVE
staging relay and verifies on the staging subgraph that:

  1. the old mixed Crystal is tombstoned (isActive=false / gone),
  2. the old collapsed facts are tombstoned,
  3. fresh re-keyed facts exist, carrying NEW (per-segment) session_ids,
  4. a fresh Crystal exists for each multi-fact corrected session.

STAGING ONLY — never production (hard project rule). The vault is a fresh
in-process random mnemonic; it is NEVER printed and never written to disk.

SECURITY — the mnemonic never leaves this process and is never printed:
  * Generated in-process (BIP-39, 128-bit) — never read from disk, never a CLI
    arg, never returned.
  * Every line of output goes through redact() (full phrase + each whitespace
    fragment -> [REDACTED]).
  * Library logging is suppressed to WARNING+ so no routine log can carry it.

Usage:
  PYTHONPATH=src python tests/e2e/recrystallize_staging_e2e.py             # real run
  PYTHONPATH=src python tests/e2e/recrystallize_staging_e2e.py --self-test # redaction check, no network
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback

STAGING_URL = "https://api-staging.totalreclaw.xyz"

# Two clearly-distinct topics so the semantic segmenter splits them, plus a big
# time gap so the 30-min gap rule also fires. All originally stored under ONE
# bad session_id "collapsed-giant" to mimic the collapse bug.
BAD_SESSION_ID = "collapsed-giant-0000"
INDEX_POLL_SECONDS = 240
INDEX_POLL_INTERVAL = 5

# (text, embedding-anchor-topic, created_at_offset_seconds)
TOPIC_A_FACTS = [
    "The Kia EV6 has an EPA range of 310 miles on the long-range battery.",
    "Kia EV6 DC fast charging goes from 10 to 80 percent in about 18 minutes.",
]
TOPIC_B_FACTS = [
    "The sourdough starter should be fed a 1:1:1 ratio of starter, flour, water.",
    "A cold overnight proof in the fridge deepens the sourdough's flavor.",
]


def _gen_mnemonic() -> str:
    """Generate a fresh 12-word BIP-39 mnemonic in-process (128-bit entropy)."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    _acct, mnemonic = Account.create_with_mnemonic()
    return mnemonic


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
    dummy = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    sample = f"traceback: mnemonic='{dummy}' owner-from '{dummy}' line 3"
    red = redact(sample, dummy)
    say(dummy, "[self-test] redacted sample ->", red)
    leaked = any(f in red for f in [dummy] + dummy.split())
    if leaked:
        say(dummy, "[self-test] FAIL: a phrase fragment survived redaction")
        return 1
    say(dummy, "[self-test] OK: no phrase fragment in redacted output (no network used)")
    return 0


async def _seed_vault(client, phrase) -> dict:
    """Seed the collapsed vault: 4 facts under BAD_SESSION_ID + 1 mixed Crystal.

    Returns a dict of seeded ids + the embeddings used (so the tool reuses them).
    """
    from totalreclaw.embedding import get_embedding
    from totalreclaw.recrystallize import METADATA_SUBTYPE_SESSION_CRYSTAL

    now = int(time.time())
    seeded_fact_ids: list[str] = []

    # Topic A facts at t=now-4000..3990; Topic B facts at t=now (big gap).
    plan_specs = (
        [(t, now - 4000 + i) for i, t in enumerate(TOPIC_A_FACTS)]
        + [(t, now + i) for i, t in enumerate(TOPIC_B_FACTS)]
    )
    for text, ts in plan_specs:
        emb = get_embedding(text)
        fid = await client.remember(
            text,
            embedding=emb,
            importance=0.6,
            source="e2e-seed",
            fact_type="claim",
            provenance="user",
            extra_metadata={"session_id": BAD_SESSION_ID},
        )
        seeded_fact_ids.append(fid)
    say(phrase, f"[e2e] seeded {len(seeded_fact_ids)} collapsed facts under session {BAD_SESSION_ID}")

    # One mixed Crystal summarizing the mash-up.
    crystal_id = await client.remember(
        "Mixed session — cars and baking",
        importance=0.8,
        source="e2e-seed",
        fact_type="summary",
        provenance="derived",
        extra_metadata={
            "subtype": METADATA_SUBTYPE_SESSION_CRYSTAL,
            "session_id": BAD_SESSION_ID,
            "session_title": "Mixed session — cars and baking",
        },
    )
    say(phrase, f"[e2e] seeded mixed Crystal {crystal_id}")
    return {"fact_ids": seeded_fact_ids, "crystal_id": crystal_id}


async def _wait_indexed(client, phrase, expect_count: int) -> bool:
    """Poll until the vault has >= expect_count active facts indexed."""
    from totalreclaw.recrystallize import fetch_and_decrypt_vault

    deadline = time.time() + INDEX_POLL_SECONDS
    while time.time() < deadline:
        try:
            vault = await fetch_and_decrypt_vault(client)
            if len(vault) >= expect_count:
                say(phrase, f"[e2e] {len(vault)} facts indexed")
                return True
        except Exception as e:
            say(phrase, "[e2e] index poll error:", type(e).__name__)
        await asyncio.sleep(INDEX_POLL_INTERVAL)
    return False


async def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    logging.disable(logging.WARNING)
    phrase = _gen_mnemonic()
    say(phrase, "[e2e] fresh throwaway vault (mnemonic withheld). target:", STAGING_URL)

    from totalreclaw import TotalReclaw
    from totalreclaw.recrystallize import (
        plan_recrystallize,
        execute_recrystallize,
        fetch_and_decrypt_vault,
    )

    client = TotalReclaw(
        recovery_phrase=phrase, server_url=STAGING_URL, is_test=True,
        suppress_welcome=True,
    )
    try:
        owner = await client.get_wallet_address()
        say(phrase, "[e2e] vault (smart account) owner:", owner)

        seeded = await _seed_vault(client, phrase)
        expected_total = len(seeded["fact_ids"]) + 1  # + crystal

        if not await _wait_indexed(client, phrase, expected_total):
            say(phrase, f"[e2e] FAIL: seeded facts not indexed within {INDEX_POLL_SECONDS}s")
            return 2

        # ── DRY RUN ──
        plan = await plan_recrystallize(client)
        say(phrase, "[e2e] --- dry-run plan ---")
        for line in plan.summary_lines():
            say(phrase, "  ", line)
        # Expect: F=4 atomic, C_old=1, and >=2 corrected sessions (A vs B).
        if plan.estimate.atomic_facts != 4 or plan.estimate.old_crystals != 1:
            say(phrase, "[e2e] FAIL: plan F/C_old mismatch",
                plan.estimate.atomic_facts, plan.estimate.old_crystals)
            return 3
        n_sessions = len(plan.corrected_sessions)
        fresh_sids = [s.fresh_session_id for s in plan.corrected_sessions]
        say(phrase, f"[e2e] plan corrected sessions={n_sessions} fresh_sids={fresh_sids}")
        if n_sessions < 2:
            say(phrase, "[e2e] FAIL: segmentation did not split the mixed vault")
            return 3

        # ── EXECUTE ──
        say(phrase, "[e2e] executing backfill (write-side-fix-confirmed, confirm) ...")
        cp = await execute_recrystallize(
            client, plan,
            write_side_fix_confirmed=True, confirm=True,
            progress=lambda m: say(phrase, "   >", m),
        )
        say(phrase, "[e2e] execute status:", cp.status)
        if cp.status != "completed":
            say(phrase, "[e2e] FAIL: execute did not complete")
            return 4

        # ── VERIFY on the subgraph (re-fetch the vault) ──
        # Poll for the tombstones + new facts to index.
        old_ids = set(seeded["fact_ids"]) | {seeded["crystal_id"]}
        fresh_sid_set = set(fresh_sids)
        deadline = time.time() + INDEX_POLL_SECONDS
        ok = False
        while time.time() < deadline:
            vault = await fetch_and_decrypt_vault(client)
            active_ids = {f.fact_id for f in vault}
            new_session_ids = {
                f.metadata.get("session_id")
                for f in vault
                if f.metadata.get("session_id")
            }
            new_crystals = [f for f in vault if f.is_crystal]
            old_gone = not (old_ids & active_ids)
            has_fresh_sids = bool(fresh_sid_set & new_session_ids)
            no_bad_sid = BAD_SESSION_ID not in new_session_ids
            has_new_crystal = len(new_crystals) >= 1
            if old_gone and has_fresh_sids and no_bad_sid and has_new_crystal:
                ok = True
                say(phrase, "[e2e] verify: old tombstoned + fresh session_ids + new Crystal present")
                say(phrase, f"[e2e]   active_count={len(vault)} "
                            f"new_session_ids={sorted(new_session_ids)} "
                            f"new_crystals={len(new_crystals)}")
                break
            await asyncio.sleep(INDEX_POLL_INTERVAL)

        if not ok:
            say(phrase, "[e2e] FAIL: post-backfill state not verified within timeout")
            return 5

        # Evidence summary for the report.
        vault = await fetch_and_decrypt_vault(client)
        say(phrase, "[e2e] === EVIDENCE ===")
        say(phrase, "[e2e] vault_owner:", owner)
        say(phrase, "[e2e] old_crystal_id_tombstoned:", seeded["crystal_id"])
        for f in vault:
            tag = "CRYSTAL" if f.is_crystal else "fact"
            say(phrase, f"[e2e]   {tag} id={f.fact_id} session_id={f.metadata.get('session_id')}")
        say(phrase, "[e2e] VERDICT: PASS")
        return 0
    except Exception as e:
        say(phrase, "[e2e] EXCEPTION:", type(e).__name__, "-", redact(str(e), phrase))
        say(phrase, "[e2e] TRACEBACK:", redact(traceback.format_exc(), phrase))
        return 6
    finally:
        # Best-effort cleanup: tombstone everything still active in the vault.
        try:
            from totalreclaw.recrystallize import fetch_and_decrypt_vault as _fetch
            vault = await _fetch(client)
            for f in vault:
                try:
                    await client.forget(f.fact_id)
                except Exception:
                    pass
            say(phrase, f"[e2e] cleanup: tombstoned {len(vault)} remaining facts")
        except Exception as e:
            say(phrase, "[e2e] cleanup error:", type(e).__name__)
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
