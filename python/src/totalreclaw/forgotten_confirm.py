"""Confirmation step for an explicit remember that restates a forgotten fact
(internal#715).

The forget ledger already keeps auto-extraction from re-creating a fact the
user asked to forget. An explicit ``totalreclaw_remember`` is the agent's own
call, so the agent can bring a forgotten fact back without the user knowing.
When the text restates a ledger entry, the tool stores nothing and returns
``confirmation_required`` with a ``confirm_token``. The agent must ask the
user, and the write goes through only on a re-call with the same text and the
token, in a later turn (same token store and turn rule as the sensitive-data
confirmation, see :mod:`totalreclaw.pii`).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from .forget_guard import ForgetLedger
from .pii import confirmations

logger = logging.getLogger(__name__)

#: Token category for this confirmation.
CATEGORY = "restates_forgotten"

MESSAGE = "This restates something you asked me to forget. Store it anyway?"
NEXT_STEP = (
    "Relay message to the user and wait for their answer. Only if they confirm "
    "in a later message, call totalreclaw_remember again with the same text "
    "and this confirm_token."
)


def forgotten_notice(
    client: Any,
    text: str,
    embedding: Optional[Sequence[float]],
    confirm_token: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Optional[dict]:
    """``None`` when the write may proceed, else the confirmation-required
    tool result. Fails open when the ledger is absent or unreadable (the
    explicit path never blocked on it before)."""
    try:
        ledger = getattr(client, "forget_ledger", None)
        if not isinstance(ledger, ForgetLedger):
            return None
        match = ledger.match(text, embedding)
    except Exception as exc:  # noqa: BLE001
        logger.debug("forgotten confirm: ledger match failed: %s", type(exc).__name__)
        return None
    if not match:
        return None
    try:
        if confirm_token:
            reason = confirmations.redeem(confirm_token, CATEGORY, text, conversation_id)
            if reason is None:
                logger.info("remember storing a forgotten fact's restatement after user confirmation")
                return None
            logger.info("remember: restates a forgotten fact; confirm_token refused (reason=%s)", reason)
        else:
            logger.info("remember: restates a forgotten fact; confirmation required")
        token: Optional[str] = confirmations.issue(CATEGORY, text, conversation_id)
    except Exception as exc:  # noqa: BLE001 - fail closed: never store, never raise
        logger.warning("forgotten confirm: confirmation store error (%s)", type(exc).__name__)
        token = None
    notice = {
        "stored": False,
        "confirmation_required": CATEGORY,
        "fact_id": match.get("id"),
        "message": MESSAGE,
    }
    if token is not None:
        notice["confirm_token"] = token
        notice["next_step"] = NEXT_STEP
    return notice
