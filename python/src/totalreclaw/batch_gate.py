"""Boot-time chain-gate predicate for client-side ``executeBatch`` UserOp
submission. Spec #281 §9 Phase 1 (work-leaf imp-16).

Mirror of ``skill/plugin/batch-gate.ts``. The default behaviour is chain-
aware: Pro tier (Gnosis, chain 100) batches via ``executeBatch``; Free tier
(Base Sepolia, chain 84532) submits one UserOp per fact. The
``TOTALRECLAW_GNOSIS_BATCH_ENABLED`` env var is a hard kill-switch — setting
it to ``false`` disables batching on every chain regardless of tier, so ops
can revert to single-fact submission without a client redeploy if T-7
surfaces a billing regression on staging-Gnosis.

Read at module-import (boot) time only. Per-write reads would re-parse the
env on every fact submission — too expensive for the auto-extraction hot
path and pointless because the env doesn't change mid-process.

Sibling work-leaves wire ``should_batch_on_chain`` into
``agent/lifecycle.py`` and ``import_engine.py``; this module ships the
primitive only.
"""
from __future__ import annotations

import os

GNOSIS_CHAIN_ID = 100

_BATCH_ENABLED_AT_BOOT: bool = (
    os.environ.get("TOTALRECLAW_GNOSIS_BATCH_ENABLED", "").lower() != "false"
)


def should_batch_on_chain(chain_id: int) -> bool:
    """Return True if the client should submit ``executeBatch`` UserOps on
    ``chain_id``. False means fall back to single-fact UserOps.

    The decision uses the boot-time snapshot of ``TOTALRECLAW_GNOSIS_BATCH_ENABLED``.
    Mutating ``os.environ`` after import does not change the result; this is
    intentional (see module docstring).
    """
    if not _BATCH_ENABLED_AT_BOOT:
        return False
    return chain_id == GNOSIS_CHAIN_ID


def _read_gate_for_tests(env: dict[str, str], chain_id: int) -> bool:
    """Test-only helper: simulate the gate against an arbitrary env dict
    without re-importing the module. Mirrors the production logic exactly.
    """
    enabled = env.get("TOTALRECLAW_GNOSIS_BATCH_ENABLED", "").lower() != "false"
    if not enabled:
        return False
    return chain_id == GNOSIS_CHAIN_ID
