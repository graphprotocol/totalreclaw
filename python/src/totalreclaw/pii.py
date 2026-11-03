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
  call carries ``allow_sensitive=true`` (see :func:`screen_explicit`).

Logging rule: never log the screened text — log the decision and category
only. :func:`log_label` gives a log-safe stand-in for the text.

If the core binding is missing or raises, :func:`check` returns ``flag`` with
category ``filter_error``: automatic extraction then drops the fact (never
stores unscreened text), while an explicit remember still stores it (the
user asked for it).
"""
from __future__ import annotations

import json
import logging
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


def block_notice(result: PiiResult) -> dict:
    """Block-and-tell tool result.

    Keys and order are identical to the MCP server's ``sensitiveBlockNotice``
    (``mcp/src/pii.ts``) so every client returns the same object.
    """
    return {
        "stored": False,
        "blocked": "sensitive",
        "category": result.category,
        "message": result.message,
        "override": ALLOW_SENSITIVE_PARAM,
    }


def log_label(text: str, result: PiiResult, limit: int = 200) -> str:
    """Log-safe stand-in: the text preview when allowed, else ``<redacted:category>``."""
    if result.is_allow:
        return text[:limit]
    return f"<redacted:{result.category}>"


def screen_explicit(text: str, allow_sensitive: bool) -> tuple[PiiResult, Optional[dict]]:
    """Gate for an explicit remember.

    Returns ``(result, notice)``. ``notice`` is the block-and-tell dict to
    return instead of storing, or ``None`` when the write may proceed.
    """
    result = check(text)
    if result.decision == "block" and not allow_sensitive:
        logger.info("remember blocked: sensitive text (category=%s)", result.category)
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
