"""
Mem0 import adapter — parses a Mem0 export JSON or API response.

Mem0 is a pre-structured source: each memory in the export is already
an atomic fact, so the adapter emits :class:`NormalizedFact` objects
directly rather than :class:`ConversationChunk` chunks. The
``ImportEngine`` then stores them via ``client.remember`` without any
LLM re-extraction.

Ported from ``skill/plugin/import-adapters/mem0-adapter.ts`` (233 LOC).
The structural parts — shape detection, category mapping, validation —
are preserved 1:1 so the Python adapter behaves identically to the TS
adapter for the same export file. The TS adapter also has an optional
API-fetch path (``fetchFromApi``); that is intentionally *omitted* here
for Phase A — users export JSON from Mem0 and paste or point-to it,
which is the 95% case. If a user ever needs live API ingestion we can
add it in a follow-up (TODO flagged in README).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult


# Map Mem0 category strings to TotalReclaw v0 fact types (the BaseImportAdapter
# v0 → v1 coercion happens downstream in client.remember). Parity with the TS
# ``CATEGORY_MAP`` constant in mem0-adapter.ts.
CATEGORY_MAP: Dict[str, str] = {
    'preference': 'preference',
    'preferences': 'preference',
    'like': 'preference',
    'dislike': 'preference',
    'fact': 'fact',
    'personal': 'fact',
    'biographical': 'fact',
    'decision': 'decision',
    'goal': 'goal',
    'objective': 'goal',
    'experience': 'episodic',
    'event': 'episodic',
    'memory': 'episodic',
}


class Mem0Adapter(BaseImportAdapter):
    """Adapter for Mem0 (mem0.ai) export JSON."""

    source = 'mem0'
    display_name = 'Mem0'

    def parse(
        self,
        *,
        content: Optional[str] = None,
        file_path: Optional[str] = None,
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        warnings: List[str] = []
        errors: List[str] = []

        # Resolve input: content > file_path > error.
        raw: Optional[str] = None
        if content:
            raw = content
        elif file_path:
            try:
                resolved = os.path.expanduser(file_path)
                with open(resolved, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception as e:
                errors.append(f'Failed to read file: {e}')
                return AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
        else:
            errors.append(
                'Mem0 import requires either content (pasted export JSON) '
                'or file_path. Export from Mem0: dashboard → Settings → '
                'Export Memories, or paste an API response.',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        memories = self._parse_export_content(raw, errors)

        if on_progress:
            on_progress({
                'current': 0,
                'total': len(memories),
                'phase': 'parsing',
                'message': f'Parsing {len(memories)} Mem0 memories...',
            })

        # Convert to partial fact dicts for BaseImportAdapter.validate_facts.
        raw_facts: List[dict] = []
        for mem in memories:
            if not isinstance(mem, dict):
                continue
            text = mem.get('memory', '')
            categories = mem.get('categories') or []
            # Older Mem0 exports stored the category under metadata.category
            # instead of the top-level categories array. Support both.
            md = mem.get('metadata') or {}
            md_category = md.get('category') if isinstance(md, dict) else None
            fact_type = self._map_category(categories, md_category)

            # Timestamp: prefer updated_at, fall back to created_at.
            ts: Optional[str] = None
            if isinstance(md, dict):
                ts = md.get('updated_at') or md.get('created_at')

            # Tag list mirrors what the TS adapter emits (categories array).
            tags = [c for c in categories if isinstance(c, str)]

            raw_facts.append({
                'text': text,
                'type': fact_type,
                # Mem0 does not provide an importance score — default to 6 so
                # the fact passes the importance >= 6 downstream filter. Parity
                # with the TS adapter.
                'importance': 6,
                'source_id': mem.get('id'),
                'source_timestamp': ts,
                'tags': tags,
            })

        facts, invalid_count = self.validate_facts(raw_facts)

        if invalid_count > 0:
            warnings.append(
                f'{invalid_count} memories had invalid/empty text and were skipped'
            )

        return AdapterParseResult(
            facts=facts,
            chunks=[],
            total_messages=0,
            warnings=warnings,
            errors=errors,
            source_metadata={
                'total_from_source': len(memories),
                'format': 'mem0-json',
            },
        )

    # ── Internal ─────────────────────────────────────────────────────────

    def _parse_export_content(
        self,
        content: str,
        errors: List[str],
    ) -> List[dict]:
        """Parse a Mem0 export string into a list of memory dicts.

        Accepts three shapes (parity with the TS adapter):
          1. Export file format ``{memories: [...]}`` (the dashboard export).
          2. API response format ``{results: [...]}`` (live API call).
          3. Bare JSON array of memories.
        """
        try:
            data: Any = json.loads(content.strip())
        except json.JSONDecodeError as e:
            errors.append(f'Failed to parse Mem0 JSON: {e}')
            return []

        # Dashboard export.
        if isinstance(data, dict) and isinstance(data.get('memories'), list):
            return data['memories']
        # API response.
        if isinstance(data, dict) and isinstance(data.get('results'), list):
            return data['results']
        # Bare array.
        if isinstance(data, list):
            return data

        errors.append(
            'Unrecognized Mem0 format. Expected {memories: [...]}, '
            '{results: [...]}, or a bare array.',
        )
        return []

    def _map_category(
        self,
        categories: List[str],
        single_category: Optional[str],
    ) -> str:
        """Resolve a Mem0 category → TotalReclaw v0 fact type.

        Checks the ``categories`` array first, then the ``metadata.category``
        fallback. Unknown categories default to ``fact`` (the safest, most
        neutral type — v0 ``fact`` coerces to v1 ``claim`` downstream).
        """
        all_categories: List[str] = list(categories) if categories else []
        if single_category:
            all_categories.append(single_category)

        for cat in all_categories:
            if not isinstance(cat, str):
                continue
            mapped = CATEGORY_MAP.get(cat.lower())
            if mapped:
                return mapped

        return 'fact'
