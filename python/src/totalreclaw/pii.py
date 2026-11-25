"""Sensitive-data (PII / credential) screening for memory writes — PRD-04 DEP-4.

Thin adapter over ``totalreclaw_core.pii_filter`` (Rust, PyO3). The decision
logic, the category tags and the user-facing block messages all live in core,
so Hermes, the MCP server and the enclave make identical decisions.

Semantics (PRD-04 Q2, "block-and-tell"):

* automatic extraction stores a fact only when the decision is ``allow`` —
  ``block`` and ``flag`` are dropped (see :func:`drop_sensitive_facts`,
  :func:`auto_storable`);
* an explicit ``totalreclaw_remember`` stores ``allow`` and ``flag``; for
  ``block`` it returns :func:`block_notice` instead of storing, unless the
  call carries ``allow_sensitive=true`` AND a ``confirm_token`` issued by an
  earlier block, redeemed in a later conversation turn (see
  :func:`screen_explicit` and :class:`ConfirmationStore`). The tool enforces
  this, so the contract does not depend on the chat model's discretion.

Logging rule: never log the screened text — log the decision and category
only. :func:`log_label` gives a log-safe stand-in for the text.

If the core binding is missing or raises, :func:`check` returns ``flag`` with
category ``filter_error``: automatic extraction then drops the fact (never
stores unscreened text), while an explicit remember still stores it (the
user asked for it).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Optional, TypeVar

logger = logging.getLogger(__name__)

#: Tool parameter that overrides a ``block`` on an explicit remember.
ALLOW_SENSITIVE_PARAM = "allow_sensitive"
#: Category reported when the core filter itself is unavailable or failed.
FILTER_ERROR_CATEGORY = "filter_error"

#: Extraction actions that write a new fact (DELETE/NOOP never store text).
_STORING_ACTIONS = frozenset({"ADD", "UPDATE"})

T = TypeVar("T")


@dataclass(frozen=True)
class PiiResult:
    """Decoded ``pii_filter`` decision."""

    decision: str  # "allow" | "block" | "flag"
    category: Optional[str] = None
    message: Optional[str] = None

    @property
    def is_allow(self) -> bool:
        return self.decision == "allow"


def check(text: str) -> PiiResult:
    """Run the core filter on ``text``."""
    try:
        import totalreclaw_core

        raw = json.loads(totalreclaw_core.pii_filter(text))
        return PiiResult(
            decision=str(raw["decision"]),
            category=raw.get("category"),
            message=raw.get("message"),
        )
    except Exception as exc:  # noqa: BLE001 — fail closed for auto, open for explicit
        logger.warning(
            "pii_filter unavailable (%s); treating text as flag/%s",
            type(exc).__name__,
            FILTER_ERROR_CATEGORY,
        )
        return PiiResult(decision="flag", category=FILTER_ERROR_CATEGORY)


def auto_storable(text: str) -> bool:
    """True only when automatic extraction may store ``text``."""
    return check(text).is_allow


def parse_allow_sensitive(value: Any) -> bool:
    """Strict opt-in: only JSON ``true`` or the string ``"true"`` count.

    ``bool("false")`` is ``True`` in Python, so a naive ``bool(...)`` would
    let a stringly-typed ``"false"`` override a block.
    """
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


#: Confirmation tokens live this long (seconds).
CONFIRM_TOKEN_TTL_SECONDS = 30 * 60
#: At most this many live tokens are kept; the oldest is dropped first.
CONFIRM_TOKEN_MAX_LIVE = 32

CONFIRM_NEXT_STEP = (
    "Relay message to the user and wait for their answer. Only if they confirm "
    "in a later message, call totalreclaw_remember again with "
    "allow_sensitive=true and this confirm_token."
)


def _normalize_value(text: str) -> str:
    """Normalize ``text`` for value binding: NFKC, casefold, collapsed whitespace."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


