"""
ChatGPT import adapter -- parses conversations.json or plain text memories.

Ported from skill/plugin/import-adapters/chatgpt-adapter.ts
"""

import glob
import json
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional, Callable, List, Any, Tuple

import psutil

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult, ConversationChunk


# Maximum messages per conversation chunk for LLM extraction.
CHUNK_SIZE = 20

# Real ChatGPT exports split conversations across numbered files inside the
# export zip (conversations-000.json, conversations-001.json, ...). Older
# exports ship a single conversations.json.
_CONVERSATIONS_MEMBER_RE = re.compile(r'(?:^|/)conversations(?:-\d+)?\.json$')

_MAX_TOTAL_MB = 500


class ChatGPTAdapter(BaseImportAdapter):
    source = 'chatgpt'
    display_name = 'ChatGPT'

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
                documents, doc_errors = self._read_export_documents(resolved)
                if doc_errors:
                    errors.extend(doc_errors)
                    return AdapterParseResult(
                        facts=[], chunks=[], total_messages=0,
                        warnings=warnings, errors=errors,
                    )
                if len(documents) > 1 or os.path.isdir(resolved) or zipfile.is_zipfile(resolved):
                    # Multi-file export (zip or unpacked directory): parse the
                    # concatenated conversation list directly.
                    conversations: List[dict] = []
                    for doc in documents:
                        parsed = json.loads(doc)
                        if isinstance(parsed, list):
                            conversations.extend(parsed)
                        elif isinstance(parsed, dict) and 'mapping' in parsed:
                            conversations.append(parsed)
                    return self._parse_conversation_list(
                        conversations, warnings, errors, on_progress,
                    )
                raw = documents[0]
            except Exception as e:
                errors.append(f'Failed to read file: {e}')
                return AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
        else:
            errors.append(
                'ChatGPT import requires either content (pasted text or JSON) or file_path. '
                'Export from ChatGPT: Settings -> Data Controls -> Export Data (conversations.json), '
                'or copy from Settings -> Personalization -> Memory -> Manage.',
            )
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        # Detect format: JSON array = conversations.json, plain text = memories
        trimmed = raw.strip()

        if trimmed.startswith('[') or trimmed.startswith('{'):
            return self._parse_conversations_json(trimmed, warnings, errors, on_progress)

        # Plain text: ChatGPT memories (one per line)
        return self._parse_memories_text(trimmed, warnings, errors, on_progress)

    def _parse_conversations_json(
        self,
        content: str,
        warnings: List[str],
        errors: List[str],
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        """
        Parse ChatGPT conversations.json -- full export with mapping tree.
        Returns conversation chunks for LLM extraction (no pattern matching).
        """
        try:
            data = json.loads(content)

            if isinstance(data, list):
                conversations = data
            elif isinstance(data, dict) and 'conversations' in data and isinstance(data['conversations'], list):
                conversations = data['conversations']
            elif isinstance(data, dict) and 'mapping' in data:
                # Single conversation object
                conversations = [data]
            else:
                errors.append(
                    'Unrecognized ChatGPT format. Expected an array of conversation objects '
                    '(conversations.json) or plain text (ChatGPT memories).',
                )
                return AdapterParseResult(
                    facts=[], chunks=[], total_messages=0,
                    warnings=warnings, errors=errors,
                )
        except json.JSONDecodeError as e:
            errors.append(f'Failed to parse ChatGPT JSON: {e}')
            return AdapterParseResult(
                facts=[], chunks=[], total_messages=0,
                warnings=warnings, errors=errors,
            )

        return self._parse_conversation_list(conversations, warnings, errors, on_progress)

    def _read_export_documents(self, resolved: str) -> Tuple[List[str], List[str]]:
        """
        Read the raw JSON document(s) of an export given a path that may be:
        a single conversations.json, an export zip, or an unpacked export
        directory (conversations-000.json, conversations-001.json, ...).

        Returns (documents, errors). File ordering is deterministic (sorted by
        name) so chunk offsets — and therefore import resume state — are
        stable across calls.
        """
        errors: List[str] = []

        def _check_budget(total_bytes: int, what: str) -> bool:
            total_mb = total_bytes / (1024 * 1024)
            if total_mb > _MAX_TOTAL_MB:
                errors.append(
                    f'{what} is too large to import: {total_mb:.1f}MB exceeds the '
                    f'{_MAX_TOTAL_MB}MB cap. Split it and import each part separately.',
                )
                return False
            free_mem = psutil.virtual_memory().available
            if free_mem < total_bytes * 2:
                free_mb = free_mem / (1024 * 1024)
                need_mb = math.ceil(total_bytes * 2 / (1024 * 1024))
                errors.append(
                    f'Not enough free memory: {free_mb:.0f}MB available, '
                    f'~{need_mb}MB needed (2× data size). '
                    'Close other applications or split the file.',
                )
                return False
            return True

        if os.path.isdir(resolved):
            paths = sorted(glob.glob(os.path.join(resolved, 'conversations*.json')))
            if not paths:
                errors.append(
                    f'No conversations*.json found in directory {resolved}. '
                    'Point at the unpacked ChatGPT export folder or the export zip.',
                )
                return [], errors
            total = sum(os.stat(p).st_size for p in paths)
            if not _check_budget(total, 'Export directory'):
                return [], errors
            docs = []
            for p in paths:
                with open(p, 'r', encoding='utf-8') as f:
                    docs.append(f.read())
            return docs, errors

        if zipfile.is_zipfile(resolved):
            with zipfile.ZipFile(resolved) as zf:
                members = sorted(
                    (m for m in zf.infolist() if _CONVERSATIONS_MEMBER_RE.search(m.filename)),
                    key=lambda m: m.filename,
                )
                if not members:
                    errors.append(
                        f'No conversations*.json found inside {resolved}. '
                        'Is this a ChatGPT data export zip?',
                    )
                    return [], errors
                total = sum(m.file_size for m in members)  # uncompressed
                if not _check_budget(total, 'Export archive'):
                    return [], errors
                return [zf.read(m).decode('utf-8') for m in members], errors

        # Single file
        stat = os.stat(resolved)
        if not _check_budget(stat.st_size, 'File'):
            return [], errors
        with open(resolved, 'r', encoding='utf-8') as f:
            return [f.read()], errors

    def _parse_conversation_list(
        self,
        conversations: List[dict],
        warnings: List[str],
        errors: List[str],
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        if on_progress:
            on_progress({
                'current': 0,
                'total': len(conversations),
                'phase': 'parsing',
                'message': f'Parsing {len(conversations)} ChatGPT conversations...',
            })

        chunks: List[ConversationChunk] = []
        total_messages = 0
        conv_index = 0

        for conv in conversations:
            mapping = conv.get('mapping')
            if not mapping or not isinstance(mapping, dict):
                warnings.append(
                    f'Conversation "{conv.get("title", "untitled")}" has no mapping -- skipped'
                )
                continue

            # Extract user + assistant messages along the canonical branch
            messages = self._extract_messages(mapping, current_node=conv.get('current_node'))
            if not messages:
                continue

            total_messages += len(messages)

            # Conversation-level timestamp — fallback for messages without
            # their own create_time.
            create_time = conv.get('create_time')
            conv_timestamp = None
            if create_time:
                try:
                    conv_timestamp = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            title = conv.get('title') or 'Untitled Conversation'
            conversation_id = conv.get('conversation_id') or conv.get('id')

            # Chunk into batches of CHUNK_SIZE messages
            for i in range(0, len(messages), CHUNK_SIZE):
                batch = messages[i:i + CHUNK_SIZE]
                chunk_index = i // CHUNK_SIZE + 1
                total_chunks = (len(messages) + CHUNK_SIZE - 1) // CHUNK_SIZE

                chunk_title = (
                    f'{title} (part {chunk_index}/{total_chunks})'
                    if total_chunks > 1
                    else title
                )
                # Chunk timestamp = first message's own time, so multi-chunk
                # conversations don't collapse onto one conversation-level time.
                chunk_ts = batch[0].get('timestamp') or conv_timestamp
                chunks.append(ConversationChunk(
                    title=chunk_title,
                    messages=batch,
                    timestamp=chunk_ts,
                    conversation_id=conversation_id,
                ))

            conv_index += 1
            if on_progress and conv_index % 50 == 0:
                on_progress({
                    'current': conv_index,
                    'total': len(conversations),
                    'phase': 'parsing',
                    'message': (
                        f'Parsed {conv_index}/{len(conversations)} conversations '
                        f'({len(chunks)} chunks, {total_messages} messages)...'
                    ),
                })

        if not chunks and conversations:
            warnings.append(
                f'Parsed {len(conversations)} conversations but found no messages with text content.',
            )

        return AdapterParseResult(
            facts=[],
            chunks=chunks,
            total_messages=total_messages,
            warnings=warnings,
            errors=errors,
            source_metadata={
                'format': 'conversations.json',
                'conversations_count': len(conversations),
                'chunks_count': len(chunks),
                'total_messages': total_messages,
            },
        )

    def _parse_memories_text(
        self,
        content: str,
        warnings: List[str],
        errors: List[str],
        on_progress: Optional[Callable] = None,
    ) -> AdapterParseResult:
        """
        Parse ChatGPT memories -- plain text, one memory per line.
        Users copy this from Settings -> Personalization -> Memory -> Manage.
        """
        # Split by newlines and filter empty lines
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        # Skip common header lines
        header_re = re.compile(
            r'^(?:memories?|chatgpt memories?|my memories?|saved memories?):?\s*$',
            re.IGNORECASE,
        )
        lines = [line for line in lines if not header_re.match(line)]

        if on_progress:
            on_progress({
                'current': 0,
                'total': len(lines),
                'phase': 'parsing',
                'message': f'Parsing {len(lines)} ChatGPT memories...',
            })

        # Clean lines: strip bullet/dash/number markers
        cleaned_lines: List[str] = []
        for line in lines:
            cleaned = re.sub(r'^[-*\u2022]\s+', '', line)     # bullet points
            cleaned = re.sub(r'^\d+[.)]\s+', '', cleaned)     # numbered lists
            cleaned = cleaned.strip()
            if len(cleaned) >= 3:
                cleaned_lines.append(cleaned)

        # Group all memories into chunks of CHUNK_SIZE for efficient LLM extraction
        chunks: List[ConversationChunk] = []
        for i in range(0, len(cleaned_lines), CHUNK_SIZE):
            batch = cleaned_lines[i:i + CHUNK_SIZE]
            chunks.append(ConversationChunk(
                title=f'ChatGPT memories ({i + 1}-{min(i + CHUNK_SIZE, len(cleaned_lines))})',
                messages=[{'role': 'user', 'text': text} for text in batch],
            ))

        return AdapterParseResult(
            facts=[],
            chunks=chunks,
            total_messages=len(cleaned_lines),
            warnings=warnings,
            errors=errors,
            source_metadata={
                'format': 'memories-text',
                'total_lines': len(lines),
                'chunks_count': len(chunks),
            },
        )

    def _extract_messages(
        self,
        mapping: dict,
        current_node: Optional[str] = None,
    ) -> List[dict]:
        """
        Extract user + assistant messages along the CANONICAL branch of the
        mapping tree, in chronological order.

        ChatGPT stores edited/regenerated messages as sibling branches; the
        thread the user actually sees is the ``current_node`` -> parent chain.
        Walking all branches (the old BFS) imports superseded drafts as
        near-duplicates. When ``current_node`` is missing or dangling we fall
        back to the deepest root-to-leaf path.

        Both roles are included because the assistant's response often provides
        context that helps the LLM understand what the user meant.
        """
        path = self._canonical_node_path(mapping, current_node)

        messages: List[dict] = []
        for node_id in path:
            node = mapping.get(node_id) or {}
            message = node.get('message')
            if not message:
                continue
            role = (message.get('author') or {}).get('role')
            # Only collect user and assistant messages (skip system, tool)
            if role not in ('user', 'assistant'):
                continue
            text = self._extract_text_from_message(message)
            if not text or len(text) < 3:
                continue
            entry = {'role': role, 'text': text}
            create_time = message.get('create_time')
            if create_time:
                try:
                    entry['timestamp'] = datetime.fromtimestamp(
                        create_time, tz=timezone.utc,
                    ).isoformat()
                except (ValueError, TypeError, OSError):
                    pass
            messages.append(entry)

        return messages

    def _canonical_node_path(
        self,
        mapping: dict,
        current_node: Optional[str],
    ) -> List[str]:
        """Root-to-leaf node ids of the canonical thread."""

        def _chain_up(leaf_id: str) -> List[str]:
            chain: List[str] = []
            seen: set = set()
            nid = leaf_id
            while nid and nid in mapping and nid not in seen:
                seen.add(nid)
                chain.append(nid)
                nid = mapping[nid].get('parent')
            chain.reverse()
            return chain

        if current_node and current_node in mapping:
            return _chain_up(current_node)

        # Fallback: deepest leaf wins (longest root-to-leaf chain).
        leaves = [
            nid for nid, node in mapping.items()
            if not node.get('children')
        ]
        best: List[str] = []
        for leaf in leaves:
            chain = _chain_up(leaf)
            if len(chain) > len(best):
                best = chain
        return best

    def _extract_text_from_message(self, message: dict) -> Optional[str]:
        """
        Extract text from a message's content across the content types that
        appear in real exports:

        - ``text`` / default: string entries in ``content.parts``
        - ``multimodal_text``: string parts plus dict parts carrying text —
          voice messages store their transcript as
          ``{"content_type": "audio_transcription", "text": ...}``; image
          pointers have no text and are skipped
        - ``code``: the source lives in ``content.text``, not ``parts``
        """
        content = message.get('content') or {}
        content_type = content.get('content_type')

        if content_type == 'code':
            text = content.get('text')
            return text.strip() if isinstance(text, str) and text.strip() else None

        pieces: List[str] = []
        parts = content.get('parts')
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, str) and p.strip():
                    pieces.append(p.strip())
                elif isinstance(p, dict):
                    t = p.get('text') or p.get('transcript')
                    if isinstance(t, str) and t.strip():
                        pieces.append(t.strip())

        if not pieces:
            return None
        return ' '.join(pieces).strip()
