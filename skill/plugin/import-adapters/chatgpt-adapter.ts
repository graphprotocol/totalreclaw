import { BaseImportAdapter } from './base-adapter.js';
import type {
  ImportSource,
  AdapterParseResult,
  ConversationChunk,
  ProgressCallback,
} from './types.js';
import {
  CONVERSATION_CHUNK_SIZE,
  MEMORIES_CHUNK_SIZE,
  CHUNK_TOKEN_BUDGET,
  windowMessages,
  splitByTokenBudget,
} from './chunking.js';
import fs from 'node:fs';
import os from 'node:os';

// ── ChatGPT conversations.json types ────────────────────────────────────────

interface ChatGPTMessage {
  id: string;
  author: { role: 'user' | 'assistant' | 'system' | 'tool'; name?: string };
  content: {
    content_type: string;
    parts?: (string | null | Record<string, unknown>)[];
  };
  create_time?: number;
  metadata?: Record<string, unknown>;
}

interface ChatGPTMappingNode {
  id: string;
  message?: ChatGPTMessage | null;
  parent?: string | null;
  children: string[];
}

interface ChatGPTConversation {
  id?: string;
  title?: string;
  create_time?: number;
  update_time?: number;
  mapping: Record<string, ChatGPTMappingNode>;
}

// Chunk sizes imported from ./chunking.ts:
//   CONVERSATION_CHUNK_SIZE = 80 (narrative preservation per the 2026-04-12 spec)
//   MEMORIES_CHUNK_SIZE     = 20 (atomic facts, no narrative)
// The conversation path also runs splitByTokenBudget so oversized chunks get
// recursively halved to fit the LLM context window.

// ── ChatGPT Adapter ─────────────────────────────────────────────────────────

export class ChatGPTAdapter extends BaseImportAdapter {
  readonly source: ImportSource = 'chatgpt';
  readonly displayName = 'ChatGPT';

  async parse(
    input: { content?: string; file_path?: string },
    onProgress?: ProgressCallback,
  ): Promise<AdapterParseResult> {
    const warnings: string[] = [];
    const errors: string[] = [];

    let content: string;

    if (input.content) {
      content = input.content;
    } else if (input.file_path) {
      try {
        const resolvedPath = input.file_path.replace(/^~/, os.homedir());
        content = fs.readFileSync(resolvedPath, 'utf-8');
      } catch (e) {
        errors.push(`Failed to read file: ${e instanceof Error ? e.message : 'Unknown error'}`);
        return { facts: [], chunks: [], totalMessages: 0, warnings, errors };
      }
    } else {
      errors.push(
        'ChatGPT import requires either content (pasted text or JSON) or file_path. ' +
        'Export from ChatGPT: Settings -> Data Controls -> Export Data (conversations.json), ' +
        'or copy from Settings -> Personalization -> Memory -> Manage.',
      );
      return { facts: [], chunks: [], totalMessages: 0, warnings, errors };
    }

    // Detect format: JSON array = conversations.json, plain text = memories
    const trimmed = content.trim();

    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      // Try to parse as JSON (conversations.json or memory list)
      return this.parseConversationsJson(trimmed, warnings, errors, onProgress);
    }

