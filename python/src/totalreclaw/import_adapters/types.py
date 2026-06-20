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
    type: Literal['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary']
    importance: int  # 1-10
    source: ImportSource
    source_id: Optional[str] = None
    source_timestamp: Optional[str] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class ConversationChunk:
    """A chunk of conversation messages for LLM-based fact extraction."""
    title: str
    messages: List[dict]  # [{"role": "user"|"assistant", "text": "..."}]
    timestamp: Optional[str] = None  # ISO 8601


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
    # Smart-import (imp-4): chunks the triage pass marked SKIP for this
    # batch, plus a snapshot of the profile+triage pipeline that ran for
    # the import (None when smart-import didn't run — no LLM, fact-only
    # sources, or older core wheels). Keys mirror the plugin's
    # ``smart_import`` payload shape (extract_count / skip_count /
    # profile_duration_ms).
    chunks_skipped: int = 0
    smart_import: Optional[dict] = None
    # Per-chunk "0 facts" diagnostics (issue #389 follow-up): one
    # ``{index, title, reason}`` dict per chunk that produced 0 storable facts
    # (excluding triage-skips and exceptions, which have their own reporting).
    # ``reason`` ∈ {extractor_empty, filtered_importance, filtered_text,
    # filtered, malformed}. None when every chunk yielded facts. See
    # ``import_engine._classify_zero_fact_reason``.
    chunk_diagnostics: Optional[List[dict]] = None
