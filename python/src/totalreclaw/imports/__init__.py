"""
Import subsystem for TotalReclaw.

Consolidates the full import capability into one package:

  - ``totalreclaw.imports.adapters`` -- per-source parsers (gemini, chatgpt,
    claude, mem0) plus the shared adapter types.
  - ``totalreclaw.imports.engine``   -- the batch import orchestrator
    (``ImportEngine``): parse -> batch -> extract -> embed -> store.
  - ``totalreclaw.imports.state``    -- on-disk import checkpoint + the
    imported-conversation registry.
  - ``totalreclaw.imports.smart``    -- smart-import (semantic segmentation)
    pipeline used by the engine.

The pre-consolidation module paths (``totalreclaw.import_engine``,
``totalreclaw.import_state``, ``totalreclaw._smart_import``,
``totalreclaw.import_adapters``) remain importable as back-compat shims.
"""

from .engine import ImportEngine
from .adapters import (
    get_adapter,
    list_sources,
    ImportSource,
    NormalizedFact,
    ConversationChunk,
    AdapterParseResult,
    BatchImportResult,
)

__all__ = [
    "ImportEngine",
    "get_adapter",
    "list_sources",
    "ImportSource",
    "NormalizedFact",
    "ConversationChunk",
    "AdapterParseResult",
    "BatchImportResult",
]
