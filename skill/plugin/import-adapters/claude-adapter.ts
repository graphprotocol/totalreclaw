import { BaseImportAdapter } from './base-adapter.js';
import type {
  ImportSource,
  NormalizedFact,
  AdapterParseResult,
  ProgressCallback,
} from './types.js';
import fs from 'node:fs';
import os from 'node:os';

/**
 * Pattern for lines that start with a date prefix.
 * Claude memory entries sometimes have: [2026-03-15] - User prefers TypeScript
 */
const DATE_PREFIX_RE = /^\[(\d{4}-\d{2}-\d{2})\]\s*[-:]\s*/;

/**
 * Pattern for bullet-prefixed lines.
 */
const BULLET_PREFIX_RE = /^[-*\u2022]\s+/;

/**
 * Pattern for numbered list lines.
 */
const NUMBERED_PREFIX_RE = /^\d+[.)]\s+/;

/**
 * Patterns for classifying Claude memory entries by type.
 * Claude memories are already curated facts, so we use lighter heuristics.
 */
const TYPE_PATTERNS: Array<{ pattern: RegExp; type: NormalizedFact['type'] }> = [
  { pattern: /\bprefers?\b/i, type: 'preference' },
  { pattern: /\blikes?\b/i, type: 'preference' },
  { pattern: /\bdislikes?\b/i, type: 'preference' },
  { pattern: /\bfavorite\b/i, type: 'preference' },
  { pattern: /\bfavourite\b/i, type: 'preference' },
  { pattern: /\bavoids?\b/i, type: 'preference' },
  { pattern: /\bdecided\b/i, type: 'decision' },
  { pattern: /\bchose\b/i, type: 'decision' },
  { pattern: /\bwants? to\b/i, type: 'goal' },
  { pattern: /\bplans? to\b/i, type: 'goal' },
  { pattern: /\bgoal\b/i, type: 'goal' },
  { pattern: /\baims? to\b/i, type: 'goal' },
  { pattern: /\bworking on\b/i, type: 'context' },
  { pattern: /\bcurrently\b/i, type: 'context' },
  { pattern: /\bproject\b/i, type: 'context' },
];

function classifyMemory(text: string): NormalizedFact['type'] {
  for (const { pattern, type } of TYPE_PATTERNS) {
    if (pattern.test(text)) return type;
  }
  return 'fact';
}

export class ClaudeAdapter extends BaseImportAdapter {
  readonly source: ImportSource = 'claude';
  readonly displayName = 'Claude';

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
        'Claude import requires either content (pasted text) or file_path. ' +
        'Copy your memories from Claude: Settings -> Memory -> select all and copy.',
      );
      return { facts: [], warnings, errors };
    }

    // Claude memory export is plain text, one fact per line.
    // Sometimes with date prefixes like [2026-03-15] - User prefers TypeScript.
    // Sometimes with bullet points or numbered lists.
    return this.parseMemoriesText(content.trim(), warnings, errors, onProgress);
  }

  /**
   * Parse Claude memories — plain text, one memory per line.
   */
  private parseMemoriesText(
    content: string,
    warnings: string[],
    errors: string[],
    onProgress?: ProgressCallback,
  ): AdapterParseResult {
    // Split by newlines and filter
    const lines = content.split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
      // Skip common header lines
      .filter((line) => !/^(?:memories?|claude memories?|my memories?|saved memories?):?\s*$/i.test(line));

    if (onProgress) {
      onProgress({
        current: 0,
        total: lines.length,
        phase: 'parsing',
        message: `Parsing ${lines.length} Claude memories...`,
      });
    }

    const rawFacts: Partial<NormalizedFact>[] = lines.map((line, i) => {
      let cleaned = line;
      let timestamp: string | undefined;

      // Extract date prefix if present
      const dateMatch = cleaned.match(DATE_PREFIX_RE);
      if (dateMatch) {
        timestamp = dateMatch[1];
        cleaned = cleaned.replace(DATE_PREFIX_RE, '');
      }

      // Strip bullet/numbering markers
      cleaned = cleaned
        .replace(BULLET_PREFIX_RE, '')
        .replace(NUMBERED_PREFIX_RE, '')
        .trim();

      const type = classifyMemory(cleaned);

      return {
        text: cleaned.slice(0, 512),
        type,
        // Claude memories are already curated -- default to importance 6
        importance: 6,
        source: 'claude' as ImportSource,
        sourceId: `claude-memory-${i}`,
        sourceTimestamp: timestamp,
        tags: ['claude-memory'],
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
}
