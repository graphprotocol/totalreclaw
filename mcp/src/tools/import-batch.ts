/**
 * TotalReclaw MCP — Batch Import Tool
 *
 * Processes a fixed slice of conversation chunks from a source file.
 * Called repeatedly by the host agent (with increasing offset) for
 * large imports that exceed a single turn timeout.
 *
 * The MCP server parses the file and returns chunks. The host agent
 * does LLM extraction and calls totalreclaw_remember for each fact.
 *
 * For small imports (<50 chunks), use totalreclaw_import_from directly.
 */

import { resolve } from 'node:path';
import type { ImportSource, AdapterParseResult, ConversationChunk } from './import-from.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_BATCH_SIZE = 25;
const EXTRACTION_RATIO = 2.5; // avg facts per chunk, empirical
const SECONDS_PER_BATCH = 45;

// ---------------------------------------------------------------------------
// Tool Definition
// ---------------------------------------------------------------------------

export const importBatchToolDefinition = {
  name: 'totalreclaw_import_batch',
  description:
    'Internal polling for LARGE imports (Gemini/ChatGPT/Claude 50+ convos). Slice per call.\n' +
    '\nINVOKE WHEN:\n' +
    '- totalreclaw_import_from returned total_chunks > 50\n' +
    '- large Takeout/conversations.json times out\n' +
    '\nPOLLING: offset=0, batch_size def 25, offset+=batch_size until is_complete. Extract + totalreclaw_remember. Dedup safe.\n' +
    '\nWHEN NOT TO USE:\n' +
    '- <50 convos → totalreclaw_import_from\n' +
    '- pre-structured (Mem0) → totalreclaw_import_from\n' +
    '- single block → totalreclaw_import_from\n' +
    '- don\'t expose tool name',
  inputSchema: {
    type: 'object',
    properties: {
      source: {
        type: 'string',
        enum: ['gemini', 'chatgpt', 'claude'],
        description: 'Source system (gemini: Google Takeout HTML; chatgpt: conversations.json; claude: memory text)',
      },
      file_path: {
        type: 'string',
        description: 'Path to the source file on disk',
      },
      content: {
        type: 'string',
        description: 'File content (for text-based sources like Claude memories)',
      },
      offset: {
        type: 'number',
        default: 0,
        description: 'Starting chunk index (0-based). Increment by batch_size for each call.',
      },
      batch_size: {
        type: 'number',
        default: DEFAULT_BATCH_SIZE,
        description: `Number of chunks to process per call (default ${DEFAULT_BATCH_SIZE})`,
      },
    },
    required: ['source'],
  },
  annotations: {
    readOnlyHint: true, // Parsing only — storage is via separate totalreclaw_remember calls
    destructiveHint: false,
    idempotentHint: true,
  },
};

// ---------------------------------------------------------------------------
// Adapter loader (reuses import-from's loader pattern)
// ---------------------------------------------------------------------------

interface ImportAdapter {
  readonly source: ImportSource;
  readonly displayName: string;
  parse(
    input: { content?: string; file_path?: string },
  ): Promise<AdapterParseResult>;
}

async function loadAdapter(source: string): Promise<ImportAdapter> {
  const adapterPath = resolve(__dirname, '..', '..', '..', 'skill', 'plugin', 'import-adapters', 'index.js');
  const adaptersModule = await import(adapterPath);
  if (typeof adaptersModule.getAdapter === 'function') {
    return adaptersModule.getAdapter(source) as ImportAdapter;
  }
  throw new Error(`Import adapters module does not export getAdapter()`);
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

export async function handleImportBatch(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as {
    source: string;
    file_path?: string;
    content?: string;
    offset?: number;
    batch_size?: number;
  };

  const validSources = ['gemini', 'chatgpt', 'claude'];
  if (!input.source || !validSources.includes(input.source)) {
    return {
      content: [{ type: 'text', text: JSON.stringify({ success: false, error: `Invalid source. Must be one of: ${validSources.join(', ')}` }) }],
    };
  }

  const offset = input.offset ?? 0;
  const batchSize = input.batch_size ?? DEFAULT_BATCH_SIZE;

  try {
    const adapter = await loadAdapter(input.source);

    // Parse the file (fast: ~2s for large files)
    const parseResult = await adapter.parse({
      content: input.content,
      file_path: input.file_path,
    });

    if (parseResult.errors.length > 0 && parseResult.chunks.length === 0) {
      return {
        content: [{ type: 'text', text: JSON.stringify({ success: false, error: parseResult.errors.join('; ') }) }],
      };
    }

    const totalChunks = parseResult.chunks.length;
    const slice = parseResult.chunks.slice(offset, offset + batchSize);
    const remaining = Math.max(0, totalChunks - offset - slice.length);
    const isComplete = remaining === 0;

    // Format chunks as text for the host agent to extract facts from
    const chunkTexts = slice.map((chunk, i) => {
      const idx = offset + i + 1;
      const header = `--- Conversation ${idx}/${totalChunks}: ${chunk.title} ---`;
      const ts = chunk.timestamp ? `(${chunk.timestamp})` : '';
      const msgs = chunk.messages.map((m) => `[${m.role}]: ${m.text}`).join('\n');
      return `${header} ${ts}\n${msgs}`;
    });

    const result = {
      success: true,
      batch_offset: offset,
      batch_size: slice.length,
      total_chunks: totalChunks,
      chunks_in_batch: slice.length,
      remaining_chunks: remaining,
      is_complete: isComplete,
      // Estimation
      estimated_total_facts: Math.round(totalChunks * EXTRACTION_RATIO),
      estimated_total_userops: Math.ceil(totalChunks * EXTRACTION_RATIO / 15),
      estimated_total_minutes: Math.ceil(Math.ceil(totalChunks / batchSize) * SECONDS_PER_BATCH / 60),
    };

    const instructions = isComplete
      ? `This is the final batch. After processing these ${slice.length} chunks, the import is complete.`
      : `After processing, call totalreclaw_import_batch again with offset=${offset + slice.length} to continue.`;

    return {
      content: [
        { type: 'text', text: JSON.stringify(result) },
        { type: 'text', text: `${instructions}\n\nExtract important personal facts from each conversation below and store them with totalreclaw_remember:\n\n${chunkTexts.join('\n\n')}` },
      ],
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      content: [{ type: 'text', text: JSON.stringify({ success: false, error: msg }) }],
    };
  }
}
