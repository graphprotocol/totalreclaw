"""
Gemini import adapter -- parses Google Takeout HTML (My Activity.html).

Ported from skill/plugin/import-adapters/gemini-adapter.ts
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Callable, List

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult, ConversationChunk


# Maximum messages per conversation chunk for LLM extraction.
CHUNK_SIZE = 20

# Gap (in minutes) between entries that starts a new pseudo-session.
SESSION_GAP_MINUTES = 30

# ── Timestamp Parsing ────────────────────────────────────────────────────────

MONTHS = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
}

_TIMESTAMP_RE = re.compile(
    r'^(\d{1,2})\s+(\w{3})\s+(\d{4}),\s+(\d{2}):(\d{2}):(\d{2})\s+'
)


def _parse_timestamp(raw: str) -> Optional[str]:
    """
    Parse Gemini timestamp: "1 Apr 2026, 18:39:35 WEST" -> ISO 8601.
    Timezone is treated as UTC (all entries use the same TZ, preserving order).
    """
    m = _TIMESTAMP_RE.match(raw)
    if not m:
        return None
    month_abbr = m.group(2)
    if month_abbr not in MONTHS:
        return None
    dt = datetime(
        year=int(m.group(3)),
        month=MONTHS[month_abbr],
        day=int(m.group(1)),
        hour=int(m.group(4)),
        minute=int(m.group(5)),
        second=int(m.group(6)),
        tzinfo=timezone.utc,
    )
    return dt.isoformat()


# ── HTML Helpers ─────────────────────────────────────────────────────────────

def _decode_entities(t: str) -> str:
    """Decode common HTML entities."""
    return (
        t.replace('&#39;', "'")
        .replace('&quot;', '"')
        .replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&nbsp;', ' ')
    )


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    s = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    s = re.sub(r'</p>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'</li>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'</h[1-6]>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<hr\s*/?>', '\n---\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


# ── Entry Types ──────────────────────────────────────────────────────────────

@dataclass
class _GeminiEntry:
    user_prompt: str
    ai_response: str
    timestamp_iso: str
    timestamp_unix: int


# ── Gemini Adapter ───────────────────────────────────────────────────────────

class GeminiAdapter(BaseImportAdapter):
    source = 'gemini'
    display_name = 'Google Gemini'

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
            html = content
        elif file_path:
            try:
                resolved = os.path.expanduser(file_path)
                with open(resolved, 'r', encoding='utf-8') as f:
                    html = f.read()
            except Exception as e:
                errors.append(f'Failed to read file: {e}')
                return AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
        else:
            errors.append(
                'Gemini import requires either content or file_path. '
                'Export from Google Takeout: takeout.google.com -> select Gemini Apps -> export. '
                'Provide the "My Activity.html" file path.',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        if on_progress:
            on_progress({'current': 0, 'total': 0, 'phase': 'parsing', 'message': 'Parsing Gemini HTML...'})

        # Parse HTML into entries
        entries = self._parse_html(html)
        if not entries:
            warnings.append('No conversation entries found in the HTML file.')
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        # Group into pseudo-sessions by temporal proximity
        sessions = self._group_sessions(entries)

        if on_progress:
            on_progress({
                'current': 0,
                'total': len(sessions),
                'phase': 'parsing',
                'message': f'Parsed {len(entries)} entries into {len(sessions)} sessions',
            })

        # Build conversation chunks from sessions
        chunks: List[ConversationChunk] = []
        total_messages = 0

        for session in sessions:
            messages: List[dict] = []
            for entry in session:
                if entry.user_prompt:
                    messages.append({'role': 'user', 'text': entry.user_prompt})
                if entry.ai_response:
                    messages.append({'role': 'assistant', 'text': entry.ai_response})
            if not messages:
                continue

            total_messages += len(messages)
            timestamp = session[0].timestamp_iso

            # Sub-chunk large sessions
            for i in range(0, len(messages), CHUNK_SIZE):
                batch = messages[i:i + CHUNK_SIZE]
                chunk_idx = i // CHUNK_SIZE + 1
                total_chunks = (len(messages) + CHUNK_SIZE - 1) // CHUNK_SIZE
                if total_chunks > 1:
                    title = f'Gemini session (part {chunk_idx}/{total_chunks})'
                else:
                    title = 'Gemini session'
                chunks.append(ConversationChunk(title=title, messages=batch, timestamp=timestamp))

        return AdapterParseResult(
            facts=[],
            chunks=chunks,
            total_messages=total_messages,
            warnings=warnings,
            errors=errors,
            source_metadata={
                'format': 'gemini-takeout-html',
                'total_entries': len(entries),
                'sessions_count': len(sessions),
                'chunks_count': len(chunks),
                'total_messages': total_messages,
                'date_range': {
                    'earliest': entries[0].timestamp_iso if entries else None,
                    'latest': entries[-1].timestamp_iso if entries else None,
                },
            },
        )

    def _parse_html(self, html: str) -> List[_GeminiEntry]:
        """
        Parse Gemini Takeout HTML into structured entries.

        Each outer-cell div contains: "Prompted USER_TEXT<br>TIMESTAMP<br>RESPONSE_HTML"
        all within one content-cell.
        """
        entries: List[_GeminiEntry] = []
        cell_pattern = re.compile(
            r'<div class="outer-cell[^"]*">([\s\S]*?)(?=<div class="outer-cell|$)'
        )
        ts_pattern = re.compile(r'(\d{1,2}\s+\w{3}\s+\d{4},\s+\d{2}:\d{2}:\d{2}\s+\w+)')

        for match in cell_pattern.finditer(html):
            cell = match.group(1)

            # Only process "Prompted" entries (skip canvas, feedback)
            prompted_idx = cell.find('Prompted\u00a0')
            if prompted_idx == -1:
                continue

            # Extract timestamp
            ts_match = ts_pattern.search(cell)
            if not ts_match:
                continue
            timestamp_iso = _parse_timestamp(ts_match.group(1))
            if not timestamp_iso:
                continue

            # Split on timestamp to separate user prompt from AI response
            after_prompted = cell[prompted_idx + len('Prompted\u00a0'):]
            ts_inner_match = ts_pattern.search(after_prompted)
            ts_idx = ts_inner_match.start() if ts_inner_match else -1

            user_prompt = ''
            ai_response = ''

            if ts_idx > 0:
                user_prompt = _strip_html(_decode_entities(after_prompted[:ts_idx])).strip()

                if ts_inner_match:
                    after_ts = after_prompted[ts_idx + len(ts_inner_match.group(0)):]
                    after_ts = re.sub(r'^\s*<br\s*/?>\s*', '', after_ts, flags=re.IGNORECASE)
                    end_div_match = re.search(r'</div>\s*<div class="content-cell', after_ts)
                    if end_div_match:
                        raw_resp = after_ts[:end_div_match.start()]
                    else:
                        raw_resp = after_ts
                    ai_response = _strip_html(_decode_entities(raw_resp)).strip()

            if len(user_prompt) < 3 and len(ai_response) < 3:
                continue

            timestamp_unix = int(
                datetime.fromisoformat(timestamp_iso).timestamp()
            )
            entries.append(_GeminiEntry(
                user_prompt=user_prompt,
                ai_response=ai_response,
                timestamp_iso=timestamp_iso,
                timestamp_unix=timestamp_unix,
            ))

        # Sort chronologically (HTML is newest-first)
        entries.sort(key=lambda e: e.timestamp_unix)
        return entries

    def _group_sessions(self, entries: List[_GeminiEntry]) -> List[List[_GeminiEntry]]:
        """Group entries into pseudo-sessions by temporal proximity."""
        if not entries:
            return []

        sessions: List[List[_GeminiEntry]] = []
        current: List[_GeminiEntry] = [entries[0]]

        for i in range(1, len(entries)):
            gap = entries[i].timestamp_unix - entries[i - 1].timestamp_unix
            if gap > SESSION_GAP_MINUTES * 60:
                sessions.append(current)
                current = [entries[i]]
            else:
                current.append(entries[i])

        if current:
            sessions.append(current)
        return sessions
