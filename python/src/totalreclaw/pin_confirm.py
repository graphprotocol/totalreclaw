"""Confirmation step before the agent changes a user-pinned fact (internal#778).

A pin is the user's "do not change this". The agent used to unpin, overwrite
and forget a pinned fact on its own. Now ``totalreclaw_forget``,
``totalreclaw_unpin``, ``totalreclaw_retype``, ``totalreclaw_set_scope`` and a
``totalreclaw_remember`` that restates a pinned fact stop on a pinned fact:
nothing changes, and the result is ``confirmation_required`` with a
``confirm_token``. The change happens only when the agent calls again with the
same arguments and the token, after asking the user, in a later turn (same
token store and turn rule as the sensitive-data confirmation, see
:mod:`totalreclaw.pii`).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .pii import confirmations

logger = logging.getLogger(__name__)

#: action -> (tool name, key that reports "not done", message for the user)
_ACTIONS: dict[str, tuple[str, str, str]] = {
    "forget": ("totalreclaw_forget", "deleted", "This memory is pinned. Forget it anyway?"),
    "unpin": ("totalreclaw_unpin", "unpinned", "This memory is pinned. Unpin it anyway?"),
    "retype": ("totalreclaw_retype", "retyped", "This memory is pinned. Change its type anyway?"),
    "set_scope": ("totalreclaw_set_scope", "scope_set", "This memory is pinned. Change its scope anyway?"),
    "remember": (
        "totalreclaw_remember",
        "stored",
        "A pinned memory already says something similar. Store this anyway?",
    ),
}

_NEXT_STEP = (
    "Relay message to the user and wait for their answer. Only if they confirm "
    "in a later message, call {tool} again with the same arguments and this "
    "confirm_token."
)


def category(action: str) -> str:
    return f"pinned_{action}"


def confirm_or_notice(
    action: str,
    fact_id: str,
    subject: str,
    confirm_token: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Optional[dict]:
    """``None`` when the change may proceed (a valid token from an earlier
    turn), else the confirmation-required tool result. Call only once the
    target is known to be pinned."""
    tool, done_key, message = _ACTIONS[action]
    cat = category(action)
    if confirm_token:
        reason = confirmations.redeem(confirm_token, cat, subject, conversation_id)
        if reason is None:
            logger.info("%s on a pinned memory: proceeding after user confirmation", action)
            return None
        logger.info("%s on a pinned memory: confirm_token refused (reason=%s)", action, reason)
    else:
        logger.info("%s on a pinned memory: confirmation required", action)
    return {
        done_key: False,
        "confirmation_required": cat,
        "fact_id": fact_id,
        "message": message,
        "confirm_token": confirmations.issue(cat, subject, conversation_id),
        "next_step": _NEXT_STEP.format(tool=tool),
    }


async def pinned_change_notice(
    client: Any,
    action: str,
    fact_id: str,
    args: dict,
    kwargs: dict,
    *,
    subject: Optional[str] = None,
) -> Optional[dict]:
    """Look up the pin state of ``fact_id`` and, when it is pinned, return the
    confirmation-required result (or ``None`` once a valid token is given).

    A failed pin lookup raises: the calling tool reports the error and changes
    nothing (fail closed), the same as the pin guard on auto-extraction.
    """
    if not await client.get_fact_pin_status(fact_id):
        return None
    token = args.get("confirm_token")
    conv = kwargs.get("session_id")
    return confirm_or_notice(
        action,
        fact_id,
        subject if subject is not None else fact_id,
        token if isinstance(token, str) else None,
        conv if isinstance(conv, str) else None,
    )
