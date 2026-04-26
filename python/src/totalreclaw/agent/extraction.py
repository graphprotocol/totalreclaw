"""
LLM-guided fact extraction for TotalReclaw (Memory Taxonomy v1).

Mirrors the v1 G-pipeline from the OpenClaw plugin's ``extractor.ts``:

  1. Single merged-topic LLM call → ``{topics, facts}``
  2. ``apply_provenance_filter_lax`` (tag-don't-drop; assistant-sourced facts
     are capped at importance 7 rather than filtered out)
  3. ``comparative_rescore_v1`` (forces re-rank when ``facts >= 5``)
  4. ``default_volatility`` heuristic fallback
  5. Lexical importance bumps

As of ``totalreclaw`` 2.0.0 v1 is the DEFAULT AND ONLY extraction path — no
env-var gate. Legacy v0 tokens (fact, decision, episodic, goal, context,
rule) are coerced to v1 via ``V0_TO_V1_TYPE`` on the read side so pre-2.0
vault entries still deserialize, but extraction emits v1 unconditionally.

This module is framework-agnostic — any Python agent integration (Hermes,
LangChain, CrewAI, or custom agents) can import from here.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

import totalreclaw_core

from .llm_client import (
    LLMConfig,
    chat_completion,
    derive_extraction_config,
    detect_llm_config,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Memory Taxonomy v1 — the 6 canonical memory types.
#
# Plugin 3.0.0 / python 2.0.0 adopt v1 as the ONLY taxonomy. Legacy v0 tokens
# are retained as ``LEGACY_V0_MEMORY_TYPES`` + ``V0_TO_V1_TYPE`` for the
# read path (pre-v1 vault entries still carry v0 strings). Extraction and
# write paths emit v1 exclusively.
#
# When adding a new v1 type, update ALL of:
#   * ``skill/plugin/extractor.ts`` VALID_MEMORY_TYPES
#   * ``mcp/src/v1-types.ts`` VALID_MEMORY_TYPES
#   * ``python/src/totalreclaw/agent/extraction.py`` (this constant)
#   * ``rust/totalreclaw-core/src/claims.rs`` MemoryTypeV1 enum
#   * ``skill/plugin/claims-helper.ts`` TYPE_TO_CATEGORY_V1
#   * ``python/src/totalreclaw/claims_helper.py`` TYPE_TO_CATEGORY_V1
#   * The EXTRACTION_SYSTEM_PROMPT "TYPE" section
# ---------------------------------------------------------------------------

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

VALID_ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP"}

#: Allowed entity types — must match ``skill/plugin/extractor.ts``.
VALID_ENTITY_TYPES = {"person", "project", "tool", "company", "concept", "place"}

#: Default confidence score when the LLM response omits ``confidence``.
#: Must match the plugin's ``DEFAULT_EXTRACTION_CONFIDENCE``.
DEFAULT_EXTRACTION_CONFIDENCE: float = 0.85


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


@dataclass
class ExtractedEntity:
    """A named entity referenced by an extracted fact.

    Mirrors ``skill/plugin/extractor.ts`` ``ExtractedEntity``. The ``type``
    field is one of :data:`VALID_ENTITY_TYPES`. ``role`` is optional free
    text describing the entity's role in the claim ("chooser", "employer").
    """

    name: str
    type: str
    role: Optional[str] = None


@dataclass
class ExtractedFact:
    """Extracted fact carrying full v1 taxonomy fields.

    Mirrors the TS ``ExtractedFact`` (v1). ``source`` is optional on the
    dataclass but required on the write path — upstream pipelines
    (:func:`apply_provenance_filter_lax`) tag it during extraction;
    defensive callers supply ``"user-inferred"`` when upstream fails.

    ``_embedding`` is a transient field populated by
    :func:`deduplicate_facts_by_embedding` and consumed by the
    lifecycle-layer store path. It is NOT serialized to the vault —
    embeddings are computed from ``text`` on the write side. Kept on the
    dataclass so in-batch cross-fact dedup doesn't have to recompute the
    vector at store time.
    """

    text: str
    type: str  # v1: claim | preference | directive | commitment | episode | summary
    importance: int  # 1-10
    action: str  # ADD | UPDATE | DELETE | NOOP
    existing_fact_id: Optional[str] = None
    entities: Optional[List[ExtractedEntity]] = None
    confidence: float = DEFAULT_EXTRACTION_CONFIDENCE
    # v1 additions (all optional at the dataclass level; the write path
    # populates missing ``source`` with "user-inferred" defensively).
    source: Optional[str] = None
    scope: Optional[str] = None
    reasoning: Optional[str] = None
    volatility: Optional[str] = None
    # Transient — set by deduplicate_facts_by_embedding, read by the
    # lifecycle store path. Not part of the v1 wire format.
    _embedding: Optional[List[float]] = None


def normalize_confidence(raw: Any) -> float:
    """Clamp a raw confidence value to ``[0, 1]`` with default fallback."""
    if isinstance(raw, bool):
        return DEFAULT_EXTRACTION_CONFIDENCE
    if not isinstance(raw, (int, float)):
        return DEFAULT_EXTRACTION_CONFIDENCE
    f = float(raw)
    if not math.isfinite(f):
        return DEFAULT_EXTRACTION_CONFIDENCE
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _parse_entity(raw: Any) -> Optional[ExtractedEntity]:
    """Parse a single entity dict from LLM output; return None if invalid.

    Invalid entities are silently dropped so one bad entry never kills the
    whole fact. Mirrors the plugin's ``parseEntity``.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    etype = str(raw.get("type", "")).lower()
    if etype not in VALID_ENTITY_TYPES:
        return None
    entity = ExtractedEntity(name=name[:128], type=etype)
    role = raw.get("role")
    if isinstance(role, str) and role.strip():
        entity.role = role.strip()[:128]
    return entity


