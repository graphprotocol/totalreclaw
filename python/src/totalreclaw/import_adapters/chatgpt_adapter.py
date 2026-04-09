"""
ChatGPT import adapter -- parses conversations.json or plain text memories.

Ported from skill/plugin/import-adapters/chatgpt-adapter.ts
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional, Callable, List, Any

from .base_adapter import BaseImportAdapter
from .types import AdapterParseResult, ConversationChunk


# Maximum messages per conversation chunk for LLM extraction.
CHUNK_SIZE = 20


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

            # Extract user + assistant messages in chronological order
            messages = self._extract_messages(mapping)
            if not messages:
                continue

            total_messages += len(messages)

            # Determine timestamp from conversation create_time
            create_time = conv.get('create_time')
            timestamp = None
            if create_time:
                try:
                    timestamp = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            title = conv.get('title') or 'Untitled Conversation'

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
                chunks.append(ConversationChunk(title=chunk_title, messages=batch, timestamp=timestamp))

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
    ) -> List[dict]:
        """
        Traverse the mapping tree and extract user + assistant messages
        in chronological order.

        Both roles are included because the assistant's response often provides
        context that helps the LLM understand what the user meant.
        """
        # Find the root node (the one with no parent or parent not in mapping)
        root_id = None
        for node_id, node in mapping.items():
            parent = node.get('parent')
            if not parent or parent not in mapping:
                root_id = node_id
                break

        if not root_id:
            return []

        # Walk the tree breadth-first, following children in order (main thread)
        messages: List[dict] = []
        visited: set = set()
        queue: List[str] = [root_id]

        while queue:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)

            node = mapping.get(node_id)
            if not node:
                continue

            message = node.get('message')
            if message:
                role = None
                author = message.get('author')
                if author:
                    role = author.get('role')

                # Only collect user and assistant messages (skip system, tool)
                if role in ('user', 'assistant'):
                    content = message.get('content')
                    parts = content.get('parts') if content else None
                    text_parts = self._extract_text_from_parts(parts)
                    if text_parts and len(text_parts) >= 3:
                        messages.append({'role': role, 'text': text_parts})

            # Follow children (add them to queue in order)
            for child_id in node.get('children', []):
                queue.append(child_id)

        return messages

    def _extract_text_from_parts(self, parts: Any) -> Optional[str]:
        """
        Extract plain text from message content parts.
        Parts can be strings, None, or complex objects (images, etc.) -- we only want strings.
        """
        if not parts or not isinstance(parts, list):
            return None

        text_parts = [p for p in parts if isinstance(p, str) and p.strip()]

        if not text_parts:
            return None

        return ' '.join(text_parts).strip()
