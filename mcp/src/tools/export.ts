import { TotalReclaw, RerankedResult } from '@totalreclaw/client';
import { EXPORT_TOOL_DESCRIPTION } from '../prompts.js';
import { readBlobUnified } from '../claims-helper.js';

export interface ExportInput {
  format?: 'markdown' | 'json';
  include_metadata?: boolean;
}

export interface ExportOutput {
  content: string;
  format: string;
  fact_count: number;
  exported_at: string;
}

export const exportToolDefinition = {
  name: 'totalreclaw_export',
  description: EXPORT_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      format: {
        type: 'string',
        enum: ['markdown', 'json'],
        default: 'markdown',
        description: 'Output format',
      },
      include_metadata: {
        type: 'boolean',
        default: true,
        description: 'Include metadata in export',
      },
    },
  },
  annotations: {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
  },
};

export async function handleExport(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = (args || {}) as ExportInput;
  const format = input.format || 'markdown';
  const includeMetadata = input.include_metadata !== false;

  try {
    const results = await client.recall('*', 1000);

    const exportedAt = new Date().toISOString();

    // For each fact, parse the stored text as a v1 or v0 blob so we can
    // surface taxonomy fields in the export. `readBlobUnified` handles all
    // shapes (v1, v0 short-key canonical, plugin-legacy, raw-text fallback)
    // and returns a uniform result including the v1 surface when present.
    const parsed = results.map((r: RerankedResult) => {
      const doc = readBlobUnified(r.fact.text);
      return { r, doc };
    });

    let content: string;
    if (format === 'json') {
      const jsonData = {
        version: '1.0.0',
        exported_at: exportedAt,
        facts: parsed.map(({ r, doc }) => {
          const base: Record<string, unknown> = {
            id: r.fact.id,
            text: doc.text,
            importance: Math.round((r.fact.metadata.importance ?? 0.5) * 10),
            created_at: r.fact.createdAt.toISOString(),
          };
          // Surface v1 fields when present (memory-taxonomy-v1 blobs).
          if (doc.v1) {
            base.type = doc.v1.type;
            base.source = doc.v1.source;
            if (doc.v1.scope) base.scope = doc.v1.scope;
            if (doc.v1.volatility) base.volatility = doc.v1.volatility;
            if (doc.v1.reasoning) base.reasoning = doc.v1.reasoning;
            if (doc.v1.expires_at) base.expires_at = doc.v1.expires_at;
            if (doc.v1.confidence != null) base.confidence = doc.v1.confidence;
            if (doc.v1.superseded_by) base.superseded_by = doc.v1.superseded_by;
            if (doc.v1.entities) base.entities = doc.v1.entities;
          } else {
            // v0 / legacy: surface at least the inferred category.
            base.type = doc.category;
          }
          if (includeMetadata) {
            base.metadata = {
              tags: r.fact.metadata.tags,
              source: r.fact.metadata.source,
            };
          }
          return base;
        }),
      };
      content = JSON.stringify(jsonData, null, 2);
    } else {
      const lines: string[] = [
        `# TotalReclaw Export`,
        ``,
        `**Exported:** ${exportedAt}`,
        `**Total Facts:** ${results.length}`,
        ``,
        `---`,
        ``,
      ];

      for (const { r, doc } of parsed) {
        const importance = Math.round((r.fact.metadata.importance ?? 0.5) * 10);
        lines.push(`## ${doc.text}`);
        lines.push(``);
        if (includeMetadata) {
          lines.push(`- **Importance:** ${importance}/10`);
          lines.push(`- **Created:** ${r.fact.createdAt.toISOString()}`);
          // v1-aware metadata rendering.
          if (doc.v1) {
            lines.push(`- **Type:** ${doc.v1.type}`);
            lines.push(`- **Source:** ${doc.v1.source}`);
            if (doc.v1.scope) lines.push(`- **Scope:** ${doc.v1.scope}`);
            if (doc.v1.reasoning) lines.push(`- **Reasoning:** ${doc.v1.reasoning}`);
            if (doc.v1.volatility) lines.push(`- **Volatility:** ${doc.v1.volatility}`);
          } else {
            lines.push(`- **Type:** ${doc.category}`);
          }
          if (r.fact.metadata.tags?.length) {
            lines.push(`- **Tags:** ${r.fact.metadata.tags.join(', ')}`);
          }
        }
        lines.push(``);
        lines.push(`ID: \`${r.fact.id}\``);
        lines.push(``);
        lines.push(`---`);
        lines.push(``);
      }

      content = lines.join('\n');
    }

    const result: ExportOutput = {
      content,
      format,
      fact_count: results.length,
      exported_at: exportedAt,
    };

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(result),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error occurred';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          content: '',
          format,
          fact_count: 0,
          exported_at: new Date().toISOString(),
          error: `Failed to export memories: ${message}`,
        }),
      }],
    };
  }
}
