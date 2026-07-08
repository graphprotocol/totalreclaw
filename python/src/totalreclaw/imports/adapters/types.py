"""
Types for import adapters.

Ported from skill/plugin/import-adapters/types.ts
"""

from dataclasses import dataclass, field
from typing import List, Optional, Literal


ImportSource = Literal[
    'mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini',
]


@dataclass
class NormalizedFact:
    """Normalized fact -- the common format all adapters produce."""
    text: str
    # v1 memory taxonomy — MUST match ``totalreclaw.memory_types.VALID_MEMORY_TYPES``.
    # Adapters coerce any legacy v0 token to v1 at the boundary
    # (``BaseImportAdapter.validate_fact`` via ``normalize_to_v1_type``) so this
    # public surface never leaks v0 vocabulary to ``client.remember``.
    type: Literal['claim', 'preference', 'directive', 'commitment', 'episode', 'summary']
    importance: int  # 1-10
    source: ImportSource
    source_id: Optional[str] = None
    source_timestamp: Optional[str] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class ConversationChunk:
    """A chunk of conversation messages for LLM-based fact extraction."""
    title: str
    messages: List[dict]  # [{"role": "user"|"assistant", "text": "...", "timestamp": ISO?}]
    timestamp: Optional[str] = None  # ISO 8601
    #: Explicit conversation boundary from the source export. ChatGPT/Claude
    #: conversations have first-class ids; Gemini Takeout does not. When set,
    #: the import engine groups all chunks sharing an id into one session and
    #: skips semantic (centroid-walk) segmentation.
    conversation_id: Optional[str] = None


@dataclass
class ParsedTurn:
    """A single conversation turn (one prompt→reply exchange) with its OWN
    timestamp, in chronological order.

    Mirrors ``totalreclaw_core``'s ``ParsedTurn`` (import_parsers.rs, #368 Part
    2). Exposed by conversation adapters (Gemini) so the import engine can run
    true turn-granularity semantic session segmentation (``segment_sessions``)
    over real per-turn timestamps + text, instead of re-deriving turns from
    chunks and approximating every turn's time with its chunk's timestamp.

    ``chunk_index`` maps the turn back to the chunk that holds its first message
    (chunks are the extraction unit), computed authoritatively by core.
    """
    user_text: str
    assistant_text: str
    #: Combined ``user_text`` + ``assistant_text`` (newline-joined, non-empty
    #: sides only) — the canonical string to embed for segmentation.
    text: str
    chunk_index: int
    #: RFC3339/ISO-8601 UTC timestamp, or None when the export carried none.
    ts_iso: Optional[str] = None
    #: Unix seconds, or None when unknown (None ≠ epoch-0 — segmentation treats
    #: None as a 0-gap to the previous turn).
    ts_unix: Optional[float] = None
    #: Message index range ``[chunk_msg_start, chunk_msg_end)`` of this turn
    #: WITHIN its ``chunk_index`` chunk (#368 Part 2, straddle fidelity). Lets the
    #: engine split a chunk whose turns straddle a session boundary into
    #: per-session sub-chunks so each fact lands in the right session. Both None
    #: on older core wheels that predate the range fields — the engine then falls
    #: back to whole-chunk assignment (no straddle-splitting).
    chunk_msg_start: Optional[int] = None
    chunk_msg_end: Optional[int] = None


@dataclass
class AdapterParseResult:
    """
    Adapter parse result -- returned by each adapter's parse method.

    Adapters return EITHER `facts` (pre-structured sources like Mem0, MCP Memory)
    OR `chunks` (conversation-based sources like ChatGPT, Claude) that need
    LLM extraction. The caller checks which field is populated.
    """
    facts: List[NormalizedFact]
    chunks: List[ConversationChunk]
    total_messages: int
    warnings: List[str]
    errors: List[str]
    source_metadata: Optional[dict] = None
    #: Flat, chronological per-turn view with real per-turn timestamps (#368
    #: Part 2). Populated by conversation adapters that recover per-turn times
    #: (Gemini MyActivity/HTML); empty for pre-structured (Mem0) or timestamp-less
    #: (Gemini "Saved info" paste) sources. When present, the import engine uses
    #: it for turn-granularity segmentation instead of the chunk approximation.
    turns: List[ParsedTurn] = field(default_factory=list)


@dataclass
class BatchImportResult:
    """Result of a batch import operation."""
    success: bool
    batch_offset: int
    batch_size: int
    chunks_processed: int
    total_chunks: int
    facts_extracted: int
    facts_stored: int
    remaining_chunks: int
    is_complete: bool
    errors: List[str] = field(default_factory=list)
    duration_ms: int = 0
    # #422 — duplicates skipped by client-side pre-write dedup (cross-vault
    # fingerprint lookup + intra-call text dedup). 0 when dedup found nothing.
    dups_skipped: int = 0
    # Smart-import (imp-4): chunks the triage pass marked SKIP for this
    # batch, plus a snapshot of the profile+triage pipeline that ran for
    # the import (None when smart-import didn't run — no LLM, fact-only
    # sources, or older core wheels). Keys mirror the plugin's
    # ``smart_import`` payload shape (extract_count / skip_count /
    # profile_duration_ms).
    chunks_skipped: int = 0
    # #436 — conversations dropped BEFORE extraction because they are already
    # in the per-source imported-conversation registry (re-import guard). Each
    # distinct dropped conversation is counted once. 0 for fresh imports and
    # for Gemini (which carries no conversation_id).
    conversations_skipped: int = 0
    # #457 accounting: DERIVED facts stored beyond the LLM-extracted atomic
    # facts — one session Crystal (type=summary) per multi-turn conversation.
    # These count in ``facts_stored`` but not ``facts_extracted``, so QA saw
    # facts_stored (79) > facts_extracted (72). Surfacing this reconciles the
    # numbers: facts_stored ≈ facts_extracted + derived_facts − dups_skipped.
    derived_facts: int = 0
    smart_import: Optional[dict] = None
    # Per-chunk "0 facts" diagnostics (issue #389 follow-up): one
    # ``{index, title, reason}`` dict per chunk that produced 0 storable facts
    # (excluding triage-skips and exceptions, which have their own reporting).
    # ``reason`` ∈ {extractor_empty, filtered_importance, filtered_text,
    # filtered, malformed}. None when every chunk yielded facts. See
    # ``import_engine._classify_zero_fact_reason``.
    chunk_diagnostics: Optional[List[dict]] = None
