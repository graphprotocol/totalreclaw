"""
Claude import adapter -- parses plain text memories (one per line, optional date prefix).

Ported from skill/plugin/import-adapters/claude-adapter.ts
"""

import os
import re
from typing import Optional, Callable, List

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult, ConversationChunk


# Pattern for lines that start with a date prefix.
# Claude memory entries sometimes have: [2026-03-15] - User prefers TypeScript
DATE_PREFIX_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2})\]\s*[-:]\s*')

# Pattern for bullet-prefixed lines.
BULLET_PREFIX_RE = re.compile(r'^[-*\u2022]\s+')

# Pattern for numbered list lines.
NUMBERED_PREFIX_RE = re.compile(r'^\d+[.)]\s+')

# Maximum messages per conversation chunk for LLM extraction.
CHUNK_SIZE = 20


class ClaudeAdapter(BaseImportAdapter):
    source = 'claude'
    display_name = 'Claude'

    def parse(
        self,
        *,
        content: Optional[str] = None,
        file_path: Optional[str] = None,
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        warnings: List[str] = []
        errors: List[str] = []

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
                'Claude import requires either content (pasted text) or file_path. '
                'Copy your memories from Claude: Settings -> Memory -> select all and copy.',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        # Claude memory export is plain text, one fact per line.
        return self._parse_memories_text(raw.strip(), warnings, errors, on_progress)

    def _parse_memories_text(
        self,
        content: str,
        warnings: List[str],
        errors: List[str],
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        """
        Parse Claude memories -- plain text, one memory per line.
        Returns conversation chunks for LLM extraction (no pattern matching).

        Each line is cleaned (date prefixes, bullets, numbers stripped) and
        grouped into chunks for the LLM to process.
        """
        # Split by newlines and filter
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        # Skip common header lines
        header_re = re.compile(
            r'^(?:memories?|claude memories?|my memories?|saved memories?):?\s*$',
            re.IGNORECASE,
        )
        lines = [line for line in lines if not header_re.match(line)]

        if on_progress:
            on_progress({
                'current': 0,
                'total': len(lines),
                'phase': 'parsing',
                'message': f'Parsing {len(lines)} Claude memories...',
            })

        # Clean each line: extract date, strip formatting
        cleaned_entries: List[dict] = []
        for line in lines:
            cleaned = line
            timestamp = None

            # Extract date prefix if present
            date_match = DATE_PREFIX_RE.match(cleaned)
            if date_match:
                timestamp = date_match.group(1)
                cleaned = DATE_PREFIX_RE.sub('', cleaned)

            # Strip bullet/numbering markers
            cleaned = BULLET_PREFIX_RE.sub('', cleaned)
            cleaned = NUMBERED_PREFIX_RE.sub('', cleaned)
            cleaned = cleaned.strip()

            if len(cleaned) >= 3:
                cleaned_entries.append({'text': cleaned, 'timestamp': timestamp})

        # Group memories into chunks of CHUNK_SIZE for efficient LLM extraction
        chunks: List[ConversationChunk] = []
        for i in range(0, len(cleaned_entries), CHUNK_SIZE):
            batch = cleaned_entries[i:i + CHUNK_SIZE]

            # Use the timestamp from the first entry in the batch (if available)
            batch_timestamp = None
            for entry in batch:
                if entry['timestamp']:
                    batch_timestamp = entry['timestamp']
                    break

            chunks.append(ConversationChunk(
                title=f'Claude memories ({i + 1}-{min(i + CHUNK_SIZE, len(cleaned_entries))})',
                messages=[{'role': 'user', 'text': entry['text']} for entry in batch],
                timestamp=batch_timestamp,
            ))

        return AdapterParseResult(
            facts=[],
            chunks=chunks,
            total_messages=len(cleaned_entries),
            warnings=warnings,
            errors=errors,
            source_metadata={
                'format': 'memories-text',
                'total_lines': len(lines),
                'chunks_count': len(chunks),
            },
        )
