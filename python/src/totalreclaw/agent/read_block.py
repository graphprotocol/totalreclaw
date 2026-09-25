"""Plain-language wording for the client-wide relay read-pause (#662).

Framework-agnostic, like ``recall.py`` beside it — used by Hermes' hooks
(``hermes/hooks.py``) and tools (``hermes/tools.py``), and available to any
other Python agent host built on ``totalreclaw.agent``.

The wording is deliberately non-technical: the reader is a non-technical
end user, relayed through whatever LLM is driving the host. Every notice
ends with an explicit instruction to the agent so a downstream model
doesn't editorialize ("you have no memories") on top of a transport error.

See ``docs/specs/totalreclaw/read-error-surfacing.md`` §4.5-4.6 for the
full design.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from ..relay import (
    RelayRateLimited,
    RelayReadQuotaExceeded,
    read_pause_reprobe_seconds,
)

if TYPE_CHECKING:
    from ..relay import ReadBlockState, RelayReadBlocked


def _reset_phrase(blk: "ReadBlockState") -> str:
    """User-facing phrase for when the monthly read quota resets.

    The new (429 ``read_quota_exceeded``) contract carries ``resets_at``
    directly. The legacy 403 ``quota_exceeded`` contract does not — the
    relay's ``getMonthStart`` is the UTC calendar month, so we display that
    boundary. This is a display floor, not a promise: the re-probe can (and
    in the 2026-09-18 incident, did) recover earlier than the stated date if
    the cap is raised mid-month.
    """
    err = blk.error
    if isinstance(err, RelayReadQuotaExceeded) and not err.legacy and err.resets_at is not None:
        return f"it resets on {err.resets_at.strftime('%Y-%m-%d')} (UTC)"
    return "the 1st of next month (UTC)"


def _remaining_minutes(blk: "ReadBlockState") -> int:
    remaining = max(0.0, blk.paused_until - time.monotonic())
    return max(1, round(remaining / 60))


def format_read_block_notice(blk: "ReadBlockState", *, compact: bool = False) -> str:
    """Format the ``[totalreclaw]`` context-injection notice for *blk*.

    ``compact=True`` is used for the same episode after it has already been
    announced in full once this session (see
    ``AgentState.read_block_notice``).
    """
    if compact:
        hhmm = blk.paused_until_utc.strftime("%H:%M")
        return (
            f"[totalreclaw] Memory lookups still paused until ~{hhmm} UTC. "
            "If the user asks about past memories, say lookups are paused, "
            "not that nothing is saved."
        )

    err = blk.error
    if isinstance(err, RelayReadQuotaExceeded):
        reprobe_min = max(1, round(read_pause_reprobe_seconds() / 60))
        upgrade = f" To lift the limit now: {err.upgrade_url}" if err.upgrade_url else ""
        return (
            "[totalreclaw] Memory lookups are paused: this account has "
            "used its monthly memory-read allowance. Saved memories are "
            "safe, and new ones are still being saved, but I can't search "
            f"them until {_reset_phrase(blk)}. TotalReclaw retries "
            f"automatically every {reprobe_min} minutes, so if the limit "
            f"is raised it resumes on its own.{upgrade} Tell the user this "
            "in one or two short sentences. Do NOT say they have no "
            "memories."
        )

    if isinstance(err, RelayRateLimited):
        minutes = _remaining_minutes(blk)
        return (
            "[totalreclaw] Memory lookups are briefly paused (too many "
            f"requests). They resume automatically in about {minutes} "
            "minutes. Saved memories are safe. Mention it once, briefly. "
            "Do NOT say the user has no memories."
        )

    # Defensive — only RelayReadQuotaExceeded / RelayRateLimited are ever
    # blocking today, but degrade to something honest rather than raising
    # from a notice helper.
    return (
        "[totalreclaw] Memory lookups are paused. Saved memories are safe. "
        "Mention it once, briefly. Do NOT say the user has no memories."
    )


def read_block_tool_payload(
    err: "RelayReadBlocked", blk: Optional["ReadBlockState"]
) -> dict:
    """JSON-friendly payload for a Hermes tool handler that caught
    ``RelayReadBlocked`` (``totalreclaw_recall`` / ``_export`` / ``_pin`` /
    ``_unpin`` / ``_retype`` / ``_set_scope``).

    Deliberately carries NO ``count`` / ``memories`` keys — that shape
    reads as "0 results", which is exactly the confabulation this feature
    exists to prevent.
    """
    is_quota = isinstance(err, RelayReadQuotaExceeded)
    tool_error_code = "read_quota_exceeded" if is_quota else "rate_limited"

    if is_quota:
        reset_phrase = _reset_phrase(blk) if blk is not None else "the monthly reset"
        error_text = (
            "Memory lookups are paused: this account has used its monthly "
            "memory-read allowance. Memories are safe and new ones are "
            f"still being saved; lookups resume automatically ({reset_phrase})."
        )
    else:
        minutes = _remaining_minutes(blk) if blk is not None else None
        if minutes is not None:
            error_text = (
                "Memory lookups are briefly paused (too many requests). "
                f"They resume automatically in about {minutes} minutes."
            )
        else:
            error_text = "Memory lookups are briefly paused (too many requests)."

    resets_at = getattr(err, "resets_at", None)
    return {
        "error": error_text,
        "error_code": tool_error_code,
        "reads_paused_until": blk.paused_until_utc.isoformat() if blk is not None else None,
        "retry_after_seconds": (
            max(0, int(round(blk.paused_until - time.monotonic())))
            if blk is not None
            else None
        ),
        "resets_at": resets_at.isoformat() if resets_at is not None else None,
        "upgrade_url": getattr(err, "upgrade_url", None),
        "instruction": (
            "Relay this to the user in one sentence. Do NOT say they have "
            "no memories."
        ),
    }