# ---------------------------------------------------------------------------
# Extraction system prompts (v1 merged-topic pipeline)
#
# As of totalreclaw 2.3.0 these prompts are sourced from the Rust core
# (``totalreclaw_core.get_extraction_system_prompt`` /
# ``get_compaction_system_prompt``, available since core 2.2.0). One
# source → byte-identical prompt bytes across Python and the TypeScript
# clients (skill/plugin + skill-nanoclaw via the WASM binding), so the
# cross-client extraction parity that the v1 taxonomy depends on is
# enforced at compile time rather than by convention. See
# ``docs/specs/totalreclaw/memory-taxonomy-v1.md`` §"Canonical prompts".
#
# The names ``EXTRACTION_SYSTEM_PROMPT`` + ``COMPACTION_SYSTEM_PROMPT``
# are preserved as module-level ``str`` constants so existing importers
# and tests (``python/tests/test_v1_taxonomy.py``) continue to work
# unchanged.
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT: str = totalreclaw_core.get_extraction_system_prompt()
"""Canonical v1 merged-topic extraction prompt. Sourced from core 2.2.0+.

This binding is a static reference — the underlying bytes are embedded
into ``totalreclaw-core`` via ``include_str!`` so the prompt is
byte-identical across Python and the TypeScript clients (skill/plugin,
skill-nanoclaw) that consume the same WASM binding.
"""

COMPACTION_SYSTEM_PROMPT: str = totalreclaw_core.get_compaction_system_prompt()
"""Canonical v1 compaction prompt. Sourced from core 2.2.0+.

Compaction variant drops the importance floor to 5 (from the default 6)
and adds a format-agnostic parsing section. Byte-identical across all
TotalReclaw clients by construction.
"""


# ---------------------------------------------------------------------------
# Helpers — message formatting + response parsing
# ---------------------------------------------------------------------------


def _truncate_messages(messages: list[dict], max_chars: int = 12000) -> str:
    """Format and truncate messages to fit token budget."""
    lines = []
    total = 0
    for msg in messages:
        line = f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def _build_fact(
    raw: dict, default_type: str = "claim", importance_floor: int = 6
) -> Optional[ExtractedFact]:
    """Construct an ``ExtractedFact`` from a single raw JSON dict.

    Returns None for items that fail type/text validation. Filters by
    importance threshold (preserves DELETE actions).
    """
    text = str(raw.get("text", "")).strip()
    if len(text) < 5:
        return None

    # Accept both v1 tokens and legacy v0 tokens — coerce v0 via V0_TO_V1_TYPE.
    raw_type = str(raw.get("type", default_type)).lower()
    fact_type = normalize_to_v1_type(raw_type)

    raw_source = str(raw.get("source", "user-inferred")).lower()
    source = raw_source if raw_source in VALID_MEMORY_SOURCES else "user-inferred"

    raw_scope = str(raw.get("scope", "unspecified")).lower()
    scope = raw_scope if raw_scope in VALID_MEMORY_SCOPES else "unspecified"

    reasoning_raw = raw.get("reasoning")
    reasoning = reasoning_raw[:256] if isinstance(reasoning_raw, str) and reasoning_raw else None

    try:
        importance = max(1, min(10, int(raw.get("importance", 5))))
    except (ValueError, TypeError):
        importance = 5

    action = str(raw.get("action", "ADD")).upper()
    if action not in VALID_ACTIONS:
        action = "ADD"

    # Reject illegal type:summary + source:user combination
    if fact_type == "summary" and source == "user":
        return None

    # Importance floor (DELETE always passes)
    if importance < importance_floor and action != "DELETE":
        return None

    existing_id = raw.get("existingFactId") or raw.get("existing_fact_id")

    raw_entities = raw.get("entities")
    entities: Optional[List[ExtractedEntity]] = None
    if isinstance(raw_entities, list):
        parsed_entities = [e for e in (_parse_entity(r) for r in raw_entities) if e is not None]
        if parsed_entities:
            entities = parsed_entities

    confidence = normalize_confidence(raw.get("confidence"))

    volatility_raw = raw.get("volatility")
    volatility = (
        volatility_raw if isinstance(volatility_raw, str) and volatility_raw in VALID_MEMORY_VOLATILITIES
        else None
    )

    return ExtractedFact(
        text=text[:512],
        type=fact_type,
        importance=importance,
        action=action,
        existing_fact_id=str(existing_id) if existing_id else None,
        entities=entities,
        confidence=confidence,
        source=source,
        scope=scope,
        reasoning=reasoning,
        volatility=volatility,
    )


