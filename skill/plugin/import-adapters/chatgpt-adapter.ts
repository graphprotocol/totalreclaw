import { BaseImportAdapter } from './base-adapter.js';
import type {
  ImportSource,
  NormalizedFact,
  AdapterParseResult,
  ProgressCallback,
} from './types.js';
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

// ── Pattern matching for fact extraction ────────────────────────────────────

/**
 * Patterns that indicate fact-like statements.
 * Each pattern maps to a NormalizedFact type and importance boost.
 */
const FACT_PATTERNS: Array<{
  pattern: RegExp;
  type: NormalizedFact['type'];
  importanceBoost: number;
}> = [
  // Identity & personal info
  { pattern: /\bmy name is\b/i, type: 'fact', importanceBoost: 2 },
  { pattern: /\bi(?:'m| am) (?:a |an |the )?\w/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bi work (?:at|for|in|as)\b/i, type: 'fact', importanceBoost: 2 },
  { pattern: /\bi live (?:in|at|near)\b/i, type: 'fact', importanceBoost: 2 },
  { pattern: /\bi(?:'m| am) from\b/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bmy (?:wife|husband|partner|dog|cat|kid|child|son|daughter|mom|dad|brother|sister)\b/i, type: 'fact', importanceBoost: 2 },
  { pattern: /\bmy (?:job|role|title|position) is\b/i, type: 'fact', importanceBoost: 2 },
  { pattern: /\bmy (?:email|phone|address|birthday)\b/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bi(?:'m| am) \d{1,3} years old\b/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bi speak\b/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bi studied\b/i, type: 'fact', importanceBoost: 1 },
  { pattern: /\bi graduated\b/i, type: 'fact', importanceBoost: 1 },

  // Preferences
  { pattern: /\bi (?:like|love|enjoy|prefer|favor)\b/i, type: 'preference', importanceBoost: 1 },
  { pattern: /\bi (?:don'?t like|dislike|hate|avoid|can'?t stand)\b/i, type: 'preference', importanceBoost: 1 },
  { pattern: /\bi(?:'d| would) (?:rather|prefer)\b/i, type: 'preference', importanceBoost: 1 },
  { pattern: /\bmy (?:favorite|favourite|preferred)\b/i, type: 'preference', importanceBoost: 1 },
  { pattern: /\bi always\b/i, type: 'preference', importanceBoost: 0 },
  { pattern: /\bi never\b/i, type: 'preference', importanceBoost: 0 },
  { pattern: /\bi usually\b/i, type: 'preference', importanceBoost: 0 },

  // Decisions
  { pattern: /\bi (?:decided|chose|picked|selected|went with)\b/i, type: 'decision', importanceBoost: 1 },
  { pattern: /\bwe (?:decided|chose|agreed)\b/i, type: 'decision', importanceBoost: 1 },

  // Goals
  { pattern: /\bi (?:want to|need to|plan to|hope to|aim to|intend to)\b/i, type: 'goal', importanceBoost: 1 },
  { pattern: /\bi(?:'m| am) (?:trying to|working on|building|learning|studying)\b/i, type: 'goal', importanceBoost: 1 },
  { pattern: /\bmy goal is\b/i, type: 'goal', importanceBoost: 2 },

  // Context / work
  { pattern: /\bi use\b/i, type: 'fact', importanceBoost: 0 },
  { pattern: /\bi(?:'m| am) using\b/i, type: 'fact', importanceBoost: 0 },
  { pattern: /\bmy (?:project|app|website|company|team|startup|business)\b/i, type: 'context', importanceBoost: 1 },
];

/**
 * Classify a user message and determine its fact type and importance.
 * Returns null if the message is too short or doesn't match any patterns.
 */
function classifyMessage(text: string): { type: NormalizedFact['type']; importance: number } | null {
  const trimmed = text.trim();

  // Skip very short messages (questions, greetings, etc.)
  if (trimmed.length < 15) return null;

  // Skip messages that are purely questions (start with question word and end with ?)
  if (/^(?:what|where|when|who|why|how|can|could|would|should|is|are|do|does|did)\b/i.test(trimmed) && trimmed.endsWith('?')) {
    return null;
  }

  // Skip messages that are just greetings or single-word responses
  if (/^(?:hi|hello|hey|thanks|thank you|ok|okay|yes|no|sure|great|cool|nice|bye|goodbye)\b/i.test(trimmed) && trimmed.length < 30) {
    return null;
  }

  let bestType: NormalizedFact['type'] = 'episodic';
  let bestBoost = -1;

  for (const { pattern, type, importanceBoost } of FACT_PATTERNS) {
    if (pattern.test(trimmed)) {
      if (importanceBoost > bestBoost) {
        bestType = type;
        bestBoost = importanceBoost;
      }
    }
  }

  if (bestBoost >= 0) {
    // Matched a pattern -- this is a fact-like statement
    return { type: bestType, importance: 5 + bestBoost };
  }

  // No pattern match -- return null to let caller decide whether to include as episodic
  return null;
}

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
        return { facts: [], warnings, errors };
      }
    } else {
      errors.push(
        'ChatGPT import requires either content (pasted text or JSON) or file_path. ' +
        'Export from ChatGPT: Settings -> Data Controls -> Export Data (conversations.json), ' +
        'or copy from Settings -> Personalization -> Memory -> Manage.',
      );
      return { facts: [], warnings, errors };
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
        return { facts: [], warnings, errors };
      }
    } catch (e) {
      errors.push(`Failed to parse ChatGPT JSON: ${e instanceof Error ? e.message : 'Unknown error'}`);
      return { facts: [], warnings, errors };
    }

    if (onProgress) {
      onProgress({
        current: 0,
        total: conversations.length,
        phase: 'parsing',
        message: `Parsing ${conversations.length} ChatGPT conversations...`,
      });
    }

    const rawFacts: Partial<NormalizedFact>[] = [];
    let convIndex = 0;

    for (const conv of conversations) {
      if (!conv.mapping) {
        warnings.push(`Conversation "${conv.title || 'untitled'}" has no mapping — skipped`);
        continue;
      }

      // Extract user messages from the mapping tree
      const userMessages = this.extractUserMessages(conv.mapping);

      for (const msg of userMessages) {
        const textParts = this.extractTextFromParts(msg.message?.content?.parts);
        if (!textParts) continue;

        // Split multi-sentence messages into individual sentences for better fact extraction
        const sentences = this.splitIntoSentences(textParts);

        for (const sentence of sentences) {
          const classification = classifyMessage(sentence);

          if (classification) {
            rawFacts.push({
              text: sentence.slice(0, 512),
              type: classification.type,
              importance: classification.importance,
              source: 'chatgpt' as ImportSource,
              sourceId: msg.id,
              sourceTimestamp: msg.message?.create_time
                ? new Date(msg.message.create_time * 1000).toISOString()
                : conv.create_time
                  ? new Date(conv.create_time * 1000).toISOString()
                  : undefined,
              tags: conv.title ? [`conversation:${conv.title.slice(0, 60)}`] : [],
            });
          }
        }
      }

      convIndex++;
      if (onProgress && convIndex % 50 === 0) {
        onProgress({
          current: convIndex,
          total: conversations.length,
          phase: 'parsing',
          message: `Parsed ${convIndex}/${conversations.length} conversations (${rawFacts.length} facts so far)...`,
        });
      }
    }

    if (rawFacts.length === 0 && conversations.length > 0) {
      warnings.push(
        `Parsed ${conversations.length} conversations but extracted 0 facts. ` +
        'The heuristic extraction looks for personal statements (I am, I like, I work at, etc.). ' +
        'For better results, export your ChatGPT memories directly: Settings -> Personalization -> Memory -> Manage.',
      );
    }

    const { facts, invalidCount } = this.validateFacts(rawFacts);

    if (invalidCount > 0) {
      warnings.push(`${invalidCount} extracted statements had invalid/empty text and were skipped`);
    }

    return {
      facts,
      warnings,
      errors,
      source_metadata: {
        format: 'conversations.json',
        conversations_count: conversations.length,
        user_messages_extracted: rawFacts.length,
      },
    };
  }

  /**
   * Parse ChatGPT memories — plain text, one memory per line.
   * Users copy this from Settings -> Personalization -> Memory -> Manage.
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

    const rawFacts: Partial<NormalizedFact>[] = lines.map((line, i) => {
      // Strip leading bullet/dash/number markers
      const cleaned = line
        .replace(/^[-*\u2022]\s+/, '')        // bullet points
        .replace(/^\d+[.)]\s+/, '')            // numbered lists
        .trim();

      // Classify using pattern matching
      const classification = classifyMessage(cleaned);

      return {
        text: cleaned.slice(0, 512),
        type: classification?.type ?? 'fact',
        // ChatGPT memories are pre-curated by ChatGPT's own memory system, so
        // they are generally higher quality -- default to importance 6
        importance: classification?.importance ?? 6,
        source: 'chatgpt' as ImportSource,
        sourceId: `chatgpt-memory-${i}`,
        tags: ['chatgpt-memory'],
      };
    });

    const { facts, invalidCount } = this.validateFacts(rawFacts);

    if (invalidCount > 0) {
      warnings.push(`${invalidCount} memories had invalid/empty text and were skipped`);
    }

    return {
      facts,
      warnings,
      errors,
      source_metadata: {
        format: 'memories-text',
        total_lines: lines.length,
      },
    };
  }

  /**
   * Traverse the mapping tree and extract user messages in chronological order.
   */
  private extractUserMessages(mapping: Record<string, ChatGPTMappingNode>): ChatGPTMappingNode[] {
    // Find the root node (the one with no parent or parent not in mapping)
    let rootId: string | undefined;
    for (const [id, node] of Object.entries(mapping)) {
      if (!node.parent || !mapping[node.parent]) {
        rootId = id;
        break;
      }
    }

    if (!rootId) return [];

    // Walk the tree depth-first, following first child (main thread)
    const messages: ChatGPTMappingNode[] = [];
    const visited = new Set<string>();
    const queue: string[] = [rootId];

    while (queue.length > 0) {
      const nodeId = queue.shift()!;
      if (visited.has(nodeId)) continue;
      visited.add(nodeId);

      const node = mapping[nodeId];
      if (!node) continue;

      // Only collect user messages
      if (node.message?.author?.role === 'user') {
        messages.push(node);
      }

      // Follow children (add them to queue in order)
      for (const childId of node.children || []) {
        queue.push(childId);
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

  /**
   * Split a message into individual sentences for finer-grained fact extraction.
   * Only splits on sentence boundaries; keeps short messages intact.
   */
  private splitIntoSentences(text: string): string[] {
    // If the message is short enough, return as-is
    if (text.length < 100) return [text];

    // Split on sentence-ending punctuation followed by space and uppercase
    const sentences = text
      .split(/(?<=[.!?])\s+(?=[A-Z])/)
      .map((s) => s.trim())
      .filter((s) => s.length >= 10);

    return sentences.length > 0 ? sentences : [text];
  }
}
