"""
Recall-health notice — turn silent auto-recall death into an agent-visible signal.

Born from internal#486: `RelayClient.query_subgraph` dropped the
`X-Wallet-Address` header, every Pro-tier recall silently returned
`{"count": 0}` with HTTP 200, and the auto-recall hook swallowed the empty
result with a log-only warning. The user's agent looked like it "didn't even
try" to remember — for days — because nothing in the conversation surface ever
said recall was broken.

This module builds the one-line diagnostic context the recall drivers
(`hooks.recall_for_query`, used by both `pre_llm_call` and
`MemoryProvider.prefetch`) inject when auto-recall either raises or returns
zero results despite the billing cache showing a non-empty vault. The line
instructs the agent to TELL THE USER, so a broken read path surfaces on the
first affected turn instead of after a support round-trip.

Gates, in order:
1. Kill-switch env ``TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE``.
2. Status must be actionable: ``"error"`` always is; ``"empty"`` only when the
   vault provably has stored memories (``writes_used > 0`` from the billing
   cache) — an empty vault legitimately recalls nothing.
3. 24h rate-limit via the shared timestamp-sentinel helpers in
   :mod:`totalreclaw.update_notice` (same ~/.totalreclaw/ convention), so a
   persistent outage nags once a day, not once a turn.

Pure + framework-agnostic, mirroring ``update_notice.py``: the caller decides
where to inject and calls :func:`mark_notified` only after the notice is
actually queued.
"""
from __future__ import annotations

import os
from typing import Optional

from totalreclaw.update_notice import (
    NOTICE_INTERVAL_SECONDS,
    sentinel_mark,
    sentinel_within,
)

_SENTINEL_NAME = "recall-health-notice-last-shown"

#: Statuses from ``agent.recall.auto_recall_with_status`` that can warrant a
#: notice. "unconfigured" and "ok" never do.
_ACTIONABLE_STATUSES = ("error", "empty")


def disabled_by_env() -> bool:
    """True when ``TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE`` is truthy."""
    return os.environ.get(
        "TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE", ""
    ).strip().lower() in ("1", "true", "yes", "on")


def build_recall_failure_notice(status: str, writes_used: Optional[int]) -> str:
    """The one-line diagnostic context injected into the LLM prompt."""
    if status == "error":
        detail = "memory recall raised an error"
    else:
        vault = (
            f"the vault reports {writes_used} stored memories"
            if writes_used
            else "the vault reports stored memories"
        )
        detail = f"memory recall returned nothing even though {vault}"
    return (
        f"[totalreclaw] Memory health warning: {detail}. The memory read "
        "path may be broken (wrong relay URL, outdated client, or routing "
        "bug). Tell the user their TotalReclaw memory is currently "
        "unavailable and suggest running `totalreclaw doctor --recall-smoke` "
        "on the host to diagnose. Do not silently answer as if no memories "
        "exist."
    )


def maybe_build_recall_failure_notice(
    status: str,
    writes_used: Optional[int],
    now: Optional[float] = None,
) -> Optional[str]:
    """Return the notice if it should fire right now, else None.

    The caller must call :func:`mark_notified` only after the notice is
    actually queued (so a failure to inject doesn't burn the 24h window).
    """
    if disabled_by_env():
        return None
    if status not in _ACTIONABLE_STATUSES:
        return None
    if status == "empty" and not (isinstance(writes_used, int) and writes_used > 0):
        return None
    if sentinel_within(_SENTINEL_NAME, NOTICE_INTERVAL_SECONDS, now):
        return None
    return build_recall_failure_notice(status, writes_used)


def mark_notified(now: Optional[float] = None) -> None:
    """Persist 'notice shown at now' so the next 24h are suppressed."""
    sentinel_mark(_SENTINEL_NAME, now)
