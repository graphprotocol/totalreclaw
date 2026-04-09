"""
Abstract base class for import adapters.

Adapters are PARSERS only -- they convert raw export data into either:
- Pre-structured facts (Mem0, MCP Memory -- facts are already atomic)
- Conversation chunks (ChatGPT, Claude, Gemini -- need LLM extraction)

The caller (import tool) handles LLM extraction, encryption, and storage.

Ported from skill/plugin/import-adapters/base-adapter.ts
"""

from abc import ABC, abstractmethod
from typing import Optional, Callable, List

from .types import AdapterParseResult, NormalizedFact


class BaseImportAdapter(ABC):
    """Abstract base for all import adapters."""

    source: str
    display_name: str

    @abstractmethod
    def parse(
        self,
        *,
        content: Optional[str] = None,
        file_path: Optional[str] = None,
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        """
        Parse source data into normalized facts or conversation chunks.

        For file sources, provide either content (string) or file_path.
        """
        ...

    def validate_fact(self, fact: dict) -> Optional[NormalizedFact]:
        """
        Validate and clean a single fact dict.
        Returns None if the fact should be skipped.
        """
        text = (fact.get('text') or '').strip()
        if len(text) < 3:
            return None

        # Truncate to 512 chars
        text = text[:512]

        # Normalize type
        valid_types = ('fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary')
        fact_type = fact.get('type', 'fact')
        if fact_type not in valid_types:
            fact_type = 'fact'

        # Normalize importance to 1-10
        importance = fact.get('importance', 5)
        if isinstance(importance, float) and importance <= 1:
            # 0-1 scale -- convert to 1-10
            importance = max(1, round(importance * 10))
        importance = max(1, min(10, int(importance)))

        return NormalizedFact(
            text=text,
            type=fact_type,
            importance=importance,
            source=self.source,
            source_id=fact.get('source_id'),
            source_timestamp=fact.get('source_timestamp'),
            tags=fact.get('tags', []),
        )

    def validate_facts(self, raw_facts: List[dict]) -> tuple:
        """
        Batch-validate an array of partial fact dicts.
        Returns (facts, invalid_count).
        """
        facts: List[NormalizedFact] = []
        invalid_count = 0

        for raw in raw_facts:
            validated = self.validate_fact(raw)
            if validated:
                facts.append(validated)
            else:
                invalid_count += 1

        return facts, invalid_count
