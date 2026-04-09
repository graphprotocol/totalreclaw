"""
Types for import adapters.

Ported from skill/plugin/import-adapters/types.ts
"""

from dataclasses import dataclass, field
from typing import List, Optional, Literal


ImportSource = Literal[
    'mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini',
    'memoclaw', 'generic-json', 'generic-csv',
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