def parse_merged_response_v1(response: str) -> tuple[list[str], list[ExtractedFact]]:
    """Parse a v1 merged-topic LLM response into ``(topics, facts)``.

    Accepts:
      - The canonical ``{"topics": [...], "facts": [...]}`` merged shape.
      - A bare JSON array of fact objects (legacy / test-fixture shape;
        wrapped into ``{"topics": [], "facts": [...]}`` before validation).
      - A single fact object without a wrapper (wrapped the same way).

    Invalid entities, unknown sources/scopes, and legacy v0 type tokens are
    all coerced transparently; the downstream ``_build_fact`` applies type
    normalization and the importance >= 6 floor.
    """
    original_preview = response.strip()[:200]
    cleaned = response.strip()

    # Strip <think>...</think> (case-insensitive, multiline)
    cleaned = re.sub(
        r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", cleaned, flags=re.IGNORECASE
    ).strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # First try outermost-array greedy match (bare-array legacy shape).
        m_arr = re.search(r"\[[\s\S]*\]", cleaned)
        if m_arr:
            try:
                parsed = json.loads(m_arr.group(0))
                logger.info("parse_merged_response_v1: recovered JSON via bracket-scan fallback")
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            # Fall back to outermost-object greedy match (merged wrapper).
            m_obj = re.search(r"\{[\s\S]*\}", cleaned)
            if m_obj:
                try:
                    parsed = json.loads(m_obj.group(0))
                    logger.info("parse_merged_response_v1: recovered JSON via bracket-scan fallback")
                except json.JSONDecodeError:
                    parsed = None

    if parsed is None:
        logger.warning(
            "parse_merged_response_v1: could not parse LLM output as JSON. Preview: %r",
            original_preview,
        )
        return [], []

    # Dual-format acceptance.
    if isinstance(parsed, list):
        obj: dict = {"topics": [], "facts": parsed}
    elif isinstance(parsed, dict) and "facts" not in parsed and isinstance(parsed.get("text"), str):
        # Single fact object without a wrapper.
        obj = {"topics": [], "facts": [parsed]}
    elif isinstance(parsed, dict):
        obj = parsed
    else:
        logger.warning(
            "parse_merged_response_v1: parsed value is %s, not object/array",
            type(parsed).__name__,
        )
        return [], []

    raw_topics = obj.get("topics")
    topics: list[str] = []
    if isinstance(raw_topics, list):
        topics = [t for t in raw_topics if isinstance(t, str) and t][:3]

    raw_facts = obj.get("facts")
    if not isinstance(raw_facts, list):
        return topics, []

    facts: list[ExtractedFact] = []
    for raw in raw_facts:
        if not isinstance(raw, dict):
            continue
        fact = _build_fact(raw, default_type="claim", importance_floor=6)
        if fact is not None:
            facts.append(fact)

    return topics, facts


def parse_facts_response(response: str) -> list[ExtractedFact]:
    """Thin wrapper: discard topics, return the flat fact list."""
    _, facts = parse_merged_response_v1(response)
    return facts


def parse_facts_response_for_compaction(response: str) -> list[ExtractedFact]:
    """Parse facts for the compaction prompt (importance floor 5, not 6).

    Same JSON-cleaning + recovery logic as :func:`parse_merged_response_v1`
    but admits borderline facts (importance >= 5) that would normally be
    filtered — compaction is the last chance to capture them.
    """
    original_preview = response.strip()[:200]
    cleaned = response.strip()

    cleaned = re.sub(
        r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", cleaned, flags=re.IGNORECASE
    ).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m_arr = re.search(r"\[[\s\S]*\]", cleaned)
        if m_arr:
            try:
                parsed = json.loads(m_arr.group(0))
                logger.info(
                    "parse_facts_response_for_compaction: recovered JSON via bracket-scan fallback"
                )
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            m_obj = re.search(r"\{[\s\S]*\}", cleaned)
            if m_obj:
                try:
                    parsed = json.loads(m_obj.group(0))
                    logger.info(
                        "parse_facts_response_for_compaction: recovered JSON via bracket-scan fallback"
                    )
                except json.JSONDecodeError:
                    parsed = None

    if parsed is None:
        logger.warning(
            "parse_facts_response_for_compaction: could not parse LLM output as JSON. Preview: %r",
            original_preview,
        )
        return []

    raw_facts: list
    if isinstance(parsed, list):
        raw_facts = parsed
    elif isinstance(parsed, dict):
        raw_facts = parsed.get("facts") if isinstance(parsed.get("facts"), list) else []
    else:
        logger.warning(
            "parse_facts_response_for_compaction: parsed value is %s", type(parsed).__name__
        )
        return []

    facts: list[ExtractedFact] = []
    for raw in raw_facts:
        if not isinstance(raw, dict):
            continue
        fact = _build_fact(raw, default_type="claim", importance_floor=5)
        if fact is not None:
            facts.append(fact)
    return facts


# ---------------------------------------------------------------------------
# v1 provenance filter (tag-don't-drop) + volatility defaults
# ---------------------------------------------------------------------------


