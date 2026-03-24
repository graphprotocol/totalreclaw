import { TotalReclaw, FactMetadata } from '@totalreclaw/client';
import { resolve } from 'node:path';
import { IMPORT_FROM_TOOL_DESCRIPTION } from '../prompts.js';

// ── Types (mirrored from skill/plugin/import-adapters/types.ts) ─────────────
// We define these locally to avoid importing from outside the MCP rootDir.
// The runtime import() below loads the actual adapter code at runtime.

export type ImportSource = 'mem0' | 'mcp-memory' | 'memoclaw' | 'generic-json' | 'generic-csv';

export interface ImportFromInput {
  source: ImportSource;
  api_key?: string;
  source_user_id?: string;
  content?: string;
  file_path?: string;
  dry_run?: boolean;
  api_url?: string;
}

export interface NormalizedFact {
  text: string;
  type: 'fact' | 'preference' | 'decision' | 'episodic' | 'goal' | 'context' | 'summary';
  importance: number;
  source: ImportSource;
  sourceId?: string;
  sourceTimestamp?: string;
  tags?: string[];
}

export interface AdapterParseResult {
  facts: NormalizedFact[];
  warnings: string[];
  errors: string[];
  source_metadata?: Record<string, unknown>;
}

export interface ImportResult {
  success: boolean;
  source: ImportSource;
  total_found: number;
  imported: number;
  skipped_duplicate: number;
  skipped_invalid: number;
  errors: Array<{ index: number; text_preview: string; error: string }>;
  warnings: string[];
  import_id: string;
  duration_ms: number;
}

interface ImportAdapter {
  readonly source: ImportSource;
  readonly displayName: string;
  parse(
    input: { content?: string; api_key?: string; source_user_id?: string; api_url?: string; file_path?: string },
  ): Promise<AdapterParseResult>;
}

// ── Tool Definition ─────────────────────────────────────────────────────────

export const importFromToolDefinition = {
  name: 'totalreclaw_import_from',
  description: IMPORT_FROM_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      source: {
        type: 'string',
        enum: ['mem0', 'mcp-memory', 'memoclaw', 'generic-json', 'generic-csv'],
        description: 'The source system to import from',
      },
      api_key: {
        type: 'string',
        description: 'API key for the source system (Mem0, MemoClaw). NOT stored — used only for this import.',
      },
      source_user_id: {
        type: 'string',
        description: 'User or agent ID in the source system',
      },
      content: {
        type: 'string',
        description: 'For file-based sources: the file content (pasted JSON, CSV, or JSONL)',
      },
      file_path: {
        type: 'string',
        description: 'For file-based sources: path to the file on disk',
      },
      dry_run: {
        type: 'boolean',
        default: false,
        description: 'Parse and validate without actually importing. Shows what would be imported.',
      },
      api_url: {
        type: 'string',
        description: 'Override API base URL (for self-hosted instances)',
      },
    },
    required: ['source'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true, // content fingerprint dedup makes it idempotent
  },
};

// ── Adapter loader ──────────────────────────────────────────────────────────
// Adapters live in skill/plugin/import-adapters/ (outside MCP rootDir).
// We use dynamic import() to load them at runtime from the compiled path.

async function loadAdapter(source: ImportSource): Promise<ImportAdapter> {
  // At runtime, this file is at dist/tools/import-from.js
  // The adapters are at skill/plugin/import-adapters/ in the repo root.
  // We resolve the path dynamically so TypeScript doesn't try to resolve it at compile time.
  // From dist/tools/ -> ../../../skill/plugin/import-adapters/index.js -> <repo>/skill/plugin/import-adapters/
  const adapterPath = resolve(__dirname, '..', '..', '..', 'skill', 'plugin', 'import-adapters', 'index.js');

  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const adaptersModule = await import(adapterPath);

    // The adapters module should export a getAdapter function
    if (typeof adaptersModule.getAdapter === 'function') {
      return adaptersModule.getAdapter(source) as ImportAdapter;
    }

    throw new Error(
      `Import adapters module does not export getAdapter(). ` +
      `Ensure skill/plugin/import-adapters/ has been built with the concrete adapters.`
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);

    // Provide a helpful error if the adapters haven't been created yet
    if (msg.includes('Cannot find module') || msg.includes('ERR_MODULE_NOT_FOUND')) {
      throw new Error(
        `Import adapter for "${source}" not found. ` +
        `Ensure the import adapters are installed at skill/plugin/import-adapters/. ` +
        `Run: cd skill/plugin && npm run build`
      );
    }
    throw e;
  }
}