#: Cap on per-conversation turn counters kept (oldest dropped first).
_MAX_TRACKED_CONVERSATIONS = 256


class ConfirmationStore:
    """Process-wide store of single-use confirmation tokens for sensitive writes.

    A token is issued with a block and records the category, a keyed hash of
    the normalized text it was issued for, the conversation id (when the host
    gave one) and the completed turn count at issue time. :meth:`redeem`
    accepts it only if it is known, unexpired, the category and the value hash
    match, the conversation matches, and at least one turn completed since
    issue (:meth:`note_turn_completed`), so the user had a chance to answer.

    The plaintext value is never stored: only an HMAC-SHA256 under a random
    per-process salt. When a conversation id is known, the turn counter that
    must advance is that conversation's own, so another chat finishing a turn
    cannot release this chat's token. Without an id the process-wide counter
    is used. The clock is injectable for tests.
    """

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        # token -> (category, issued_at, issued_global_turn, issued_conv_turn,
        #           value_hash, conversation_id)
        self._tokens: "OrderedDict[str, tuple[str, float, int, int, bytes, Optional[str]]]" = OrderedDict()
        self._turns = 0
        self._conv_turns: "OrderedDict[str, int]" = OrderedDict()
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()

    def _hash(self, text: Optional[str]) -> bytes:
        return hmac.new(self._salt, _normalize_value(text or "").encode("utf-8"), hashlib.sha256).digest()

    @property
    def turn_seq(self) -> int:
        with self._lock:
            return self._turns

    def note_turn_completed(self, conversation_id: Optional[str] = None) -> None:
        with self._lock:
            self._turns += 1
            if conversation_id:
                self._conv_turns[conversation_id] = self._conv_turns.pop(conversation_id, 0) + 1
                while len(self._conv_turns) > _MAX_TRACKED_CONVERSATIONS:
                    self._conv_turns.popitem(last=False)

    def issue(
        self,
        category: Optional[str],
        text: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        with self._lock:
            token = secrets.token_hex(8)
            conv = conversation_id or None
            self._tokens[token] = (
                str(category),
                self._clock(),
                self._turns,
                self._conv_turns.get(conv, 0) if conv else 0,
                self._hash(text),
                conv,
            )
            while len(self._tokens) > CONFIRM_TOKEN_MAX_LIVE:
                self._tokens.popitem(last=False)
            return token

    def redeem(
        self,
        token: Optional[str],
        category: Optional[str],
        text: Optional[str] = None,
        conversation_id: Optional[str] = None,
        now: Optional[float] = None,
        turn_seq: Optional[int] = None,
    ) -> Optional[str]:
        """Return ``None`` and consume the token if valid, else the reject reason."""
        if not isinstance(token, str) or not token:
            return "no_token"
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return "unknown_token"
            tok_category, issued_at, issued_turn, issued_conv_turn, tok_hash, tok_conv = entry
            now = self._clock() if now is None else now
            conv = conversation_id or None
            if now - issued_at >= CONFIRM_TOKEN_TTL_SECONDS:
                del self._tokens[token]
                return "expired"
            # A token issued for a conversation is redeemable only there; one
            # issued without an id is redeemable with or without one.
            if tok_conv is not None and tok_conv != conv:
                return "conversation_mismatch"
            # Per-conversation counter only if ingest_turn has recorded a turn
            # for this conversation id in this process and it was not reset; otherwise the
            # process-wide counter (the host may not pass the same id to tools
            # and to ingest_turn).
            if tok_conv is not None and self._conv_turns.get(tok_conv, -1) >= issued_conv_turn:
                # (a reset or evicted counter reads below the issue-time value)
                counter, issued, used = self._conv_turns[tok_conv], issued_conv_turn, "conversation"
            else:
                counter, issued, used = self._turns, issued_turn, "global"
            if turn_seq is not None:
                counter = turn_seq
            logger.debug("confirmation turn check used the %s counter", used)
            if counter <= issued:
                return "same_turn"
            if tok_category != str(category):
                return "category_mismatch"
            if not hmac.compare_digest(tok_hash, self._hash(text)):
                return "value_mismatch"
            del self._tokens[token]
            return None


#: Shared by every conversation slot (the gateway is one process).
confirmations = ConfirmationStore()


def note_turn_completed(conversation_id: Optional[str] = None) -> None:
    """Record a completed conversation turn (called from the per-turn sync)."""
    confirmations.note_turn_completed(conversation_id)


def block_notice(result: PiiResult, confirm_token: Optional[str] = None) -> dict:
    """Block-and-tell tool result.

    Keys and order match the planned MCP ``sensitiveBlockNotice``
    (``mcp/src/pii.ts`` does not exist yet; the MCP client must adopt this
    same shape, including ``confirm_token`` and ``next_step``, when it adds
    the filter). ``confirm_token`` and ``next_step`` are appended only when a
    token is given.
    """
    notice = {
        "stored": False,
        "blocked": "sensitive",
        "category": result.category,
        "message": result.message,
        "override": ALLOW_SENSITIVE_PARAM,
    }
    if confirm_token is not None:
        notice["confirm_token"] = confirm_token
        notice["next_step"] = CONFIRM_NEXT_STEP
    return notice


def log_label(text: str, result: PiiResult, limit: int = 200) -> str:
    """Log-safe stand-in: the text preview when allowed, else ``<redacted:category>``."""
    if result.is_allow:
        return text[:limit]
    return f"<redacted:{result.category}>"


def screen_explicit(
    text: str,
    allow_sensitive: bool,
    confirm_token: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> tuple[PiiResult, Optional[dict]]:
    """Gate for an explicit remember.

    Returns ``(result, notice)``. ``notice`` is the block-and-tell dict to
    return instead of storing, or ``None`` when the write may proceed.
    ``allow_sensitive`` overrides a block only with a valid ``confirm_token``
    (see :class:`ConfirmationStore`); otherwise it is ignored.
    """
    result = check(text)
    if result.decision == "block":
        try:
            if allow_sensitive:
                reason = confirmations.redeem(
                    confirm_token, result.category, text, conversation_id
                )
                if reason is None:
                    logger.info(
                        "remember storing sensitive text on explicit request (decision=%s category=%s)",
                        result.decision,
                        result.category,
                    )
                    return result, None
                logger.info(
                    "remember blocked: allow_sensitive without a valid confirmation "
                    "(reason=%s, category=%s)",
                    reason,
                    result.category,
                )
            else:
                logger.info("remember blocked: sensitive text (category=%s)", result.category)
            return result, block_notice(result, confirmations.issue(result.category, text, conversation_id))
        except Exception as exc:  # noqa: BLE001 — fail closed: never store, never raise
            logger.warning("remember blocked: confirmation store error (%s)", type(exc).__name__)
            try:
                return result, block_notice(result, confirmations.issue(result.category, text, conversation_id))
            except Exception:  # noqa: BLE001
                return result, block_notice(result)
    if not result.is_allow:
        logger.info(
            "remember storing sensitive text on explicit request (decision=%s category=%s)",
            result.decision,
            result.category,
        )
    return result, None


def drop_sensitive_facts(facts: Iterable[T]) -> list[T]:
    """Gate for automatic extraction over ``ExtractedFact``-like objects.

    ADD/UPDATE facts are kept only when their ``text`` is ``allow``;
    DELETE/NOOP facts pass through untouched (they write no text).
    """
    kept: list[T] = []
    for fact in facts:
        action = str(getattr(fact, "action", "ADD") or "ADD").upper()
        if action in _STORING_ACTIONS:
            result = check(str(getattr(fact, "text", "") or ""))
            if not result.is_allow:
                logger.info(
                    "auto-extraction dropped a fact (decision=%s category=%s)",
                    result.decision,
                    result.category,
                )
                continue
        kept.append(fact)
    return kept
