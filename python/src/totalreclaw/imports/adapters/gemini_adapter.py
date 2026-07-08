"""
Gemini import adapter -- thin shim over the shared Rust core parser.

All Gemini format parsing (MyActivity.json, legacy Takeout HTML, and pasted
"Saved info") lives in ``totalreclaw_core.parse_gemini`` so the logic --
including the locale-robust, lossless timestamp handling -- is identical across
every client (Python/Hermes via PyO3, the TypeScript clients via WASM).

This adapter owns only file I/O and the 500MB/RAM preflight; it then delegates
parsing to core. ``totalreclaw-core`` is a hard dependency of this package (it
also backs crypto), so there is no Python-side parser fallback.
"""

import json
import math
import os
from typing import Optional, Callable, List

import psutil

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult, ConversationChunk, ParsedTurn


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
            raw = content
        elif file_path:
            raw, preflight_err = self._read_file(file_path, warnings, errors)
            if preflight_err is not None:
                return preflight_err
        else:
            errors.append(
                'Gemini import requires either content or file_path. '
                'Export from Google Takeout (takeout.google.com -> "My Activity" -> '
                '"Gemini Apps"); provide the "My Activity.html" (or MyActivity.json) '
                'file path, or paste your Saved info.',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        if on_progress:
            on_progress({'current': 0, 'total': 0, 'phase': 'parsing',
                         'message': 'Parsing Gemini export...'})

        try:
            import totalreclaw_core
            data = json.loads(totalreclaw_core.parse_gemini(raw))
        except Exception as e:
            errors.append(
                'Gemini parsing requires totalreclaw-core >= 2.5.0 (the shared native '
                f'parser); it could not be loaded: {e}',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        if on_progress:
            on_progress({
                'current': len(data.get('chunks', [])),
                'total': len(data.get('chunks', [])),
                'phase': 'parsing',
                'message': (
                    f"Parsed {data.get('total_messages', 0)} messages into "
                    f"{len(data.get('chunks', []))} chunks"
                ),
            })

        return self._result_from_core(data, warnings, errors)

    # ── File read + preflight (the only client-native responsibility) ────────

    def _read_file(self, file_path, warnings, errors):
        """Read a file with the 500MB / RAM preflight. Returns (text, error_result).

        On success returns (text, None); on failure returns ('', AdapterParseResult).
        """
        try:
            resolved = os.path.expanduser(file_path)
            stat = os.stat(resolved)
            file_size_mb = stat.st_size / (1024 * 1024)
            if file_size_mb > 500:
                errors.append(
                    f'File is too large to import: {file_size_mb:.1f}MB exceeds the 500MB cap. '
                    'Split the file into smaller chunks and import each separately.',
                )
                return '', AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
            free_mem = psutil.virtual_memory().available
            if free_mem < stat.st_size * 2:
                free_mb = free_mem / (1024 * 1024)
                need_mb = math.ceil(stat.st_size * 2 / (1024 * 1024))
                errors.append(
                    f'Not enough free memory: {free_mb:.0f}MB available, '
                    f'~{need_mb}MB needed (2× file size). '
                    'Close other applications or split the file.',
                )
                return '', AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
            with open(resolved, 'r', encoding='utf-8') as f:
                return f.read(), None
        except Exception as e:
            errors.append(f'Failed to read file: {e}')
            return '', AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

    # ── Convert core ParseResult -> AdapterParseResult ───────────────────────

    @staticmethod
    def _result_from_core(data: dict, warnings: List[str], errors: List[str]) -> AdapterParseResult:
        chunks = [
            ConversationChunk(
                title=c.get('title', 'Gemini session'),
                messages=c.get('messages', []),
                timestamp=c.get('timestamp'),
            )
            for c in data.get('chunks', [])
        ]
        # #368 Part 2 — flat per-turn view with real per-turn timestamps. Absent
        # on older core wheels (pre-Part-2) and for the timestamp-less "Saved
        # info" paste format; the import engine falls back to the chunk-level
        # approximation when this list is empty.
        turns = [
            ParsedTurn(
                user_text=t.get('user_text', ''),
                assistant_text=t.get('assistant_text', ''),
                text=t.get('text', ''),
                chunk_index=t.get('chunk_index', 0),
                ts_iso=t.get('ts_iso'),
                ts_unix=t.get('ts_unix'),
                # Range fields present only on core wheels with #368 Part 2
                # straddle fidelity; None on older wheels (engine then assigns
                # whole chunks without straddle-splitting).
                chunk_msg_start=t.get('chunk_msg_start'),
                chunk_msg_end=t.get('chunk_msg_end'),
            )
            for t in data.get('turns', [])
        ]
        meta = {
            'format': data.get('format'),
            'chunks_count': len(chunks),
            'total_messages': data.get('total_messages', 0),
        }
        if data.get('records_count'):
            meta['records_count'] = data['records_count']
        if data.get('skipped'):
            meta['skipped_non_gemini'] = data['skipped']
        return AdapterParseResult(
            facts=[],
            chunks=chunks,
            total_messages=data.get('total_messages', 0),
            warnings=warnings + list(data.get('warnings', [])),
            errors=errors + list(data.get('errors', [])),
            source_metadata=meta,
            turns=turns,
        )
