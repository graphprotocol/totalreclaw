/**
 * @jest-environment node
 */

/**
 * Input Validation Edge Cases
 *
 * Tests boundary conditions and edge cases for all tool inputs.
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

function createMockClient(overrides = {}) {
  return {
    recall: jest.fn().mockResolvedValue([]),
    remember: jest.fn().mockResolvedValue('fact-id'),
    forget: jest.fn().mockResolvedValue(undefined),
    isReady: jest.fn().mockReturnValue(true),
    ...overrides,
  };
}

describe('Input Validation Edge Cases', () => {
  describe('handleRemember edge cases', () => {
    const { handleRemember } = require('../dist/tools/remember.js');

    it('rejects whitespace-only fact text', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: '   ' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('non-empty string');
    });

    it('accepts very long fact text (>10KB)', async () => {
      const longText = 'x'.repeat(12000);
      const mockClient = createMockClient({
        remember: jest.fn().mockResolvedValue('long-fact-id'),
      });
      const result = await handleRemember({ client: mockClient }, { fact: longText }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.fact_id).toBe('long-fact-id');
      expect(mockClient.remember).toHaveBeenCalledWith(
        longText, // text is trimmed, but 'x'.repeat(12000) has no leading/trailing whitespace
        expect.any(Object)
      );
    });

    it('trims leading/trailing whitespace from fact text', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockResolvedValue('trimmed-fact-id'),
      });
      const result = await handleRemember({ client: mockClient }, { fact: '  Hello world  ' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(mockClient.remember).toHaveBeenCalledWith(
        'Hello world',
        expect.any(Object)
      );
    });

    it('rejects importance of 0 (below minimum)', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: 0 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('between 1 and 10');
    });

    it('rejects importance of 11 (above maximum)', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: 11 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('between 1 and 10');
    });

    it('rejects negative importance', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: -5 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('between 1 and 10');
    });

    it('accepts importance at boundary (1)', async () => {
      const mockClient = createMockClient({ remember: jest.fn().mockResolvedValue('min-imp') });
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: 1 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(mockClient.remember).toHaveBeenCalledWith('test', expect.objectContaining({ importance: 0.1 }));
    });

    it('accepts importance at boundary (10)', async () => {
      const mockClient = createMockClient({ remember: jest.fn().mockResolvedValue('max-imp') });
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: 10 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(mockClient.remember).toHaveBeenCalledWith('test', expect.objectContaining({ importance: 1.0 }));
    });

    it('rejects non-numeric importance (string)', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: 'test', importance: 'high' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
    });

    it('batch with 0 facts returns invalid input error', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { facts: [] }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Empty array is not considered a valid batch (isBatch checks length > 0)
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('provide either');
    });

    it('batch with mixed valid/invalid facts processes both', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockResolvedValue('batch-fact-id'),
      });
      const result = await handleRemember({ client: mockClient }, {
        facts: [
          { text: 'Valid fact one' },
          { text: '' },  // invalid - empty text
          { text: 'Valid fact three' },
          { text: '   ' },  // invalid - whitespace only
          { text: 'Valid fact five', importance: 99 }, // invalid - out of range importance
        ],
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.total).toBe(5);
      expect(parsed.created).toBe(2); // only the valid ones
      expect(parsed.skipped).toBe(3); // 3 invalid
    });

    it('rejects undefined args', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, undefined, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
    });

    it('rejects null args', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, null, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
    });

    it('rejects numeric fact (not a string)', async () => {
      const mockClient = createMockClient();
      const result = await handleRemember({ client: mockClient }, { fact: 12345 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
    });
  });

  describe('handleRecall edge cases', () => {
    const { handleRecall } = require('../dist/tools/recall.js');

    it('rejects whitespace-only query', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: '   ' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
      expect(parsed.error).toContain('non-empty string');
    });

    it('accepts very long query string', async () => {
      const longQuery = 'test '.repeat(2000);
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: longQuery }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Should not error -- handler trims and passes to client
      expect(parsed.memories).toEqual([]);
      expect(mockClient.recall).toHaveBeenCalledWith(longQuery.trim(), 8);
    });

    it('clamps k=0 to default (8)', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: 'test', k: 0 }, 'default');
      // k < 1 is reset to 8
      expect(mockClient.recall).toHaveBeenCalledWith('test', 8);
    });

    it('clamps negative k to default (8)', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: 'test', k: -5 }, 'default');
      expect(mockClient.recall).toHaveBeenCalledWith('test', 8);
    });

    it('clamps k=100 to maximum (50)', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: 'test', k: 100 }, 'default');
      expect(mockClient.recall).toHaveBeenCalledWith('test', 50);
    });

    it('accepts k=1 (minimum valid)', async () => {
      const mockClient = createMockClient();
      await handleRecall({ client: mockClient }, { query: 'test', k: 1 }, 'default');
      expect(mockClient.recall).toHaveBeenCalledWith('test', 1);
    });

    it('accepts k=50 (maximum valid)', async () => {
      const mockClient = createMockClient();
      await handleRecall({ client: mockClient }, { query: 'test', k: 50 }, 'default');
      expect(mockClient.recall).toHaveBeenCalledWith('test', 50);
    });

    it('rejects undefined query', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: undefined }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
    });

    it('rejects numeric query', async () => {
      const mockClient = createMockClient();
      const result = await handleRecall({ client: mockClient }, { query: 12345 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
    });

    it('min_importance filters results', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([
          {
            fact: {
              id: 'f1', text: 'High importance', embedding: [],
              metadata: { importance: 0.9, tags: ['namespace:default'] },
              decayScore: 0.9, createdAt: new Date(),
            },
            score: 0.9, vectorScore: 0.9, textScore: 0.9, decayAdjustedScore: 0.9,
          },
          {
            fact: {
              id: 'f2', text: 'Low importance', embedding: [],
              metadata: { importance: 0.2, tags: ['namespace:default'] },
              decayScore: 0.2, createdAt: new Date(),
            },
            score: 0.8, vectorScore: 0.8, textScore: 0.8, decayAdjustedScore: 0.2,
          },
        ]),
      });

      const result = await handleRecall({ client: mockClient }, { query: 'test', min_importance: 5 }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toHaveLength(1);
      expect(parsed.memories[0].fact_text).toBe('High importance');
    });
  });

  describe('handleForget edge cases', () => {
    const { handleForget } = require('../dist/tools/forget.js');

    it('rejects empty fact_id string', async () => {
      const mockClient = createMockClient();
      const result = await handleForget({ client: mockClient }, { fact_id: '' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Empty string is falsy, so falls through to "requires fact_id or query"
      expect(parsed.error).toContain('fact_id or query');
      expect(parsed.deleted_count).toBe(0);
    });

    it('rejects empty object (no fact_id, no query)', async () => {
      const mockClient = createMockClient();
      const result = await handleForget({ client: mockClient }, {}, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toContain('fact_id or query');
    });

    it('throws on undefined args (unguarded cast)', async () => {
      // Note: unlike other handlers, handleForget does not guard against undefined args.
      // Passing undefined causes a TypeError because it accesses .fact_id on undefined.
      // This documents the current behavior -- a defensive fix would add an input guard.
      const mockClient = createMockClient();
      await expect(
        handleForget({ client: mockClient }, undefined, 'default')
      ).rejects.toThrow(TypeError);
    });

    it('handles whitespace-only query (still attempts recall)', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([]),
      });
      const result = await handleForget({ client: mockClient }, { query: '   ' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Whitespace query is truthy, so it proceeds to recall
      expect(parsed.deleted_count).toBe(0);
      expect(mockClient.recall).toHaveBeenCalled();
    });

    it('forget by query with namespace=default does not filter', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([
          {
            fact: {
              id: 'f1', text: 'No namespace tag', embedding: [],
              metadata: { tags: [] },
              decayScore: 0.5, createdAt: new Date(),
            },
            score: 0.9, vectorScore: 0.9, textScore: 0.9, decayAdjustedScore: 0.5,
          },
        ]),
      });
      const result = await handleForget({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // namespace=default means no filtering
      expect(parsed.deleted_count).toBe(1);
    });
  });

  describe('handleImport edge cases', () => {
    const { handleImport } = require('../dist/tools/import.js');

    it('rejects empty content string', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, { content: '' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.errors[0].error).toContain('required');
    });

    it('rejects content with only whitespace', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, { content: '   ' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Whitespace-only content is truthy but will fail to parse
      // It will be treated as markdown (does not start with { or [)
      // and markdown parser will find no ## headings
      expect(parsed.facts_imported).toBe(0);
    });

    it('rejects null content', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, { content: null }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.errors[0].error).toContain('required');
    });

    it('rejects undefined content', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {}, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.errors[0].error).toContain('required');
    });

    it('handles JSON with empty facts array', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[]}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.facts_imported).toBe(0);
    });

    it('handles bare JSON array (no wrapping object)', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '[{"text":"Bare array fact"}]',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.facts_imported).toBe(1);
    });

    it('warns about out-of-range importance in imported facts', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"text":"Out-of-range fact","importance":15}]}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.warnings.length).toBeGreaterThan(0);
      expect(parsed.warnings[0]).toContain('out of range');
    });

    it('handles markdown with no ## headings (no parseable facts)', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '# Title\nSome text without any fact headings.\n---\nMore text.',
        format: 'markdown',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.facts_imported).toBe(0);
    });

    it('auto-detects JSON format from { prefix', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"text":"Auto-detected JSON"}]}',
        // no format specified -- should auto-detect
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.facts_imported).toBe(1);
    });

    it('auto-detects JSON format from [ prefix', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '[{"text":"Auto-detected array"}]',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.facts_imported).toBe(1);
    });

    it('auto-detects markdown format (non-JSON prefix)', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '# Export\n---\n## Some fact\n**Importance:** 5\n---',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(true);
      expect(parsed.facts_imported).toBe(1);
    });

    it('skips facts with empty text in JSON', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"text":""},{"text":"Valid fact"}]}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // Empty text should be reported as an error during parsing
      expect(parsed.errors.length).toBeGreaterThan(0);
      expect(parsed.facts_imported).toBe(1);
    });

    it('handles JSON missing the text field', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"importance":5}]}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.errors.length).toBeGreaterThan(0);
      expect(parsed.errors[0].error).toContain('missing or invalid text');
    });
  });

  describe('handleExport edge cases', () => {
    const { handleExport } = require('../dist/tools/export.js');

    it('defaults to markdown format when format is not specified', async () => {
      const mockClient = createMockClient();
      const result = await handleExport({ client: mockClient }, {}, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.format).toBe('markdown');
    });

    it('exports empty vault as JSON with 0 facts', async () => {
      const mockClient = createMockClient();
      const result = await handleExport({ client: mockClient }, { format: 'json' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.fact_count).toBe(0);
      const content = JSON.parse(parsed.content);
      expect(content.facts).toHaveLength(0);
    });

    it('exports empty vault as markdown with 0 facts', async () => {
      const mockClient = createMockClient();
      const result = await handleExport({ client: mockClient }, { format: 'markdown' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.fact_count).toBe(0);
      expect(parsed.content).toContain('Total Facts:** 0');
    });

    it('handles null args (defaults applied)', async () => {
      const mockClient = createMockClient();
      const result = await handleExport({ client: mockClient }, null, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.format).toBe('markdown');
    });

    it('include_metadata=false excludes metadata from markdown', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([
          {
            fact: {
              id: 'f1', text: 'Test fact', embedding: [],
              metadata: { importance: 0.7, tags: ['namespace:default', 'preference'] },
              decayScore: 0.7, createdAt: new Date('2024-06-01'),
            },
            score: 1, vectorScore: 1, textScore: 1, decayAdjustedScore: 0.7,
          },
        ]),
      });
      const result = await handleExport({ client: mockClient }, { format: 'markdown', include_metadata: false }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.content).not.toContain('**Importance:**');
      expect(parsed.content).not.toContain('**Tags:**');
    });
  });
});

describe('Prompts validation', () => {
  const { PROMPT_DEFINITIONS, getPromptMessages, SERVER_INSTRUCTIONS } = require('../dist/prompts.js');

  it('PROMPT_DEFINITIONS has at least 2 prompts', () => {
    expect(PROMPT_DEFINITIONS.length).toBeGreaterThanOrEqual(2);
  });

  it('each prompt has name and description', () => {
    for (const p of PROMPT_DEFINITIONS) {
      expect(typeof p.name).toBe('string');
      expect(p.name.length).toBeGreaterThan(0);
      expect(typeof p.description).toBe('string');
      expect(p.description.length).toBeGreaterThan(0);
    }
  });

  it('getPromptMessages returns messages for totalreclaw_start', () => {
    const msgs = getPromptMessages('totalreclaw_start', { topic: 'cooking' });
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs[0].role).toBe('user');
    expect(msgs[0].content.text).toContain('cooking');
  });

  it('getPromptMessages returns messages for totalreclaw_save', () => {
    const msgs = getPromptMessages('totalreclaw_save');
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs[0].role).toBe('user');
    expect(msgs[0].content.text).toContain('extract');
  });

  it('getPromptMessages returns messages for totalreclaw_instructions', () => {
    const msgs = getPromptMessages('totalreclaw_instructions');
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs[0].content.text).toContain('TotalReclaw');
  });

  it('getPromptMessages throws for unknown prompt', () => {
    expect(() => getPromptMessages('unknown_prompt')).toThrow('Unknown prompt');
  });

  it('totalreclaw_start defaults topic to "recent context" when no args', () => {
    const msgs = getPromptMessages('totalreclaw_start');
    expect(msgs[0].content.text).toContain('recent context');
  });

  it('SERVER_INSTRUCTIONS is a non-empty string', () => {
    expect(typeof SERVER_INSTRUCTIONS).toBe('string');
    expect(SERVER_INSTRUCTIONS.length).toBeGreaterThan(100);
  });
});
