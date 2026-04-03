/**
 * Store-time dedup wiring tests for MCP remember tool.
 *
 * These tests verify that handleRemember() correctly:
 * 1. Searches for near-duplicates via recall()
 * 2. Applies shouldSupersede() logic for batch mode
 * 3. Always supersedes for explicit (single-fact) remember
 * 4. Calls forget() to delete superseded facts
 * 5. Inherits higher importance from existing facts
 * 6. Respects the TOTALRECLAW_STORE_DEDUP env flag
 *
 * Run with:
 *   npx jest tests/remember-dedup.test.ts --no-cache
 */

export {}; // make this file a module to avoid TS2451 scope collision with other test files

// We need to mock the @totalreclaw/client module before any imports
const mockRecall = jest.fn();
const mockRemember = jest.fn();
const mockForget = jest.fn();

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

function createMockClient(overrides: Record<string, unknown> = {}) {
  return {
    recall: mockRecall,
    remember: mockRemember,
    forget: mockForget,
    isReady: jest.fn().mockReturnValue(true),
    ...overrides,
  };
}

/**
 * Helper: create a RerankedResult-like object matching the shape
 * expected by searchForNearDuplicatesHTTP.
 *
 * @param id          - Fact ID
 * @param text        - Fact text
 * @param vectorScore - Cosine similarity score (0-1)
 * @param importance  - Importance on 0-1 scale (metadata.importance)
 */
function makeResult(id: string, text: string, vectorScore: number, importance: number = 0.5) {
  return {
    fact: {
      id,
      text,
      embedding: [1, 0, 0],
      metadata: { importance, tags: [] },
      decayScore: importance,
      createdAt: new Date(),
    },
    score: vectorScore,
    vectorScore,
    textScore: vectorScore,
    decayAdjustedScore: importance,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  // Default: recall returns no results (no duplicates)
  mockRecall.mockResolvedValue([]);
  mockRemember.mockResolvedValue('new-fact-id');
  mockForget.mockResolvedValue(undefined);
  // Ensure store-time dedup is enabled
  process.env.TOTALRECLAW_STORE_DEDUP = 'true';
});

// ---------------------------------------------------------------------------
// Single-fact (explicit remember) — always supersedes near-duplicates
// ---------------------------------------------------------------------------

