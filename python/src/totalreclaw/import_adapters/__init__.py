"""Back-compat shim: import adapters now live in ``totalreclaw.imports.adapters``.

Re-exports the public adapter API from its new home so existing
``from totalreclaw.import_adapters import get_adapter`` imports keep working,
and aliases each adapter submodule in ``sys.modules`` so submodule imports
(``from totalreclaw.import_adapters.types import ...``) and their patch()
targets keep working too. New code should use ``totalreclaw.imports.adapters``.
"""

import sys as _sys

from totalreclaw.imports import adapters as _adapters
from totalreclaw.imports.adapters import (
    base_adapter as _base_adapter,
    chatgpt_adapter as _chatgpt_adapter,
    claude_adapter as _claude_adapter,
    gemini_adapter as _gemini_adapter,
    mem0_adapter as _mem0_adapter,
    types as _types,
)

for _name, _mod in (
    ("base_adapter", _base_adapter),
    ("chatgpt_adapter", _chatgpt_adapter),
    ("claude_adapter", _claude_adapter),
    ("gemini_adapter", _gemini_adapter),
    ("mem0_adapter", _mem0_adapter),
    ("types", _types),
):
    _sys.modules[f"{__name__}.{_name}"] = _mod

from totalreclaw.imports.adapters import (
    get_adapter,
    list_sources,
    BaseImportAdapter,
    GeminiAdapter,
    ChatGPTAdapter,
    ClaudeAdapter,
    Mem0Adapter,
    ImportSource,
    NormalizedFact,
    ConversationChunk,
    AdapterParseResult,
    BatchImportResult,
)

__all__ = [
    "get_adapter",
    "list_sources",
    "BaseImportAdapter",
    "GeminiAdapter",
    "ChatGPTAdapter",
    "ClaudeAdapter",
    "Mem0Adapter",
    "ImportSource",
    "NormalizedFact",
    "ConversationChunk",
    "AdapterParseResult",
    "BatchImportResult",
]