def apply_provenance_filter_lax(
    facts: list[ExtractedFact], conversation_text: str
) -> list[ExtractedFact]:
    """Tag-don't-drop provenance filter (pipeline G / F).

    For each fact:
      - If source is already ``"assistant"``, cap importance at 7.
      - Otherwise, keyword-match the fact against user turns. If <30% of
        content words (length >= 4) appear in user turns AND source != "user",
        tag source as ``"assistant"`` and cap importance at 7 (keep the fact).
      - Drop facts below importance 5 unless DELETE action.
    """
    # Join all user turns for keyword matching.
    user_turns_lower = " ".join(
        line for line in conversation_text.split("\n\n") if line.startswith("[user]:")
    ).lower()

    out: list[ExtractedFact] = []
    for f in facts:
        if f.source == "assistant":
            f.importance = min(f.importance, 7)
            out.append(f)
            continue

        # Content words from the fact (length >= 4)
        fact_words = [
            w for w in re.sub(r"[^a-z0-9\s]", " ", f.text.lower()).split() if len(w) >= 4
        ]
        matched = sum(1 for w in fact_words if w in user_turns_lower)
        match_ratio = (matched / len(fact_words)) if fact_words else 0.0

        if match_ratio < 0.3 and f.source != "user":
            f.source = "assistant"
            f.importance = min(f.importance, 7)

        out.append(f)

    return [f for f in out if f.importance >= 5 or f.action == "DELETE"]


def default_volatility(fact: ExtractedFact) -> str:
    """Heuristic fallback volatility when the LLM doesn't assign one.

    Mirrors the TS ``defaultVolatility``.
    """
    if fact.type == "commitment":
        return "updatable"
    if fact.type == "episode":
        return "stable"
    if fact.type == "directive":
        return "stable"
    if fact.scope in ("health", "family"):
        return "stable"
    return "updatable"


COMPARATIVE_PROMPT_V1 = """You are a memory re-ranker for the v1 taxonomy. You receive facts already extracted from one conversation, each with initial importance. Your job is twofold:

1. RE-RANK importance to spread across the 1-10 range (avoid clustering at 7-8-9)
2. ASSIGN volatility to each fact

Re-ranking rules:
- Top 1/3 of facts (most significant for this user): importance 9-10
- Middle 1/3: importance 7-8
- Bottom 1/3: importance 5-6 (borderline, may be dropped)
- A fact may stay at 10 if it's clearly identity-defining (name, birthday) or marked as "never forget"
- Never raise without justification; never lower below 5 unless clearly noise
- You MUST produce a spread

Volatility rules:
- stable: unlikely to change for years (name, allergies, birthplace, fundamental traits)
- updatable: changes occasionally (current job, active project, partner's name, address)
- ephemeral: short-lived state (today's task, this week's plan, current trip itinerary)

Use the FULL conversation context to judge volatility — a single claim may be ambiguous, but in context you can usually tell.

Return JSON array, same order as input, ONLY with importance + volatility fields:
[{"importance": N, "volatility": "stable|updatable|ephemeral"}, ...]
No markdown."""


async def comparative_rescore_v1(
    facts: list[ExtractedFact],
    conversation_text: str,
    llm_config: Optional[LLMConfig] = None,
) -> list[ExtractedFact]:
    """Comparative re-scoring pass.

    Forces LLM re-rank when ``len(facts) >= 5`` to spread importance across
    the 1-10 range. When ``len(facts) < 5`` (or no LLM), fills any missing
    volatility via :func:`default_volatility` and returns.
    """
    # G-tuned behavior: force rescore when >= 5 facts
    if len(facts) < 2 or len(facts) < 5:
        for f in facts:
            if f.volatility is None:
                f.volatility = default_volatility(f)
        return facts

    config = llm_config or detect_llm_config()
    if not config:
        for f in facts:
            f.volatility = f.volatility or default_volatility(f)
        return facts

    facts_for_prompt = "\n".join(
        f"{i + 1}. [imp: {f.importance}] [type: {f.type}] [scope: {f.scope or 'unspecified'}] {f.text}"
        for i, f in enumerate(facts)
    )
    user_prompt = (
        f"Conversation context:\n{conversation_text}\n\n"
        f"Extracted facts:\n{facts_for_prompt}\n\n"
        f"Return {len(facts)} JSON objects, each with \"importance\" + \"volatility\". Match input order."
    )

    try:
        response = await chat_completion(config, COMPARATIVE_PROMPT_V1, user_prompt)
    except Exception as e:
        logger.warning("comparative_rescore_v1: chat_completion threw: %s", e)
        for f in facts:
            f.volatility = default_volatility(f)
        return facts

    if not response:
        for f in facts:
            f.volatility = default_volatility(f)
        return facts

    cleaned = response.strip()
    cleaned = re.sub(
        r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", cleaned, flags=re.IGNORECASE
    ).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    m = re.search(r"\[[\s\S]*\]", cleaned)
    if m:
        cleaned = m.group(0)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        for f in facts:
            f.volatility = default_volatility(f)
        return facts
    if not isinstance(parsed, list):
        for f in facts:
            f.volatility = default_volatility(f)
        return facts

    for i, f in enumerate(facts):
        entry = parsed[i] if i < len(parsed) and isinstance(parsed[i], dict) else {}
        raw_imp = entry.get("importance")
        raw_vol = str(entry.get("volatility", "")).lower()

        if isinstance(raw_imp, (int, float)) and not isinstance(raw_imp, bool):
            new_imp = max(5, min(10, round(float(raw_imp))))
            f.importance = int(new_imp)

        if raw_vol in VALID_MEMORY_VOLATILITIES:
            f.volatility = raw_vol
        elif f.volatility is None:
            f.volatility = default_volatility(f)

    return facts