describe('Store-time dedup wiring: single-fact (explicit remember)', () => {
  it('should store normally when no near-duplicates exist', async () => {
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, { fact: 'User likes TypeScript' });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.action).toBe('created');
    expect(parsed.was_duplicate).toBe(false);
    expect(mockRecall).toHaveBeenCalledWith('User likes TypeScript', 200);
    expect(mockRemember).toHaveBeenCalled();
    expect(mockForget).not.toHaveBeenCalled();
  });

  it('should supersede when near-duplicate found (explicit always supersedes)', async () => {
    mockRecall.mockResolvedValue([
      makeResult('old-fact-1', 'User prefers TypeScript', 0.92, 0.5),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, { fact: 'User likes TypeScript', importance: 7 });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.action).toBe('updated');
    expect(parsed.was_duplicate).toBe(true);
    expect(parsed.superseded_id).toBe('old-fact-1');
    expect(mockForget).toHaveBeenCalledWith('old-fact-1');
    expect(mockRemember).toHaveBeenCalled();
  });

  it('should inherit higher importance from existing fact', async () => {
    // Existing fact has importance 0.9 (= 9 on 1-10 scale)
    mockRecall.mockResolvedValue([
      makeResult('old-fact-1', 'User prefers TypeScript', 0.92, 0.9),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    await handleRemember(client, { fact: 'User likes TypeScript', importance: 5 });

    // Should store with max(5, 9) = 9 → 0.9 importance
    const rememberCall = mockRemember.mock.calls[0];
    expect(rememberCall[1].importance).toBe(0.9);
  });

  it('should skip dedup search when vectorScore below threshold (0.85)', async () => {
    // vectorScore 0.70 < 0.85 threshold → not a near-duplicate
    mockRecall.mockResolvedValue([
      makeResult('unrelated-fact', 'User works at Acme', 0.70, 0.5),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, { fact: 'User likes TypeScript' });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.action).toBe('created');
    expect(parsed.was_duplicate).toBe(false);
    expect(mockForget).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Batch mode — uses shouldSupersede() logic
// ---------------------------------------------------------------------------

describe('Store-time dedup wiring: batch mode', () => {
  it('should skip fact when existing has higher importance (shouldSupersede -> skip)', async () => {
    // Existing fact importance = 0.9 (9 on 1-10 scale), new fact importance = 3
    mockRecall.mockResolvedValue([
      makeResult('existing-1', 'User prefers TypeScript', 0.90, 0.9),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, {
      facts: [{ text: 'User likes TypeScript', importance: 3 }],
    });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.dedup_skipped).toBe(1);
    expect(parsed.created).toBe(0);
    expect(mockForget).not.toHaveBeenCalled();
    expect(mockRemember).not.toHaveBeenCalled();
  });

  it('should supersede when new fact has higher importance', async () => {
    // Existing fact importance = 0.3 (3 on 1-10 scale), new fact importance = 8
    mockRecall.mockResolvedValue([
      makeResult('old-weak', 'User likes TS', 0.88, 0.3),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, {
      facts: [{ text: 'User strongly prefers TypeScript', importance: 8 }],
    });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.dedup_superseded).toBe(1);
    expect(parsed.created).toBe(1);
    expect(mockForget).toHaveBeenCalledWith('old-weak');
  });

  it('should supersede when equal importance (newer wins)', async () => {
    mockRecall.mockResolvedValue([
      makeResult('existing-equal', 'User likes TypeScript', 0.90, 0.5),
    ]);
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, {
      facts: [{ text: 'User prefers TypeScript', importance: 5 }],
    });
    const parsed = JSON.parse(result.content[0].text);

    // Equal importance → shouldSupersede returns 'supersede' (newer wins)
    expect(parsed.dedup_superseded).toBe(1);
    expect(mockForget).toHaveBeenCalledWith('existing-equal');
  });
});

// ---------------------------------------------------------------------------
// Feature flag — TOTALRECLAW_STORE_DEDUP=false disables dedup search
// ---------------------------------------------------------------------------

describe('Store-time dedup: feature flag', () => {
  it('should skip dedup search when TOTALRECLAW_STORE_DEDUP=false', async () => {
    process.env.TOTALRECLAW_STORE_DEDUP = 'false';
    // Need to re-require to pick up env change (STORE_DEDUP_ENABLED is a const)
    jest.resetModules();

    // Re-apply the mock after resetModules clears it
    jest.mock('@totalreclaw/client', () => ({
      TotalReclaw: jest.fn(),
    }));

    const { handleRemember } = require('../src/tools/remember');

    const client = createMockClient();
    const result = await handleRemember(client, { fact: 'Test fact' });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.action).toBe('created');
    // recall should NOT be called for dedup (only remember is called)
    expect(mockRecall).not.toHaveBeenCalled();

    // Restore for subsequent tests
    process.env.TOTALRECLAW_STORE_DEDUP = 'true';
  });
});

// ---------------------------------------------------------------------------
// Error handling — fail-open: dedup errors don't prevent storing
// ---------------------------------------------------------------------------

describe('Store-time dedup: error handling (fail-open)', () => {
  it('should store fact even when recall throws (fail-open)', async () => {
    mockRecall.mockRejectedValue(new Error('Network error'));
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, { fact: 'Important fact' });
    const parsed = JSON.parse(result.content[0].text);

    // Should still succeed — dedup failure doesn't prevent storing
    expect(parsed.success).toBe(true);
    expect(parsed.action).toBe('created');
    expect(mockRemember).toHaveBeenCalled();
  });

  it('should store fact even when forget throws during supersession', async () => {
    mockRecall.mockResolvedValue([
      makeResult('old-1', 'Old fact', 0.90, 0.5),
    ]);
    mockForget.mockRejectedValue(new Error('Delete failed'));
    const client = createMockClient();
    const { handleRemember } = require('../src/tools/remember');

    const result = await handleRemember(client, { fact: 'New fact' });
    const parsed = JSON.parse(result.content[0].text);

    // Should still store the new fact
    expect(parsed.success).toBe(true);
    expect(mockRemember).toHaveBeenCalled();
    // But superseded_id should not be reported since delete failed
    expect(parsed.superseded_id).toBeUndefined();
  });
});
