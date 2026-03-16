import { TotalReclaw, FactMetadata } from '@totalreclaw/client';
import { IMPORT_TOOL_DESCRIPTION } from '../prompts.js';

export interface ImportInput {
  content: string;
  format?: 'markdown' | 'json';
  merge_strategy?: 'skip_existing' | 'overwrite' | 'merge';
  reencrypt?: boolean;
  validate_only?: boolean;
}

export interface ImportOutput {
  success: boolean;
  facts_imported: number;
  facts_skipped: number;
  facts_merged: number;
  errors: Array<{
    line?: number;
    fact_id?: string;
    error: string;
  }>;
  warnings: string[];
  import_id: string;
}

interface ParsedFact {
  text: string;
  importance?: number;
  type?: string;
  namespace?: string;
  id?: string;
}

export const importToolDefinition = {
  name: 'totalreclaw_import',
  description: IMPORT_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      content: {
        type: 'string',
        description: 'The exported content (JSON or Markdown string)',
      },
      format: {
        type: 'string',
        enum: ['markdown', 'json'],
        description: 'Format of content (auto-detected if not specified)',
      },
      merge_strategy: {
        type: 'string',
        enum: ['skip_existing', 'overwrite', 'merge'],
        default: 'skip_existing',
        description: 'How to handle conflicts',
      },
      validate_only: {
        type: 'boolean',
        default: false,
        description: 'Parse and validate without importing',
      },
    },
    required: ['content'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: false,
  },
};

function detectFormat(content: string): 'json' | 'markdown' {
  const trimmed = content.trim();
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return 'json';
  }
  return 'markdown';
}

function parseJsonContent(content: string): { facts: ParsedFact[]; errors: string[] } {
  const errors: string[] = [];
  const facts: ParsedFact[] = [];

  try {
    const data = JSON.parse(content);

    const factArray = Array.isArray(data) ? data : data.facts;
    if (!Array.isArray(factArray)) {
      errors.push('JSON must contain a facts array');
      return { facts, errors };
    }

    for (let i = 0; i < factArray.length; i++) {
      const item = factArray[i];
      if (!item.text || typeof item.text !== 'string') {
        errors.push(`Fact ${i}: missing or invalid text field`);
        continue;
      }

      facts.push({
        text: item.text,
        importance: item.importance,
        type: item.type || item.metadata?.type,
        namespace: item.namespace,
        id: item.id,
      });
    }
  } catch (e) {
    errors.push(`JSON parse error: ${e instanceof Error ? e.message : 'Unknown error'}`);
  }

  return { facts, errors };
}

function parseMarkdownContent(content: string): { facts: ParsedFact[]; errors: string[] } {
  const errors: string[] = [];
  const facts: ParsedFact[] = [];

  const sections = content.split(/^---$/m);

  for (const section of sections) {
    const headingMatch = section.match(/^##\s+(.+)$/m);
    if (!headingMatch) continue;

    const text = headingMatch[1].trim();

    let importance: number | undefined;
    const impMatch = section.match(/\*\*Importance:\*\*\s*(\d+)/);
    if (impMatch) {
      importance = parseInt(impMatch[1], 10);
    }

    let type: string | undefined;
    const typeMatch = section.match(/\*\*Type:\*\*\s*(\w+)/);
    if (typeMatch) {
      type = typeMatch[1];
    }

    let namespace: string | undefined;
    const nsMatch = section.match(/\*\*Namespace:\*\*\s*(\w+)/);
    if (nsMatch) {
      namespace = nsMatch[1];
    }

    let id: string | undefined;
    const idMatch = section.match(/ID:\s*`([^`]+)`/);
    if (idMatch) {
      id = idMatch[1];
    }

    facts.push({ text, importance, type, namespace, id });
  }

  return { facts, errors };
}

function generateImportId(): string {
  return `import-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

export async function handleImport(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as ImportInput;
  const errors: ImportOutput['errors'] = [];
  const warnings: string[] = [];

  if (!input.content || typeof input.content !== 'string') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          facts_imported: 0,
          facts_skipped: 0,
          facts_merged: 0,
          errors: [{ error: 'content is required' }],
          warnings: [],
          import_id: generateImportId(),
        }),
      }],
    };
  }

  const format = input.format || detectFormat(input.content);
  const mergeStrategy = input.merge_strategy || 'skip_existing';
  const validateOnly = input.validate_only === true;

  let parsed: { facts: ParsedFact[]; errors: string[] };

  if (format === 'json') {
    parsed = parseJsonContent(input.content);
  } else {
    parsed = parseMarkdownContent(input.content);
  }

  if (parsed.errors.length > 0) {
    for (const e of parsed.errors) {
      errors.push({ error: e });
    }
  }

  if (validateOnly) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: errors.length === 0,
          facts_imported: 0,
          facts_skipped: parsed.facts.length,
          facts_merged: 0,
          errors,
          warnings: ['Validate-only mode: no facts were imported'],
          import_id: generateImportId(),
        }),
      }],
    };
  }

  let factsImported = 0;
  let factsSkipped = 0;
  let factsMerged = 0;

  const existingResults = await client.recall('*', 1000);
  const existingTexts = new Map<string, string>();
  for (const r of existingResults) {
    existingTexts.set(r.fact.text.toLowerCase().trim(), r.fact.id);
  }

  for (const fact of parsed.facts) {
    if (!fact.text || fact.text.trim().length === 0) {
      errors.push({ error: 'Empty fact text' });
      continue;
    }

    if (fact.importance !== undefined && (fact.importance < 1 || fact.importance > 10)) {
      warnings.push(`Fact importance ${fact.importance} out of range, using 5`);
      fact.importance = 5;
    }

    const normalizedText = fact.text.toLowerCase().trim();
    const existingId = existingTexts.get(normalizedText);

    if (existingId && mergeStrategy === 'skip_existing') {
      factsSkipped++;
      continue;
    }

    if (existingId && mergeStrategy === 'overwrite') {
      try {
        await client.forget(existingId);
        existingTexts.delete(normalizedText);
      } catch (e) {
        errors.push({ fact_id: existingId, error: `Failed to delete existing: ${e instanceof Error ? e.message : 'Unknown'}` });
        factsSkipped++;
        continue;
      }
    }

    try {
      const metadata: FactMetadata = {
        importance: (fact.importance ?? 5) / 10,
        source: 'import',
        tags: fact.type ? [fact.type] : [],
      };

      await client.remember(fact.text, metadata);
      factsImported++;

      if (mergeStrategy === 'merge' && existingId) {
        factsMerged++;
      }
    } catch (e) {
      errors.push({
        error: `Failed to store fact: ${e instanceof Error ? e.message : 'Unknown'}`,
      });
    }
  }

  const result: ImportOutput = {
    success: true,
    facts_imported: factsImported,
    facts_skipped: factsSkipped,
    facts_merged: factsMerged,
    errors,
    warnings,
    import_id: generateImportId(),
  };

  return {
    content: [{
      type: 'text',
      text: JSON.stringify(result),
    }],
  };
}