    // Plain text: ChatGPT memories (one per line)
    return this.parseMemoriesText(trimmed, warnings, errors, onProgress);
  }

  /**
   * Parse ChatGPT conversations.json — full export with mapping tree.
   * Returns conversation chunks for LLM extraction (no pattern matching).
   */
  private parseConversationsJson(
    content: string,
    warnings: string[],
    errors: string[],
    onProgress?: ProgressCallback,
  ): AdapterParseResult {
    let conversations: ChatGPTConversation[];

    try {
      const data = JSON.parse(content);

      if (Array.isArray(data)) {
        conversations = data;
      } else if (data.conversations && Array.isArray(data.conversations)) {
        conversations = data.conversations;
      } else if (data.mapping) {
        // Single conversation object
        conversations = [data];
      } else {
        errors.push(
          'Unrecognized ChatGPT format. Expected an array of conversation objects (conversations.json) ' +
          'or plain text (ChatGPT memories).',
        );
        return { facts: [], chunks: [], totalMessages: 0, warnings, errors };
      }
    } catch (e) {
      errors.push(`Failed to parse ChatGPT JSON: ${e instanceof Error ? e.message : 'Unknown error'}`);
      return { facts: [], chunks: [], totalMessages: 0, warnings, errors };
    }

    if (onProgress) {
      onProgress({
        current: 0,
        total: conversations.length,
        phase: 'parsing',
        message: `Parsing ${conversations.length} ChatGPT conversations...`,
      });
    }

    const chunks: ConversationChunk[] = [];
    let totalMessages = 0;
    let convIndex = 0;

    for (const conv of conversations) {
      if (!conv.mapping) {
        warnings.push(`Conversation "${conv.title || 'untitled'}" has no mapping — skipped`);
        continue;
      }

      // Extract user + assistant messages in chronological order
      const messages = this.extractMessages(conv.mapping);
      if (messages.length === 0) continue;

      totalMessages += messages.length;

      // Determine timestamp from first message or conversation
      const timestamp = conv.create_time
        ? new Date(conv.create_time * 1000).toISOString()
        : undefined;

      const title = conv.title || 'Untitled Conversation';

      // Window into CONVERSATION_CHUNK_SIZE-message batches, then recursively
      // split any window whose estimated tokens exceed CHUNK_TOKEN_BUDGET.
      const windowed = windowMessages(messages, CONVERSATION_CHUNK_SIZE);
      const sized = splitByTokenBudget(windowed, CHUNK_TOKEN_BUDGET);
      const totalChunks = sized.length;
      sized.forEach((batch, idx) => {
        chunks.push({
          title: totalChunks > 1 ? `${title} (part ${idx + 1}/${totalChunks})` : title,
          messages: batch,
          timestamp,
        });
      });

      convIndex++;
      if (onProgress && convIndex % 50 === 0) {
        onProgress({
          current: convIndex,
          total: conversations.length,
          phase: 'parsing',
          message: `Parsed ${convIndex}/${conversations.length} conversations (${chunks.length} chunks, ${totalMessages} messages)...`,
        });
      }
    }

    if (chunks.length === 0 && conversations.length > 0) {
      warnings.push(
        `Parsed ${conversations.length} conversations but found no messages with text content.`,
      );
    }

    return {
      facts: [],
      chunks,
      totalMessages,
      warnings,
      errors,
      source_metadata: {
        format: 'conversations.json',
        conversations_count: conversations.length,
        chunks_count: chunks.length,
        total_messages: totalMessages,
      },
    };
  }

  /**
   * Parse ChatGPT memories — plain text, one memory per line.
   * Users copy this from Settings -> Personalization -> Memory -> Manage.
   *
   * Each line becomes a single-message conversation chunk for LLM extraction.
   */
  private parseMemoriesText(
    content: string,
    warnings: string[],
    errors: string[],
    onProgress?: ProgressCallback,
  ): AdapterParseResult {
    // Split by newlines and filter empty lines
    const lines = content.split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
      // Skip common header lines
      .filter((line) => !/^(?:memories?|chatgpt memories?|my memories?|saved memories?):?\s*$/i.test(line));

    if (onProgress) {
      onProgress({
        current: 0,
        total: lines.length,
        phase: 'parsing',
        message: `Parsing ${lines.length} ChatGPT memories...`,
      });
    }

    // Clean lines: strip bullet/dash/number markers
    const cleanedLines = lines.map((line) =>
      line
        .replace(/^[-*\u2022]\s+/, '')        // bullet points
        .replace(/^\d+[.)]\s+/, '')            // numbered lists
        .trim(),
    ).filter((line) => line.length >= 3);

    // Group memories into MEMORIES_CHUNK_SIZE-line chunks (atomic facts —
    // narrative preservation doesn't apply here, no token-safety split needed).
    const chunks: ConversationChunk[] = [];
    for (let i = 0; i < cleanedLines.length; i += MEMORIES_CHUNK_SIZE) {
      const batch = cleanedLines.slice(i, i + MEMORIES_CHUNK_SIZE);
      chunks.push({
        title: `ChatGPT memories (${i + 1}-${Math.min(i + MEMORIES_CHUNK_SIZE, cleanedLines.length)})`,
        messages: batch.map((text) => ({ role: 'user' as const, text })),
      });
    }

    return {
      facts: [],
      chunks,
      totalMessages: cleanedLines.length,
      warnings,
      errors,
      source_metadata: {
        format: 'memories-text',
        total_lines: lines.length,
        chunks_count: chunks.length,
      },
    };
  }

  /**
   * Traverse the mapping tree and extract user + assistant messages in
   * chronological order, following the "current" branch (latest sibling) when
   * a message has multiple children (e.g. regenerated responses).
   *
   * This replaces the earlier breadth-first walk which:
   *   - picked the first `Object.entries` key as root (non-deterministic on
   *     multi-root exports),
   *   - emitted siblings in `children[]` order (not chronological),
   *   - dropped any message under 3 characters.
   */
  private extractMessages(
    mapping: Record<string, ChatGPTMappingNode>,
  ): Array<{ role: 'user' | 'assistant'; text: string }> {
    // Collect all candidate roots (nodes with no parent or a parent missing
    // from the mapping). Multi-root exports are rare but do occur.
    const rootIds: string[] = [];
    for (const [id, node] of Object.entries(mapping)) {
      if (!node.parent || !mapping[node.parent]) {
        rootIds.push(id);
      }
    }
    if (rootIds.length === 0) return [];

    // Helper: earliest descendant create_time for a subtree.
    const earliestTime = (nodeId: string, seen = new Set<string>()): number => {
      if (seen.has(nodeId)) return Infinity;
      seen.add(nodeId);
      const node = mapping[nodeId];
      if (!node) return Infinity;
      let best = node.message?.create_time ?? Infinity;
      for (const childId of node.children ?? []) {
        best = Math.min(best, earliestTime(childId, seen));
      }
      return best;
    };

    // Pick the root whose subtree starts earliest. Tie-break on insertion
    // order to keep behavior deterministic.
    rootIds.sort((a, b) => earliestTime(a) - earliestTime(b));
    const rootId = rootIds[0];

    // Walk the "current branch" depth-first: at each node, pick the latest
    // child by `create_time` (matches ChatGPT's displayed thread after
    // regenerations — the user kept the newest response).
    const messages: Array<{ role: 'user' | 'assistant'; text: string }> = [];
    const visited = new Set<string>();
    let cursor: string | undefined = rootId;
    while (cursor && !visited.has(cursor)) {
      visited.add(cursor);
      const node = mapping[cursor];
      if (!node) break;

      const role = node.message?.author?.role;
      if (role === 'user' || role === 'assistant') {
        const text = this.extractTextFromParts(node.message?.content?.parts);
        // Keep any non-empty user/assistant message — even "yes" and "no"
        // carry meaning inside a conversation. Empty/whitespace still dropped.
        if (text && text.length >= 1) {
          messages.push({ role, text });
        }
      }

      // Choose next cursor: the child with the latest create_time. If no
      // children have a timestamp, fall back to the last entry in `children`
      // (insertion order — same as ChatGPT's "newest branch" heuristic).
      const children = (node.children ?? []).filter((cid) => mapping[cid]);
      if (children.length === 0) {
        cursor = undefined;
      } else if (children.length === 1) {
        cursor = children[0];
      } else {
        let best: string | undefined;
        let bestTime = -Infinity;
        for (const cid of children) {
          const t = mapping[cid].message?.create_time ?? -Infinity;
          if (t > bestTime) {
            bestTime = t;
            best = cid;
          }
        }
        cursor = best ?? children[children.length - 1];
      }
    }

    return messages;
  }

  /**
   * Extract plain text from message content parts.
   * Parts can be strings, null, or complex objects (images, etc.) -- we only want strings.
   */
  private extractTextFromParts(parts?: (string | null | Record<string, unknown>)[]): string | null {
    if (!parts || parts.length === 0) return null;

    const textParts = parts
      .filter((p): p is string => typeof p === 'string' && p.trim().length > 0);

    if (textParts.length === 0) return null;

    return textParts.join(' ').trim();
  }
}
