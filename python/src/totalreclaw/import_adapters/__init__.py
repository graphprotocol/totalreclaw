"""
Import adapters for TotalReclaw -- parse data from external memory sources.

Agent-agnostic module: any Python-based AI agent (Hermes, future agents) can use it.

Usage:
    from totalreclaw.import_adapters import get_adapter

    adapter = get_adapter('gemini')
    result = adapter.parse(file_path='~/takeout/My Activity.html')
    # result.chunks -> conversation chunks for LLM extraction
    # result.facts -> pre-structured facts (for structured sources)
"""

from .types import (
    ImportSource,
    NormalizedFact,
    ConversationChunk,
    AdapterParseResult,
    BatchImportResult,
)
from .base_adapter import BaseImportAdapter
from .gemini_adapter import GeminiAdapter
from .chatgpt_adapter import ChatGPTAdapter
from .claude_adapter import ClaudeAdapter
from .mem0_adapter import Mem0Adapter


_ADAPTERS: dict[str, type[BaseImportAdapter]] = {
    'gemini': GeminiAdapter,
    'chatgpt': ChatGPTAdapter,
    'claude': ClaudeAdapter,
    'mem0': Mem0Adapter,
}


def get_adapter(source: str) -> BaseImportAdapter:
    """
    Get an import adapter by source name.

    Currently supported: 'gemini', 'chatgpt', 'claude', 'mem0'.
    (MCP Memory adapter to be ported from TypeScript in Phase B.)

    Raises ValueError if the source is not supported.
    """
    cls = _ADAPTERS.get(source)
    if cls is None:
        supported = ', '.join(sorted(_ADAPTERS.keys()))
        raise ValueError(f"Unknown import source '{source}'. Supported: {supported}")
    return cls()


def list_sources() -> list[str]:
    """Return the list of supported import source names."""
    return sorted(_ADAPTERS.keys())


__all__ = [
    'get_adapter',
    'list_sources',
    'BaseImportAdapter',
    'GeminiAdapter',
    'ChatGPTAdapter',
    'ClaudeAdapter',
    'Mem0Adapter',
    'ImportSource',
    'NormalizedFact',
    'ConversationChunk',
    'AdapterParseResult',
    'BatchImportResult',
]
