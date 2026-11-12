"""Plain-language wording for a write refused for quota (#747).

The write-side sibling of ``read_block.py``: framework-agnostic, used by the
lifecycle (auto-extraction), Hermes' tools and the built-in memory mirror.

The reader is a non-technical user, relayed by whatever LLM drives the host.
Every notice ends with an instruction to the agent, so it neither claims a
memory was saved nor drops the subject.

The relay's 403 body carries only ``message`` and ``upgrade_url``. Tier,
usage and the reset date come from ``/v1/billing/status``
(:func:`fill_write_quota_details`). If that lookup fails, the notice is
worded without numbers and the reset date falls back to the relay's rule:
00:00 UTC on the 1st of next month.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from ..relay import BillingStatus, _parse_resets_at

if TYPE_CHECKING:
    from ..relay import RelayWriteQuotaExceeded

logger = logging.getLogger(__name__)

#: Bound on the billing lookup made when a refusal is first announced.
BILLING_LOOKUP_TIMEOUT_S = 5.0

_PRO_ALLOWANCE = "3,000 memories a month"


def next_monthly_reset(now: Optional[datetime] = None) -> datetime:
    """00:00 UTC on the 1st of the month after *now* (the relay's reset)."""
    now = now or datetime.now(timezone.utc)
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)


def fill_write_quota_details(err: "RelayWriteQuotaExceeded", status: Optional[BillingStatus]) -> None:
    """Fill *err*'s ``tier`` / ``used`` / ``limit`` / ``resets_at`` from a
    billing status. Values the relay already sent win.

    ``used`` / ``limit`` are only taken for the free tier: the relay reports
    ``free_writes_limit`` (the free cap) for Pro accounts too, so the pair is
    not meaningful there.
    """
    if isinstance(status, BillingStatus):
        if err.tier is None and isinstance(status.tier, str):
            err.tier = status.tier
        if (err.tier or status.tier) == "free":
            if err.used is None and isinstance(status.free_writes_used, int):
                err.used = status.free_writes_used
            if err.limit is None and isinstance(status.free_writes_limit, int) and status.free_writes_limit > 0:
                err.limit = status.free_writes_limit
        if err.resets_at is None:
            err.resets_at = _parse_resets_at(status.resets_at)
    if err.resets_at is None:
        err.resets_at = next_monthly_reset()


async def lookup_write_quota_details(err: "RelayWriteQuotaExceeded", client) -> None:
    """Best-effort: one ``client.status()`` call, bounded by
    :data:`BILLING_LOOKUP_TIMEOUT_S`, to fill *err*'s details. Never raises
    (the reset-date fallback still applies)."""
    status = None
    try:
        status = await asyncio.wait_for(client.status(), timeout=BILLING_LOOKUP_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - best effort
        logger.debug("write quota: billing lookup failed (%s)", type(e).__name__)
    fill_write_quota_details(err, status if isinstance(status, BillingStatus) else None)


def _reset_date(err: "RelayWriteQuotaExceeded") -> str:
    resets_at = err.resets_at or next_monthly_reset()
    return resets_at.strftime("%Y-%m-%d")


def _usage(err: "RelayWriteQuotaExceeded") -> str:
    if isinstance(err.used, int) and isinstance(err.limit, int):
        return f" ({err.used:,} of {err.limit:,})"
    return ""


def _is_pro(err: "RelayWriteQuotaExceeded") -> bool:
    return err.tier == "pro"


def format_write_quota_notice(err: "RelayWriteQuotaExceeded", *, queued: int = 0, lost: int = 0) -> str:
    """The ``[totalreclaw]`` context line for a write refused for quota.

    *queued*: refused facts kept on this device for a later retry.
    *lost*: refused facts that could not be kept (disk failure).
    """
    kept = ""
    if queued:
        kept += (
            f" {queued} new {'memory' if queued == 1 else 'memories'} from this "
            "conversation are kept on this device and will be saved "
            "automatically once saving works again."
        )
    if lost:
        kept += (
            f" {lost} new {'memory' if lost == 1 else 'memories'} could not be "
            "kept and are lost."
        )
    upgrade = (
        ""
        if _is_pro(err)
        else f" To keep saving now, offer totalreclaw_upgrade (Pro: {_PRO_ALLOWANCE})."
    )
    return (
        "[totalreclaw] New memories are NOT being saved: this account has used "
        f"its monthly memory allowance{_usage(err)}. Memories saved earlier are "
        f"safe and can still be searched.{kept} The allowance resets on "
        f"{_reset_date(err)} (UTC).{upgrade} Tell the user this in one or two "
        "short sentences. Do NOT say new memories were saved."
    )


def format_write_recovered_notice(saved_from_queue: int) -> str:
    """One-time line once a write succeeds after a refusal was announced."""
    if saved_from_queue:
        return (
            "[totalreclaw] Saving memories works again: "
            f"{saved_from_queue} {'memory' if saved_from_queue == 1 else 'memories'} "
            "that were waiting have now been saved. Mention it once, briefly."
        )
    return "[totalreclaw] Saving memories works again. Mention it once, briefly."


def write_quota_tool_payload(err: "RelayWriteQuotaExceeded") -> dict:
    """JSON payload for a Hermes tool whose write the relay refused for quota
    (``totalreclaw_remember``). No raw HTTP text: the agent relays ``error``
    to a non-technical user."""
    instruction = (
        "Tell the user this memory was NOT saved, and why, in one sentence."
    )
    if _is_pro(err):
        instruction += " It can be saved again after the reset."
    else:
        instruction += " Offer totalreclaw_upgrade."
    return {
        "stored": False,
        "error": (
            "Not saved: this account has used its monthly memory allowance"
            f"{_usage(err)}. It resets on {_reset_date(err)} (UTC)."
        ),
        "error_code": "quota_exceeded",
        "tier": err.tier,
        "used": err.used,
        "limit": err.limit,
        "resets_at": err.resets_at.isoformat() if err.resets_at is not None else None,
        "upgrade_url": err.upgrade_url,
        "instruction": instruction,
    }
