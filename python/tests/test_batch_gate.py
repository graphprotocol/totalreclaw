"""Tests for ``totalreclaw.batch_gate`` — boot-time chain-gate predicate
(#281 §9 Phase 1, work-leaf imp-16).

Covers:

* env unset → Pro chain 100 batches, Free chain 84532 does not
* env=true → same as default
* env=false → kill-switch, both chains skip batching
* env=FALSE / False → case-insensitive kill-switch
* any other value → falls back to enabled (only literal ``false`` flips off)
* unknown chain id (e.g. 137 Polygon) → never batches
* live module export matches the predicate computed from the current process
  env (boot-time snapshot)

The live export is also exercised via a parametrised re-import so we cover
the module-import wiring path, not just the test-only helper.
"""
from __future__ import annotations

import importlib

import pytest

from totalreclaw import batch_gate


PRO_CHAIN = 100
FREE_CHAIN = 84532
UNKNOWN_CHAIN = 137


@pytest.mark.parametrize(
    "env_value,chain_id,expected",
    [
        # env unset → chain-aware defaults
        (None, PRO_CHAIN, True),
        (None, FREE_CHAIN, False),
        # env=true → same as default
        ("true", PRO_CHAIN, True),
        ("true", FREE_CHAIN, False),
        # env=false → kill-switch everywhere
        ("false", PRO_CHAIN, False),
        ("false", FREE_CHAIN, False),
        # case-insensitive kill-switch
        ("FALSE", PRO_CHAIN, False),
        ("False", PRO_CHAIN, False),
        # only literal "false" flips off; everything else stays enabled
        ("1", PRO_CHAIN, True),
        ("0", PRO_CHAIN, True),
        ("", PRO_CHAIN, True),
        # unknown chain never batches
        ("true", UNKNOWN_CHAIN, False),
        ("false", UNKNOWN_CHAIN, False),
    ],
)
def test_helper_matrix(env_value: str | None, chain_id: int, expected: bool) -> None:
    """Run the matrix via the test-only helper so each row is hermetic
    (no env mutation, no re-import)."""
    env: dict[str, str] = {}
    if env_value is not None:
        env["TOTALRECLAW_GNOSIS_BATCH_ENABLED"] = env_value
    assert batch_gate._read_gate_for_tests(env, chain_id) is expected


def test_live_export_reads_env_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """``should_batch_on_chain`` reads the env exactly once at module import.

    We verify by re-importing the module under a forced env value and
    checking that the resulting predicate matches the kill-switch state,
    NOT the value the env has after import.
    """
    monkeypatch.setenv("TOTALRECLAW_GNOSIS_BATCH_ENABLED", "false")
    reloaded = importlib.reload(batch_gate)
    try:
        # boot-time snapshot picked up `false`
        assert reloaded.should_batch_on_chain(PRO_CHAIN) is False
        assert reloaded.should_batch_on_chain(FREE_CHAIN) is False

        # mutating env post-import has no effect (per-write reads forbidden)
        monkeypatch.setenv("TOTALRECLAW_GNOSIS_BATCH_ENABLED", "true")
        assert reloaded.should_batch_on_chain(PRO_CHAIN) is False, (
            "post-import env mutation must not change the boot-time snapshot"
        )
    finally:
        # restore the module to whatever the surrounding test session expects
        monkeypatch.delenv("TOTALRECLAW_GNOSIS_BATCH_ENABLED", raising=False)
        importlib.reload(batch_gate)


def test_live_export_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env unset at import time, Pro batches and Free does not."""
    monkeypatch.delenv("TOTALRECLAW_GNOSIS_BATCH_ENABLED", raising=False)
    reloaded = importlib.reload(batch_gate)
    try:
        assert reloaded.should_batch_on_chain(PRO_CHAIN) is True
        assert reloaded.should_batch_on_chain(FREE_CHAIN) is False
        assert reloaded.should_batch_on_chain(UNKNOWN_CHAIN) is False
    finally:
        importlib.reload(batch_gate)