# ---------------------------------------------------------------------------
# Phase 2.2.6: lexical importance bumps
# ---------------------------------------------------------------------------


def compute_lexical_importance_bump(fact_text: str, conversation_text: str) -> int:
    """Phase 2.2.6: post-process bump (0..2) for under-weighted facts.

    Mirrors ``computeLexicalImportanceBump`` in ``skill/plugin/extractor.ts``.
    See that function for the full signal rationale.
    """
    bump = 0
    lower_conv = conversation_text.lower()

    strong_intent = re.compile(
        r"\b(remember this|never forget|rule of thumb|don't (?:ever )?forget|critical|important|gotcha|note to self)\b",
        re.IGNORECASE,
    )
    if strong_intent.search(lower_conv):
        bump += 1

    double_excl = "!!"
    all_caps_phrase = re.compile(r"\b[A-Z]{3,}(?:\s+[A-Z]{3,}){2,}\b")
    if double_excl in conversation_text or all_caps_phrase.search(conversation_text):
        bump += 1

    lower_fact = fact_text.lower()
    stop_words = {
        "about", "after", "again", "against", "because", "before", "being",
        "between", "could", "doing", "during", "every", "further", "having",
        "their", "these", "those", "through", "under", "until", "where",
        "which", "while", "would", "should", "thing", "things", "something",
        "someone", "always", "never", "often", "still", "really", "maybe",
        "using", "works", "user", "users", "with", "from", "into", "like",
        "just", "than", "them", "they", "will", "when", "what", "were",
        "this", "that", "have",
    }
    fact_words = [
        w for w in re.split(r"[^a-z0-9_]+", lower_fact)
        if len(w) >= 5 and w not in stop_words
    ]
    triggered = False
    for word in fact_words:
        occurrences = len(re.findall(rf"\b{re.escape(word)}\b", lower_conv))
        if occurrences >= 2:
            triggered = True
            break
    if triggered:
        bump += 1

    return min(bump, 2)


# ---------------------------------------------------------------------------
# Bug #8 — in-batch cosine dedup (v2.0.2)
# ---------------------------------------------------------------------------


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Pure-Python cosine similarity. Falls back from Rust core."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


#: Cosine similarity threshold used by the store-time dedup. Mirrors
#: ``agent.lifecycle.STORE_DEDUP_THRESHOLD`` but kept local so this module
#: doesn't import from lifecycle (lifecycle imports extraction, not the
#: other way around). Keep in sync with lifecycle.
STORE_DEDUP_THRESHOLD = 0.85


def deduplicate_facts_by_embedding(
    facts: List[ExtractedFact],
    existing_memories: List[dict],
    threshold: float = STORE_DEDUP_THRESHOLD,
) -> List[ExtractedFact]:
    """Remove near-duplicate facts (cross-existing + in-batch).

    Bug #8 (v2.0.2): the prior store-time dedup in ``agent.lifecycle``
    only compared each new fact against the vault snapshot fetched ONCE
    before the store loop. Three near-identical facts produced in the
    same extraction batch would all persist because the loop's
    ``existing_memories`` list was never refreshed.

    This helper fixes that by running the dedup pass on the extracted
    fact list BEFORE it reaches the store loop. Each surviving fact also
    retains its embedding on ``fact._embedding`` so the lifecycle layer
    doesn't have to recompute.

    Rules:
      * ``UPDATE`` / ``DELETE`` actions bypass dedup (they operate on
        existing IDs and are intended to mutate the vault).
      * Facts without an embedding or a computable one are kept
        (we can't dedup without signal — be conservative).
      * A fact is dropped if its cosine similarity to any earlier
        surviving batch fact OR any ``existing_memories`` entry with an
        embedding exceeds ``threshold``.

    The first fact to appear wins — matches ``apply_mmr`` semantics
    and is deterministic for a given input order.
    """
    if not facts:
        return []

    # Filter existing_memories to those that carry usable embeddings.
    ex_with_emb = [
        m for m in (existing_memories or [])
        if isinstance(m, dict) and m.get("embedding") and len(m["embedding"]) > 0
    ]

    survivors: List[ExtractedFact] = []
    for fact in facts:
        # Pass-through for action types that bypass dedup.
        if fact.action in ("UPDATE", "DELETE", "NOOP"):
            survivors.append(fact)
            continue

        # Compute embedding lazily if not attached yet.
        embedding = fact._embedding
        if embedding is None:
            try:
                from totalreclaw.embedding import get_embedding
                embedding = get_embedding(fact.text)
                fact._embedding = embedding
            except Exception as e:
                logger.debug(
                    "deduplicate_facts_by_embedding: embedding failed for %r: %s",
                    fact.text[:40], e,
                )
                # No embedding → can't dedup → keep the fact.
                survivors.append(fact)
                continue

        if not embedding:
            survivors.append(fact)
            continue

        # Check against existing vault entries.
        is_dup = False
        for mem in ex_with_emb:
            sim = _cosine_similarity(embedding, mem["embedding"])
            if sim >= threshold:
                logger.info(
                    "deduplicate_facts_by_embedding: %r dropped (sim=%.3f to existing %s)",
                    fact.text[:60], sim, mem.get("id", "?"),
                )
                is_dup = True
                break
        if is_dup:
            continue

        # Check against earlier surviving batch facts.
        for earlier in survivors:
            if earlier._embedding is None:
                continue
            sim = _cosine_similarity(embedding, earlier._embedding)
            if sim >= threshold:
                logger.info(
                    "deduplicate_facts_by_embedding: %r dropped (sim=%.3f to batch-earlier %r)",
                    fact.text[:60], sim, earlier.text[:40],
                )
                is_dup = True
                break
        if is_dup:
            continue

        survivors.append(fact)

    return survivors


