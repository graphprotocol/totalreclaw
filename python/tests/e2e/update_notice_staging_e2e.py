#!/usr/bin/env python3
"""Staging E2E for the Hermes update-available notice (public #442 / relay #32).

Proves the full path against the LIVE staging relay (which has
LATEST_STABLE_PYTHON=2.4.5 set):

  1. A real ``GET /v1/billing/status`` against staging carries
     ``features.latest_stable_python == "2.4.5"`` (relay half is live).
  2. With a FAKED older installed ``__version__`` (2.4.4), the client's real
     session-start hook path queues the update notice EXACTLY ONCE.
  3. A second hook call within the 24h window is SUPPRESSED (disk rate-limit).

SECURITY — the recovery phrase never leaves this process and is never printed.
Mirrors tests/e2e/entity_trapdoor_staging_e2e.py: phrase read from
QA_RECOVERY_PHRASE env or the macOS keychain, every output line redacted.

Usage:
  PYTHONPATH=src python tests/e2e/update_notice_staging_e2e.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

STAGING_URL = "https://api-staging.totalreclaw.xyz"
KEYCHAIN_SERVICE = "totalreclaw-qa-phrase"
KEYCHAIN_ACCOUNT = "totalreclaw"

FAKE_INSTALLED = "2.4.4"          # older than the staging LATEST_STABLE_PYTHON
EXPECTED_LATEST = "2.4.5"         # what staging is configured to advertise


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


async def main() -> int:
    logging.disable(logging.CRITICAL)  # no library log can carry the phrase
    phrase = load_phrase()

    def log(msg: str) -> None:
        print(redact(msg, phrase), flush=True)

    # Fake an OLDER installed version BEFORE importing client code that reads it.
    import totalreclaw
    real_version = totalreclaw.__version__
    totalreclaw.__version__ = FAKE_INSTALLED
    try:
        from totalreclaw import TotalReclaw
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes import hooks
        from totalreclaw import update_notice as un

        # Isolate the 24h rate-limit sentinel into a temp dir so the real
        # ~/.totalreclaw is never touched and the run is deterministic.
        tmp = Path(tempfile.mkdtemp(prefix="tr-update-e2e-"))
        un._STATE_DIR = tmp / ".totalreclaw"
        os.environ.pop("TOTALRECLAW_DISABLE_UPDATE_NOTICE", None)

        log(f"[e2e] installed(faked)={FAKE_INSTALLED} expecting latest={EXPECTED_LATEST}")

        # --- 1. Real staging billing carries latest_stable_python ---
        client = TotalReclaw(recovery_phrase=phrase, server_url=STAGING_URL, is_test=True)
        try:
            # Derive the Smart Account address + register so billing returns the
            # tier + features payload (an unregistered / address-less wallet gets
            # an error-shaped response with no `tier`).
            await client.get_wallet_address()
            await client._ensure_registered()
            status = await client._relay.get_billing_status()
        finally:
            await client._relay.close()

        feats = status.features
        latest = getattr(feats, "latest_stable_python", None) if feats else None
        log(f"[e2e] staging billing: tier={status.tier} latest_stable_python={latest}")
        if latest != EXPECTED_LATEST:
            log(f"[e2e] FAIL: expected features.latest_stable_python={EXPECTED_LATEST!r}, got {latest!r}. "
                f"Is LATEST_STABLE_PYTHON set on staging + deploy live?")
            return 1

        # Build the billing-cache dict shape the hook consumes.
        billing = {
            "tier": status.tier,
            "free_writes_used": status.free_writes_used,
            "free_writes_limit": status.free_writes_limit,
            "features": {"latest_stable_python": latest},
        }

        # --- 2. Hook queues the notice EXACTLY ONCE ---
        with _clean_env():
            state1 = PluginState()
        hooks._maybe_queue_update_notice(state1, billing)
        notice = state1.get_quota_warning()
        log(f"[e2e] first hook call -> notice={notice!r}")
        if not notice or f"{EXPECTED_LATEST} is available" not in notice:
            log("[e2e] FAIL: expected an update notice on the first call.")
            return 1
        if FAKE_INSTALLED not in notice or "update TotalReclaw" not in notice:
            log("[e2e] FAIL: notice text malformed (missing installed version or CTA).")
            return 1

        # --- 3. Second call within the window is SUPPRESSED ---
        with _clean_env():
            state2 = PluginState()
        hooks._maybe_queue_update_notice(state2, billing)
        second = state2.get_quota_warning()
        log(f"[e2e] second hook call (within 24h) -> notice={second!r}")
        if second is not None:
            log("[e2e] FAIL: notice fired twice within the 24h window (rate-limit broken).")
            return 1

        log("[e2e] PASS: staging advertises 2.4.5; notice fired once and was suppressed on repeat.")
        return 0
    finally:
        totalreclaw.__version__ = real_version


class _clean_env:
    """Build a PluginState with clean env + no on-disk credentials so the
    constructor doesn't try to auto-configure from a real vault."""
    def __enter__(self):
        from unittest.mock import patch
        self._p1 = patch.dict(os.environ, {}, clear=True)
        self._p2 = patch.object(Path, "exists", return_value=False)
        self._p1.start()
        self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
