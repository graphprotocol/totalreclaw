/**
 * @jest-environment node
 */

/**
 * Error Propagation Tests
 *
 * Validates that every tool handler properly catches errors from the client
 * and returns well-formed JSON error responses (never throws).
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

describe('Error Propagation', () => {
  describe('handleRemember error paths', () => {
    const { handleRemember } = require('../dist/tools/remember.js');

    it('catches network error from client.remember', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockRejectedValue(new Error('ECONNREFUSED: connection refused')),
      });
      const result = await handleRemember({ client: mockClient }, { fact: 'Test fact' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('ECONNREFUSED');
    });

    it('catches timeout error from client.remember', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockRejectedValue(new Error('Request timed out after 30000ms')),
      });
      const result = await handleRemember({ client: mockClient }, { fact: 'Test fact' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('timed out');
    });

    it('handles non-Error thrown values', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockRejectedValue('string error'),
      });
      const result = await handleRemember({ client: mockClient }, { fact: 'Test fact' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
      expect(parsed.error).toContain('Unknown error');
    });

    it('returns valid JSON structure even on error', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockRejectedValue(new Error('DB crash')),
      });
      const result = await handleRemember({ client: mockClient }, { fact: 'Test' }, 'default');
      expect(result.content).toHaveLength(1);
      expect(result.content[0].type).toBe('text');
      // Ensure it is valid JSON
      expect(() => JSON.parse(result.content[0].text)).not.toThrow();
    });

    it('batch mode: individual failure does not stop other facts', async () => {
      let callCount = 0;
      const mockClient = createMockClient({
        remember: jest.fn().mockImplementation(() => {
          callCount++;
          if (callCount === 2) {
            return Promise.reject(new Error('Intermittent error'));
          }
          return Promise.resolve(`fact-${callCount}`);
        }),
      });

      const result = await handleRemember({ client: mockClient }, {
        facts: [
          { text: 'Fact A' },
          { text: 'Fact B' },
          { text: 'Fact C' },
        ],
      }, 'default');

      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.total).toBe(3);
      expect(parsed.created).toBe(2);
      expect(parsed.skipped).toBe(1);
      expect(parsed.success).toBe(true); // overall success because some worked
    });
  });

  describe('handleRecall error paths', () => {
    const { handleRecall } = require('../dist/tools/recall.js');

    it('catches network error from client.recall', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('ECONNREFUSED: connection refused')),
      });
      const result = await handleRecall({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
      expect(parsed.error).toContain('ECONNREFUSED');
    });

    it('catches timeout error from client.recall', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('Request timed out')),
      });
      const result = await handleRecall({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
      expect(parsed.error).toContain('timed out');
    });

    it('handles non-Error thrown values from recall', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(42),
      });
      const result = await handleRecall({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.memories).toEqual([]);
      expect(parsed.error).toContain('Unknown error');
    });

    it('returns latency_ms even on error', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('fail')),
      });
      const result = await handleRecall({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(typeof parsed.latency_ms).toBe('number');
      expect(parsed.latency_ms).toBeGreaterThanOrEqual(0);
    });
  });

  describe('handleForget error paths', () => {
    const { handleForget } = require('../dist/tools/forget.js');

    it('catches error from client.forget (by fact_id)', async () => {
      const mockClient = createMockClient({
        forget: jest.fn().mockRejectedValue(new Error('Storage error')),
      });
      const result = await handleForget({ client: mockClient }, { fact_id: 'fact-123' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toContain('Failed to forget');
      expect(parsed.deleted_count).toBe(0);
    });

    it('catches error from client.recall when forgetting by query', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('Recall failed during forget')),
      });
      const result = await handleForget({ client: mockClient }, { query: 'old stuff' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toContain('Failed to forget');
      expect(parsed.deleted_count).toBe(0);
    });

    it('partial failure during query-based forget: skips failed items', async () => {
      let forgetCount = 0;
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([
          {
            fact: {
              id: 'fact-1', text: 'A', embedding: [],
              metadata: { tags: ['namespace:default'] }, decayScore: 0.5, createdAt: new Date(),
            },
            score: 0.9, vectorScore: 0.9, textScore: 0.9, decayAdjustedScore: 0.5,
          },
          {
            fact: {
              id: 'fact-2', text: 'B', embedding: [],
              metadata: { tags: ['namespace:default'] }, decayScore: 0.5, createdAt: new Date(),
            },
            score: 0.8, vectorScore: 0.8, textScore: 0.8, decayAdjustedScore: 0.5,
          },
        ]),
        forget: jest.fn().mockImplementation((id) => {
          forgetCount++;
          if (id === 'fact-2') return Promise.reject(new Error('Cannot delete'));
          return Promise.resolve();
        }),
      });

      const result = await handleForget({ client: mockClient }, { query: 'test' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // fact-1 succeeds, fact-2 fails silently
      expect(parsed.deleted_count).toBe(1);
      expect(parsed.fact_ids).toContain('fact-1');
      expect(parsed.fact_ids).not.toContain('fact-2');
    });
  });

  describe('handleExport error paths', () => {
    const { handleExport } = require('../dist/tools/export.js');

    it('catches error from client.recall during export', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('DB unavailable')),
      });
      const result = await handleExport({ client: mockClient }, { format: 'json' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toContain('Failed to export');
      expect(parsed.fact_count).toBe(0);
      expect(parsed.content).toBe('');
    });

    it('returns valid structure on error (format field preserved)', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('fail')),
      });
      const result = await handleExport({ client: mockClient }, { format: 'markdown' }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.format).toBe('markdown');
      expect(typeof parsed.exported_at).toBe('string');
    });
  });

  describe('handleImport error paths', () => {
    const { handleImport } = require('../dist/tools/import.js');

    it('catches JSON parse error in content', async () => {
      const mockClient = createMockClient();
      const result = await handleImport({ client: mockClient }, {
        content: '{invalid json!!!}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      // The import still returns a result structure (with parse errors)
      expect(parsed.errors.length).toBeGreaterThan(0);
      expect(parsed.errors[0].error).toContain('JSON parse error');
    });

    it('catches client.remember failure during import', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([]),
        remember: jest.fn().mockRejectedValue(new Error('Storage full')),
      });
      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"text":"Test fact"}]}',
        format: 'json',
      }, 'default');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.facts_imported).toBe(0);
      expect(parsed.errors.length).toBeGreaterThan(0);
      expect(parsed.errors[0].error).toContain('Failed to store fact');
    });

    it('catches client.recall failure during duplicate check', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('Recall service down')),
      });

      // handleImport calls client.recall('*', 1000) for duplicate detection;
      // if that throws, the whole import should fail
      await expect(async () => {
        await handleImport({ client: mockClient }, {
          content: '{"facts":[{"text":"Test"}]}',
          format: 'json',
        }, 'default');
      }).rejects.toThrow('Recall service down');
    });

    it('catches client.forget failure during overwrite merge', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockResolvedValue([
          {
            fact: {
              id: 'existing-1', text: 'existing fact', embedding: [],
              metadata: {}, decayScore: 0.5, createdAt: new Date(),
            },
            score: 1, vectorScore: 1, textScore: 1, decayAdjustedScore: 0.5,
          },
        ]),
        forget: jest.fn().mockRejectedValue(new Error('Cannot delete')),
        remember: jest.fn().mockResolvedValue('new-id'),
      });

      const result = await handleImport({ client: mockClient }, {
        content: '{"facts":[{"text":"Existing fact"}]}',
        format: 'json',
        merge_strategy: 'overwrite',
      }, 'default');

      const parsed = JSON.parse(result.content[0].text);
      // The fact should be skipped (not imported) because forget failed
      expect(parsed.facts_skipped).toBe(1);
      expect(parsed.errors.length).toBeGreaterThan(0);
      expect(parsed.errors[0].error).toContain('Failed to delete existing');
    });
  });

  describe('all handlers never throw (always return JSON)', () => {
    const { handleRemember } = require('../dist/tools/remember.js');
    const { handleRecall } = require('../dist/tools/recall.js');
    const { handleForget } = require('../dist/tools/forget.js');
    const { handleExport } = require('../dist/tools/export.js');

    it('remember with broken client returns error JSON', async () => {
      const mockClient = createMockClient({
        remember: jest.fn().mockRejectedValue(new Error('broken')),
      });
      const result = await handleRemember({ client: mockClient }, { fact: 'test' }, 'default');
      expect(result.content).toBeDefined();
      expect(result.content[0].type).toBe('text');
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.success).toBe(false);
    });

    it('recall with broken client returns error JSON', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('broken')),
      });
      const result = await handleRecall({ client: mockClient }, { query: 'test' }, 'default');
      expect(result.content).toBeDefined();
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toBeDefined();
    });

    it('forget with broken client returns error JSON', async () => {
      const mockClient = createMockClient({
        forget: jest.fn().mockRejectedValue(new Error('broken')),
      });
      const result = await handleForget({ client: mockClient }, { fact_id: 'x' }, 'default');
      expect(result.content).toBeDefined();
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toBeDefined();
    });

    it('export with broken client returns error JSON', async () => {
      const mockClient = createMockClient({
        recall: jest.fn().mockRejectedValue(new Error('broken')),
      });
      const result = await handleExport({ client: mockClient }, {}, 'default');
      expect(result.content).toBeDefined();
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.error).toBeDefined();
    });
  });
});