# ---------------------------------------------------------------------------
# Bug #9 — product-meta request filter (v2.0.2)
# ---------------------------------------------------------------------------

#: Hard-product markers: if the extracted fact text mentions setting up or
#: installing THE PRODUCT or one of its components, it's a meta-request and
#: should not persist as a "user preference". These are case-insensitive.
_PRODUCT_META_NAME_PATTERNS = (
    r"\btotalreclaw\b",
    r"\btotal reclaw\b",
    r"\btotal-reclaw\b",
    r"\bhermes\s+plugin\b",
    r"\bmemory\s+plugin\b",
    r"\bmcp\s+memory\b",
    r"\bopenclaw\b",
)

#: Setup-action phrases combined with meta/plugin/memory context.
_PRODUCT_META_ACTION_PATTERNS = (
    r"\bset(\s+)?up\b.*\b(memory|vault|plugin|totalreclaw|product)\b",
    r"\b(install|configure)\b.*\b(memory|vault|plugin|totalreclaw)\b",
    r"\b(the\s+)?(memory|vault)\s+plugin\b",
    r"\bi\s+want\s+(encrypted\s+)?memory\s+across\s+my\s+(ai|agents|tools)\b",
)


def is_product_meta_request(text: str) -> bool:
    """Return True if ``text`` is about setting up / asking for the product.

    Used to filter spurious extractions where a setup prompt (e.g.
    "I want encrypted memory across my AI tools") lands in the vault as
    a "user preference". Matches the QA-reported phrase exactly while
    letting genuine preferences ("I like encrypted tools",
    "I prefer Signal because it's encrypted") pass through.

    Heuristic:
      1. Any mention of the product by name → meta.
      2. Setup verbs ("set up", "install", "configure") combined with
         "memory" / "vault" / "plugin" / "agent" targets → meta.
      3. The specific QA-reported "I want (encrypted) memory across my
         AI tools" idiom → meta.
    """
    if not isinstance(text, str) or not text:
        return False
    lower = text.lower().strip()

    for pat in _PRODUCT_META_NAME_PATTERNS:
        if re.search(pat, lower):
            return True

    for pat in _PRODUCT_META_ACTION_PATTERNS:
        if re.search(pat, lower, flags=re.IGNORECASE):
            return True

    return False


def _filter_product_meta_facts(facts: List[ExtractedFact]) -> List[ExtractedFact]:
    """Drop facts whose text reads as a product-meta / setup request.

    Bug #9 (v2.0.2). Runs as a final pass before the extracted facts are
    returned. Meta-requests are logged at INFO so operators can verify
    the filter isn't being over-aggressive.
    """
    kept: List[ExtractedFact] = []
    for f in facts:
        if is_product_meta_request(f.text):
            logger.info(
                "_filter_product_meta_facts: dropping meta-request fact: %r",
                f.text[:80],
            )
            continue
        kept.append(f)
    return kept


# ---------------------------------------------------------------------------
# Main extraction entry points
# ---------------------------------------------------------------------------