// ── Handler ─────────────────────────────────────────────────────────────────

export async function handleImportFrom(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as ImportFromInput;
  const startTime = Date.now();

  // Validate source
  const validSources: ImportSource[] = ['mem0', 'mcp-memory', 'memoclaw', 'generic-json', 'generic-csv'];
  if (!input.source || !validSources.includes(input.source)) {
    return errorResponse(`Invalid source. Must be one of: ${validSources.join(', ')}`);
  }

  try {
    // Get the appropriate adapter
    const adapter = await loadAdapter(input.source);

    // Parse source data
    const parseResult = await adapter.parse({
      content: input.content,
      api_key: input.api_key,
      source_user_id: input.source_user_id,
      api_url: input.api_url,
      file_path: input.file_path,
    });

    if (parseResult.errors.length > 0 && parseResult.facts.length === 0) {
      return errorResponse(
        `Failed to parse ${adapter.displayName} data:\n` +
        parseResult.errors.join('\n'),
      );
    }

    // Dry run — just report what would be imported
    if (input.dry_run) {
      const result: ImportResult = {
        success: true,
        source: input.source,
        total_found: parseResult.facts.length,
        imported: 0,
        skipped_duplicate: 0,
        skipped_invalid: 0,
        errors: [],
        warnings: [
          'DRY RUN — no facts were imported.',
          ...parseResult.warnings,
        ],
        import_id: generateImportId(),
        duration_ms: Date.now() - startTime,
      };

      // Include a preview of first 10 facts
      const preview = parseResult.facts.slice(0, 10).map((f, i) =>
        `  ${i + 1}. [${f.type}] ${f.text.slice(0, 80)}${f.text.length > 80 ? '...' : ''}`,
      ).join('\n');

      return {
        content: [{
          type: 'text',
          text: JSON.stringify(result) + '\n\nPreview of first 10 facts:\n' + preview,
        }],
      };
    }

    // Store facts via client.remember()
    let imported = 0;
    let skippedDuplicate = 0;
    const skippedInvalid = 0;
    const storeErrors: ImportResult['errors'] = [];

    for (let i = 0; i < parseResult.facts.length; i++) {
      const fact = parseResult.facts[i];

      try {
        const metadata: FactMetadata = {
          importance: fact.importance / 10, // Normalize to 0-1
          source: `import:${input.source}`,
          tags: [
            `import_source:${input.source}`,
            ...(fact.tags || []),
          ],
        };

        if (fact.sourceTimestamp) {
          metadata.timestamp = new Date(fact.sourceTimestamp);
        }

        await client.remember(fact.text, metadata);
        imported++;
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Unknown error';

        // Content fingerprint dedup returns 409
        if (msg.includes('409') || msg.includes('duplicate') || msg.includes('fingerprint')) {
          skippedDuplicate++;
        } else {
          storeErrors.push({
            index: i,
            text_preview: fact.text.slice(0, 60),
            error: msg,
          });
        }
      }

      // Limit individual errors reported
      if (storeErrors.length >= 20) {
        storeErrors.push({
          index: i,
          text_preview: '...',
          error: `Stopped reporting errors after 20. ${parseResult.facts.length - i - 1} facts remaining.`,
        });
        break;
      }
    }

    const result: ImportResult = {
      success: imported > 0 || skippedDuplicate > 0,
      source: input.source,
      total_found: parseResult.facts.length,
      imported,
      skipped_duplicate: skippedDuplicate,
      skipped_invalid: skippedInvalid,
      errors: storeErrors,
      warnings: parseResult.warnings,
      import_id: generateImportId(),
      duration_ms: Date.now() - startTime,
    };

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(result),
      }],
    };
  } catch (e) {
    return errorResponse(`Import failed: ${e instanceof Error ? e.message : 'Unknown error'}`);
  }
}

function generateImportId(): string {
  return `import-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

function errorResponse(message: string) {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({ success: false, error: message }),
    }],
  };
}
