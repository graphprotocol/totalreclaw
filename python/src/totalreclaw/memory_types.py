"""Memory Taxonomy v1 vocabulary — the shared, dependency-free vocabulary.

This is a leaf module: it imports nothing from ``totalreclaw`` (only stdlib),
so both the root package (``claims_helper``, ``retype_setscope``,
``operations``) and the ``agent`` subpackage can depend on it without creating
an import cycle. Historically these constants lived in
``totalreclaw.agent.extraction``; root modules importing them from there forced
``agent``↔root deferred-import workarounds. ``agent.extraction`` now re-exports
from here for backward compatibility.

See ``docs/specs/totalreclaw/memory-taxonomy-v1.md``.
"""
from __future__ import annotations

from typing import Any

#: The 6 canonical v1 memory types.
VALID_MEMORY_TYPES: tuple[str, ...] = (
    "claim",
    "preference",
    "directive",
    "commitment",
    "episode",
    "summary",
)

#: Backward-compat alias — prefer ``VALID_MEMORY_TYPES`` in new code.
VALID_TYPES: frozenset[str] = frozenset(VALID_MEMORY_TYPES)

#: The 5 v1 provenance sources.
VALID_MEMORY_SOURCES: tuple[str, ...] = (
    "user",
    "user-inferred",
    "assistant",
    "external",
    "derived",
)

#: The 8 v1 life-domain scopes.
VALID_MEMORY_SCOPES: tuple[str, ...] = (
    "work",
    "personal",
    "health",
    "family",
    "creative",
    "finance",
    "misc",
    "unspecified",
)

#: The 3 v1 volatility classes.
VALID_MEMORY_VOLATILITIES: tuple[str, ...] = (
    "stable",
    "updatable",
    "ephemeral",
)

#: Legacy v0 memory types — retained so ``read_claim_from_blob`` / legacy
#: fixtures can still decode pre-v1 vault entries. Do NOT emit on the write path.
LEGACY_V0_MEMORY_TYPES: tuple[str, ...] = (
    "fact",
    "preference",
    "decision",
    "episodic",
    "goal",
    "context",
    "summary",
    "rule",
)

#: Legacy v0 → v1 type mapping used on the read path.
V0_TO_V1_TYPE: dict[str, str] = {
    "fact": "claim",
    "preference": "preference",
    "decision": "claim",
    "episodic": "episode",
    "goal": "commitment",
    "context": "claim",
    "summary": "summary",
    "rule": "directive",
}


def is_valid_memory_type(value: Any) -> bool:
    """v1 type guard — returns True iff ``value`` is one of the 6 v1 types."""
    return isinstance(value, str) and value in VALID_MEMORY_TYPES


def normalize_to_v1_type(raw: Any) -> str:
    """Normalize any type token (v1 or legacy v0) to a v1 type.

    v1 tokens pass through. Legacy v0 tokens are mapped via
    ``V0_TO_V1_TYPE``. Unknown input falls back to ``"claim"``.
    """
    token = str(raw or "").lower()
    if token in VALID_MEMORY_TYPES:
        return token
    return V0_TO_V1_TYPE.get(token, "claim")
