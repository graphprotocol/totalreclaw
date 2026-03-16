/**
 * @jest-environment node
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

const {
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
} = require('../dist/tools/index.js');

describe('MCP Tool Definitions', () => {
  it('should define remember tool with correct schema', () => {
    expect(rememberToolDefinition.name).toBe('totalreclaw_remember');
    // Batch mode: neither fact nor facts is strictly required (one or the other)
    expect(rememberToolDefinition.inputSchema.properties.fact.type).toBe('string');
    expect(rememberToolDefinition.inputSchema.properties.facts.type).toBe('array');
    expect(rememberToolDefinition.inputSchema.properties.importance.minimum).toBe(1);
    expect(rememberToolDefinition.inputSchema.properties.importance.maximum).toBe(10);
    // Tool annotations
    expect(rememberToolDefinition.annotations.idempotentHint).toBe(true);
    expect(rememberToolDefinition.annotations.readOnlyHint).toBe(false);
  });

  it('should define recall tool with correct schema', () => {
    expect(recallToolDefinition.name).toBe('totalreclaw_recall');
    expect(recallToolDefinition.inputSchema.required).toContain('query');
    expect(recallToolDefinition.inputSchema.properties.query.type).toBe('string');
    expect(recallToolDefinition.inputSchema.properties.k.default).toBe(8);
  });

  it('should define forget tool with correct schema', () => {
    expect(forgetToolDefinition.name).toBe('totalreclaw_forget');
    expect(forgetToolDefinition.inputSchema.properties.fact_id).toBeDefined();
    expect(forgetToolDefinition.inputSchema.properties.query).toBeDefined();
  });

  it('should define export tool with correct schema', () => {
    expect(exportToolDefinition.name).toBe('totalreclaw_export');
    expect(exportToolDefinition.inputSchema.properties.format.enum).toContain('markdown');
    expect(exportToolDefinition.inputSchema.properties.format.enum).toContain('json');
  });

  it('should define import tool with correct schema', () => {
    expect(importToolDefinition.name).toBe('totalreclaw_import');
    expect(importToolDefinition.inputSchema.required).toContain('content');
    expect(importToolDefinition.inputSchema.properties.merge_strategy.enum).toContain('skip_existing');
    expect(importToolDefinition.inputSchema.properties.merge_strategy.enum).toContain('overwrite');
    expect(importToolDefinition.inputSchema.properties.merge_strategy.enum).toContain('merge');
  });
});

function createMockClient(overrides = {}) {
  return {
    recall: jest.fn().mockResolvedValue([]),
    remember: jest.fn().mockResolvedValue('fact-id'),
    forget: jest.fn().mockResolvedValue(undefined),
    isReady: jest.fn().mockReturnValue(true),
    ...overrides,
  };
}

describe('handleRemember', () => {
  it('should reject empty fact text', async () => {
    const mockClient = createMockClient();
    const { handleRemember } = require('../dist/tools/remember.js');
    const result = await handleRemember(mockClient, { fact: '' });

    expect(result.content[0].type).toBe('text');
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toContain('non-empty string');
  });

  it('should reject invalid importance', async () => {
    const mockClient = createMockClient();
    const { handleRemember } = require('../dist/tools/remember.js');
    const result = await handleRemember(mockClient, { fact: 'test', importance: 15 });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toContain('between 1 and 10');
  });

  it('should store fact with default namespace', async () => {
    const mockClient = createMockClient({ remember: jest.fn().mockResolvedValue('fact-123') });
    const { handleRemember } = require('../dist/tools/remember.js');
    const result = await handleRemember(mockClient, { fact: 'User likes coffee' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(true);
    expect(parsed.fact_id).toBe('fact-123');
    expect(mockClient.remember).toHaveBeenCalled();
  });
});

describe('handleRecall', () => {
  it('should reject empty query', async () => {
    const mockClient = createMockClient();
    const { handleRecall } = require('../dist/tools/recall.js');
    const result = await handleRecall(mockClient, { query: '' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.memories).toEqual([]);
  });

  it('should return memories', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'User likes coffee',
            embedding: [],
            metadata: { importance: 0.7, tags: ['namespace:default', 'preference'] },
            decayScore: 0.7,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.85,
          textScore: 0.9,
          decayAdjustedScore: 0.7,
        },
      ]),
    });
    const { handleRecall } = require('../dist/tools/recall.js');
    const result = await handleRecall(mockClient, { query: 'coffee preferences' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.memories).toHaveLength(1);
    expect(parsed.memories[0].fact_text).toBe('User likes coffee');
    expect(parsed.memories[0].importance).toBe(7);
  });

});

describe('handleExport', () => {
  it('should export as JSON', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Test fact',
            embedding: [],
            metadata: { importance: 0.5, tags: ['namespace:default'] },
            decayScore: 0.5,
            createdAt: new Date('2024-01-01'),
          },
          score: 1,
          vectorScore: 1,
          textScore: 1,
          decayAdjustedScore: 0.5,
        },
      ]),
    });
    const { handleExport } = require('../dist/tools/export.js');
    const result = await handleExport(mockClient, { format: 'json' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.format).toBe('json');
    expect(parsed.fact_count).toBe(1);
    const content = JSON.parse(parsed.content);
    expect(content.facts).toHaveLength(1);
  });

  it('should export as markdown', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Test fact',
            embedding: [],
            metadata: { importance: 0.5, tags: ['namespace:default'] },
            decayScore: 0.5,
            createdAt: new Date('2024-01-01'),
          },
          score: 1,
          vectorScore: 1,
          textScore: 1,
          decayAdjustedScore: 0.5,
        },
      ]),
    });
    const { handleExport } = require('../dist/tools/export.js');
    const result = await handleExport(mockClient, { format: 'markdown' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.format).toBe('markdown');
    expect(parsed.content).toContain('# TotalReclaw Export');
    expect(parsed.content).toContain('Test fact');
  });
});

describe('handleImport', () => {
  it('should validate only when validate_only is true', async () => {
    const mockClient = createMockClient();
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '{"facts":[{"text":"Test fact","type":"fact","importance":5,"confidence":0.9,"action":"ADD","entities":[],"relations":[]}]}',
      format: 'json',
      validate_only: true,
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.facts_imported).toBe(0);
    expect(parsed.warnings).toContain('Validate-only mode: no facts were imported');
    expect(mockClient.remember).not.toHaveBeenCalled();
  });

  it('should import facts from JSON', async () => {
    const mockClient = createMockClient();
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '{"facts":[{"text":"Test fact","type":"fact","importance":5,"confidence":0.9,"action":"ADD","entities":[],"relations":[]}]}',
      format: 'json',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(true);
    expect(parsed.facts_imported).toBe(1);
    expect(mockClient.remember).toHaveBeenCalled();
  });

  it('should detect format automatically', async () => {
    const mockClient = createMockClient();
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '[]',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(true);
  });

  it('should import from markdown format', async () => {
    const mockClient = createMockClient();
    const { handleImport } = require('../dist/tools/import.js');
    const markdownContent = `# TotalReclaw Export

## User prefers dark mode
**Type:** preference
**Importance:** 7
**Namespace:** work
ID: \`fact-123\`

---

## Project uses TypeScript
**Type:** decision
**Importance:** 8
`;
    const result = await handleImport(mockClient, {
      content: markdownContent,
      format: 'markdown',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(true);
    expect(parsed.facts_imported).toBe(2);
  });

  it('should handle skip_existing merge strategy', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'existing-1',
            text: 'Existing fact',
            embedding: [],
            metadata: {},
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 1,
          vectorScore: 1,
          textScore: 1,
          decayAdjustedScore: 0.5,
        },
      ]),
    });
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '{"facts":[{"text":"Existing fact","type":"fact","importance":5}]}',
      format: 'json',
      merge_strategy: 'skip_existing',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.facts_skipped).toBe(1);
    expect(parsed.facts_imported).toBe(0);
    expect(mockClient.remember).not.toHaveBeenCalled();
  });

  it('should handle overwrite merge strategy', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'existing-1',
            text: 'Existing fact',
            embedding: [],
            metadata: {},
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 1,
          vectorScore: 1,
          textScore: 1,
          decayAdjustedScore: 0.5,
        },
      ]),
    });
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '{"facts":[{"text":"Existing fact","type":"fact","importance":8}]}',
      format: 'json',
      merge_strategy: 'overwrite',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(mockClient.forget).toHaveBeenCalledWith('existing-1');
    expect(parsed.facts_imported).toBe(1);
  });

  it('should reject empty content', async () => {
    const mockClient = createMockClient();
    const { handleImport } = require('../dist/tools/import.js');
    const result = await handleImport(mockClient, {
      content: '',
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.errors[0].error).toContain('required');
  });
});

describe('handleForget', () => {
  it('should forget by fact_id', async () => {
    const mockClient = createMockClient();
    const { handleForget } = require('../dist/tools/forget.js');
    const result = await handleForget(mockClient, { fact_id: 'fact-123' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.deleted_count).toBe(1);
    expect(parsed.fact_ids).toContain('fact-123');
    expect(mockClient.forget).toHaveBeenCalledWith('fact-123');
  });

  it('should forget by query', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Old coffee preference',
            embedding: [],
            metadata: { tags: ['namespace:default'] },
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.5,
        },
        {
          fact: {
            id: 'fact-2',
            text: 'Another coffee note',
            embedding: [],
            metadata: { tags: ['namespace:default'] },
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 0.85,
          vectorScore: 0.85,
          textScore: 0.85,
          decayAdjustedScore: 0.5,
        },
      ]),
    });
    const { handleForget } = require('../dist/tools/forget.js');
    const result = await handleForget(mockClient, { query: 'coffee' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.deleted_count).toBe(2);
  });

  it('should require fact_id or query', async () => {
    const mockClient = createMockClient();
    const { handleForget } = require('../dist/tools/forget.js');
    const result = await handleForget(mockClient, {});

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.deleted_count).toBe(0);
    expect(parsed.error).toContain('fact_id or query');
  });

  it('should handle forget errors gracefully', async () => {
    const mockClient = createMockClient({
      forget: jest.fn().mockRejectedValue(new Error('Storage error')),
    });
    const { handleForget } = require('../dist/tools/forget.js');
    const result = await handleForget(mockClient, { fact_id: 'fact-123' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.error).toContain('Failed to forget');
  });
});

describe('handleRemember extended', () => {
  it('should store fact with type metadata', async () => {
    const mockClient = createMockClient({ remember: jest.fn().mockResolvedValue('fact-789') });
    const { handleRemember } = require('../dist/tools/remember.js');
    const result = await handleRemember(mockClient, {
      fact: 'Important decision',
      importance: 9,
      metadata: { type: 'decision' },
    });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(true);
    expect(mockClient.remember).toHaveBeenCalledWith(
      'Important decision',
      expect.objectContaining({
        importance: 0.9,
        tags: expect.arrayContaining(['decision']),
      })
    );
  });

  it('should handle storage errors', async () => {
    const mockClient = createMockClient({
      remember: jest.fn().mockRejectedValue(new Error('DB connection failed')),
    });
    const { handleRemember } = require('../dist/tools/remember.js');
    const result = await handleRemember(mockClient, { fact: 'Test fact' });

    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.success).toBe(false);
    expect(parsed.error).toContain('DB connection failed');
  });
});

describe('End-to-End Flows', () => {
  it('should store and recall facts', async () => {
    const storedFacts = [];
    const mockClient = {
      remember: jest.fn((text, metadata) => {
        const id = `fact-${storedFacts.length + 1}`;
        storedFacts.push({ id, text, metadata });
        return Promise.resolve(id);
      }),
      recall: jest.fn((query, k) => {
        return Promise.resolve(storedFacts.map(f => ({
          fact: {
            id: f.id,
            text: f.text,
            embedding: [],
            metadata: f.metadata,
            decayScore: f.metadata.importance || 0.5,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: f.metadata.importance || 0.5,
        })));
      }),
      forget: jest.fn().mockResolvedValue(undefined),
      isReady: jest.fn().mockReturnValue(true),
    };

    const { handleRemember } = require('../dist/tools/remember.js');
    const { handleRecall } = require('../dist/tools/recall.js');

    await handleRemember(mockClient, { fact: 'User likes TypeScript' });
    await handleRemember(mockClient, { fact: 'User prefers dark mode' });

    const recallResult = await handleRecall(mockClient, { query: 'preferences' });
    const parsed = JSON.parse(recallResult.content[0].text);

    expect(parsed.memories.length).toBe(2);
  });

  it('should export and import facts', async () => {
    const sourceFact = {
      id: 'fact-1',
      text: 'Fact to export',
      metadata: { tags: ['preference'], importance: 0.7 },
      decayScore: 0.7,
      createdAt: new Date(),
    };

    const exportClient = {
      recall: jest.fn().mockResolvedValue([{
        fact: sourceFact,
        score: 1,
        vectorScore: 1,
        textScore: 1,
        decayAdjustedScore: 0.7,
      }]),
      isReady: jest.fn().mockReturnValue(true),
    };

    const { handleExport } = require('../dist/tools/export.js');
    const exportResult = await handleExport(exportClient, { format: 'json' });
    const exportParsed = JSON.parse(exportResult.content[0].text);

    expect(exportParsed.fact_count).toBe(1);

    const importClient = {
      recall: jest.fn().mockResolvedValue([]),
      remember: jest.fn().mockResolvedValue('imported-1'),
      forget: jest.fn().mockResolvedValue(undefined),
      isReady: jest.fn().mockReturnValue(true),
    };

    const { handleImport } = require('../dist/tools/import.js');
    const importResult = await handleImport(importClient, {
      content: exportParsed.content,
      format: 'json',
    });
    const importParsed = JSON.parse(importResult.content[0].text);

    expect(importParsed.facts_imported).toBe(1);
  });
});