async def extract_facts_llm(
    messages: list[dict],
    mode: str = "turn",  # "turn" or "full"
    existing_memories: Optional[list[dict]] = None,
    llm_config: Optional[LLMConfig] = None,
) -> list[ExtractedFact]:
    """Extract facts using the v1 G-pipeline. Returns [] if no LLM available.

    Pipeline: single merged-topic LLM call → ``apply_provenance_filter_lax``
    → ``comparative_rescore_v1`` (forces re-rank when >= 5 facts) →
    ``default_volatility`` fallback → lexical importance bumps →
    product-meta filter (Bug #9) → in-batch embedding dedup (Bug #8).

    Parameters
    ----------
    messages : list[dict]
        Conversation turns (role + content).
    mode : {"turn", "full"}
        Only affects the user-prompt framing (turn vs full-session).
    existing_memories : list[dict], optional
        Used for LLM-side dedup context AND by the new cosine dedup pass
        (Bug #8). Entries need ``id``, ``text``, and ``embedding``.
    llm_config : LLMConfig, optional
        Pre-resolved LLM config (e.g. from Hermes). Falls back to env-var
        detection via :func:`detect_llm_config` when not provided.
    """
    chat_config = llm_config or detect_llm_config()
    if not chat_config:
        # Bug #5: loud, actionable warning — no more silent extraction disable.
        logger.warning(
            "TotalReclaw extraction disabled: no LLM config resolved. "
            "Hermes users should configure ~/.hermes/config.yaml + ~/.hermes/.env; "
            "other agents can set OPENAI_MODEL + OPENAI_API_KEY (or an equivalent "
            "provider pair). See docs/guides/env-vars-reference.md."
        )
        return []

    # rc.24 F2 — same-provider cheap-model selection (Pedro 2026-04-26).
    # The user's chat model is often a flagship (GLM-5.1, GPT-4, Sonnet)
    # that's both expensive and — in the GLM-5.1-on-Coding case — known
    # to silently return empty content for the merged-extraction prompt.
    # Swap to the same-provider cheap workhorse (GLM-4.5-flash, gpt-4o-mini,
    # claude-haiku-4-5, etc.). Same key, same provider, same auth surface;
    # only model + (for zai) base URL change. Operator override:
    # ``TOTALRECLAW_EXTRACTION_MODEL``.
    config = derive_extraction_config(chat_config)

    if not messages:
        logger.info("extract_facts_llm: no messages to process")
        return []

    # 'turn' mode trims to the last 6 messages (matches TS extractFacts).
    relevant = messages[-6:] if mode == "turn" else messages
    conversation_text = _truncate_messages(relevant)
    if len(conversation_text) < 20:
        logger.info(
            "extract_facts_llm: conversation too short (%d chars < 20, messages=%d, mode=%s)",
            len(conversation_text),
            len(messages),
            mode,
        )
        return []

    memories_ctx = ""
    if existing_memories:
        mem_lines = [f"[ID: {m['id']}] {m['text']}" for m in existing_memories[:50]]
        memories_ctx = (
            "\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n"
            + "\n".join(mem_lines)
        )

    user_prompt = (
        f"Extract important facts from these recent conversation turns:\n\n{conversation_text}{memories_ctx}"
        if mode == "turn"
        else f"Extract ALL valuable long-term memories from this conversation before it is lost:\n\n{conversation_text}{memories_ctx}"
    )

    try:
        response = await chat_completion(config, EXTRACTION_SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        logger.warning("extract_facts_llm: chat_completion threw: %s", e)
        return []

    if not response:
        # F2 (rc.24) — bumped from INFO to WARNING. An empty extraction
        # response is a CORRECTNESS bug, not a routine event: the LLM
        # accepted the call without raising, but content was empty —
        # this is what surfaced in the rc.23 QA report as 100% silent
        # auto-extraction failure on Z.AI/GLM-5.1. ``chat_completion``
        # already logs the underlying request shape (finish_reason,
        # usage, response_format) at WARN; this line tells the operator
        # WHERE the empty bubbled up so they can correlate the two.
        logger.warning(
            "extract_facts_llm: chat_completion returned None/empty — "
            "auto-extraction will be a no-op for this turn. "
            "See preceding 'LLM returned empty content' log for the "
            "underlying response shape (finish_reason / usage)."
        )
        return []

    logger.info(
        "extract_facts_llm: LLM returned %d chars; parsing merged response",
        len(response),
    )
    topics, raw_facts = parse_merged_response_v1(response)
    if topics:
        logger.info("extract_facts_llm: topics = %s", topics)

    # Provenance filter (tag-don't-drop).
    facts = apply_provenance_filter_lax(raw_facts, conversation_text)

    # Comparative rescore — forces re-rank when >= 5 facts.
    facts = await comparative_rescore_v1(facts, conversation_text, llm_config=config)

    # Defensive: ensure every fact has a volatility.
    for f in facts:
        if f.volatility is None:
            f.volatility = default_volatility(f)

    # Lexical importance bumps.
    for f in facts:
        bump = compute_lexical_importance_bump(f.text, conversation_text)
        if bump > 0:
            old = f.importance
            effective = min(bump, 1) if f.importance >= 8 else bump
            f.importance = min(10, f.importance + effective)
            logger.info(
                "extract_facts_llm: lexical bump +%d for %r (%d -> %d)",
                bump, f.text[:60], old, f.importance,
            )

    # Bug #9: drop product-meta / setup requests before they hit the vault.
    facts = _filter_product_meta_facts(facts)

    # Bug #8: collapse near-identical facts within the batch, and against
    # existing vault entries with embeddings. Runs last so importance
    # bumps are already applied (the surviving fact wins with its final
    # importance). Falls through gracefully when ``existing_memories`` is
    # empty or facts have no computable embeddings.
    facts = deduplicate_facts_by_embedding(
        facts, existing_memories or [], threshold=STORE_DEDUP_THRESHOLD,
    )

    return facts


async def extract_facts_compaction(
    messages: list[dict],
    existing_memories: Optional[list[dict]] = None,
    llm_config: Optional[LLMConfig] = None,
) -> list[ExtractedFact]:
    """Compaction-aware extraction (importance floor 5, not 6).

    Uses :data:`COMPACTION_SYSTEM_PROMPT` and always processes the full
    conversation. Mirrors ``extractFactsForCompaction`` in the TS plugin.
    """
    chat_config = llm_config or detect_llm_config()
    if not chat_config:
        # Bug #5: loud, actionable warning — no more silent extraction disable.
        logger.warning(
            "TotalReclaw compaction extraction disabled: no LLM config resolved. "
            "Hermes users should configure ~/.hermes/config.yaml + ~/.hermes/.env; "
            "other agents can set OPENAI_MODEL + OPENAI_API_KEY (or equivalent). "
            "See docs/guides/env-vars-reference.md."
        )
        return []

    # rc.24 F2 — same-provider cheap-model selection (parity with
    # extract_facts_llm above). Compaction is a STRUCTURED-OUTPUT call
    # exactly like turn-mode extraction, so the same cheap-pick rule
    # applies. See ``derive_extraction_config`` for the mapping.
    config = derive_extraction_config(chat_config)

    conversation_text = _truncate_messages(messages)
    if len(conversation_text) < 20:
        logger.info(
            "extract_facts_compaction: conversation too short (%d chars < 20, messages=%d)",
            len(conversation_text),
            len(messages),
        )
        return []

    memories_ctx = ""
    if existing_memories:
        mem_lines = [f"[ID: {m['id']}] {m['text']}" for m in existing_memories[:50]]
        memories_ctx = (
            "\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n"
            + "\n".join(mem_lines)
        )

    user_prompt = (
        f"Extract ALL valuable long-term memories from this conversation before it is compacted and lost:\n\n"
        f"{conversation_text}{memories_ctx}"
    )

    try:
        response = await chat_completion(config, COMPACTION_SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        logger.warning("extract_facts_compaction: chat_completion threw: %s", e)
        return []

    if not response:
        # F2 (rc.24) — bumped from INFO to WARNING for the same reason
        # as ``extract_facts_llm`` above. Compaction is the LAST chance
        # to capture facts before the conversation is collapsed; a
        # silent empty-response here is a hard data-loss event.
        logger.warning(
            "extract_facts_compaction: chat_completion returned None/empty — "
            "compaction extraction will be a no-op for this conversation. "
            "See preceding 'LLM returned empty content' log for the "
            "underlying response shape."
        )
        return []

    logger.info(
        "extract_facts_compaction: LLM returned %d chars; parsing merged response",
        len(response),
    )
    facts = parse_facts_response_for_compaction(response)

    # Provenance filter — same tag-don't-drop, importance floor 5 baked in.
    facts = apply_provenance_filter_lax(facts, conversation_text)

    # Comparative rescore (same trigger as regular extraction).
    facts = await comparative_rescore_v1(facts, conversation_text, llm_config=config)

    # Defensive: ensure every fact has a volatility.
    for f in facts:
        if f.volatility is None:
            f.volatility = default_volatility(f)

    # Lexical importance bumps.
    for f in facts:
        bump = compute_lexical_importance_bump(f.text, conversation_text)
        if bump > 0:
            old = f.importance
            effective = min(bump, 1) if f.importance >= 8 else bump
            f.importance = min(10, f.importance + effective)
            logger.info(
                "extract_facts_compaction: lexical bump +%d for %r (%d -> %d)",
                bump, f.text[:60], old, f.importance,
            )

    # Bug #9: filter product-meta requests.
    facts = _filter_product_meta_facts(facts)

    # Bug #8: in-batch + cross-existing cosine dedup.
    facts = deduplicate_facts_by_embedding(
        facts, existing_memories or [], threshold=STORE_DEDUP_THRESHOLD,
    )

    return facts


# ---------------------------------------------------------------------------
# Heuristic fallback (no LLM required)
# ---------------------------------------------------------------------------


def extract_facts_heuristic(messages: list[dict], max_facts: int) -> list[ExtractedFact]:
    """Simple heuristic extraction (no LLM needed).

    Used only when the LLM path is unavailable. Emits v1-shaped facts with
    ``source="user"`` (patterns all match on the user's own turns) and a
    best-guess v1 type.
    """
    facts: list[ExtractedFact] = []
    # (pattern, v1 type, importance)
    patterns: list[tuple[str, str, int]] = [
        (r"(?:I|my)\s+(?:prefer|like|want|need|use|enjoy|love|hate)\s+(.+)", "preference", 7),
        (r"(?:my name is|I'm called|call me)\s+(.+)", "claim", 8),
        (r"(?:remember|don't forget|keep in mind)\s+(?:that\s+)?(.+)", "claim", 7),
        (r"(?:I chose|I decided|we decided|decision:)\s+(.+)", "claim", 7),
        (r"(?:I work|I'm a|my job|my role)\s+(.+)", "claim", 7),
    ]
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        for pattern, fact_type, importance in patterns:
            for match in re.findall(pattern, content, re.IGNORECASE):
                text = match.strip().rstrip(".")
                if len(text) > 10 and len(facts) < max_facts:
                    facts.append(
                        ExtractedFact(
                            text=text[:512],
                            type=fact_type,
                            importance=importance,
                            action="ADD",
                            source="user",
                            scope="unspecified",
                            volatility="updatable",
                        )
                    )
    return facts


# ---------------------------------------------------------------------------
# Back-compat shims — renamed parsers. Prefer the new names in new code.
# ---------------------------------------------------------------------------


def _parse_response(response: str) -> list[ExtractedFact]:
    """Deprecated: use :func:`parse_facts_response` instead."""
    return parse_facts_response(response)


def _parse_response_compaction(response: str) -> list[ExtractedFact]:
    """Deprecated: use :func:`parse_facts_response_for_compaction`."""
    return parse_facts_response_for_compaction(response)
