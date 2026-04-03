/**
 * End-to-end tests for the MCP consolidate tool.
 *
 * Tests the full flow: recall all facts → cluster by cosine → batch delete.
 * Uses mocked client to verify the correct sequence of operations.
 */
export {}; // make this file a module to avoid TS2451 scope collision with other test files

const mockRecall = jest.fn();
const mockRemember = jest.fn();
const mockForget = jest.fn();

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

function createMockClient() {
  return {
    recall: mockRecall,
    remember: mockRemember,
    forget: mockForget,
    isReady: jest.fn().mockReturnValue(true),
  };
}

// Helper: make a RerankedResult with a specific embedding
function makeResult(
  id: string,
  text: string,
  embedding: number[],
  importance: number = 0.5,
  createdAt: Date = new Date(),
) {
  return {
    fact: {
      id,
      text,
      embedding,
      metadata: { importance, tags: [] },
      decayScore: importance,
      createdAt,
    },
    score: 1.0,
    vectorScore: 1.0,
    textScore: 1.0,
    decayAdjustedScore: importance,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRecall.mockResolvedValue([]);
  mockForget.mockResolvedValue(undefined);
});

describe('handleConsolidate', () => {
  it('should report "no memories" for empty vault', async () => {
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, {});
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.scanned).toBe(0);
    expect(parsed.clusters).toBe(0);
    expect(parsed.duplicates).toBe(0);
  });

  it('should report "no duplicates" when all facts are unique', async () => {
    mockRecall.mockResolvedValue([
      makeResult('a', 'User likes TypeScript', [1, 0, 0]),
      makeResult('b', 'Project uses PostgreSQL', [0, 1, 0]),
      makeResult('c', 'Deploy to Railway', [0, 0, 1]),
    ]);
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, {});
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.scanned).toBe(3);
    expect(parsed.clusters).toBe(0);
    expect(parsed.duplicates).toBe(0);
    expect(mockForget).not.toHaveBeenCalled();
  });

  it('should find cluster and delete duplicates (non-dry_run)', async () => {
    // Two facts with identical embeddings — a clear duplicate pair
    mockRecall.mockResolvedValue([
      makeResult('keep', 'User prefers TypeScript', [1, 0, 0], 0.8),
      makeResult('dup', 'User likes TypeScript', [1, 0, 0], 0.5),
      makeResult('unrelated', 'Deploy to Railway', [0, 1, 0], 0.5),
    ]);
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, { dry_run: false });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.scanned).toBe(3);
    expect(parsed.clusters).toBe(1);
    expect(parsed.duplicates).toBe(1);
    expect(parsed.deleted).toBe(1);
    // The lower-importance fact should be deleted
    expect(mockForget).toHaveBeenCalledWith('dup');
  });

  it('should NOT delete in dry_run mode', async () => {
    mockRecall.mockResolvedValue([
      makeResult('keep', 'User prefers TypeScript', [1, 0, 0], 0.8),
      makeResult('dup', 'User likes TypeScript', [1, 0, 0], 0.5),
    ]);
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, { dry_run: true });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.clusters).toBe(1);
    expect(parsed.duplicates).toBe(1);
    expect(parsed.deleted).toBe(0);
    expect(parsed.dry_run).toBe(true);
    expect(mockForget).not.toHaveBeenCalled();
  });

  it('should keep the fact with highest decayScore as representative', async () => {
    // 'important' has higher importance (decayScore) → should be kept
    mockRecall.mockResolvedValue([
      makeResult('weak', 'User likes TS', [1, 0, 0], 0.3),
      makeResult('important', 'User prefers TypeScript', [1, 0, 0], 0.9),
    ]);
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, { dry_run: false });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.deleted).toBe(1);
    // 'weak' should be deleted, 'important' kept
    expect(mockForget).toHaveBeenCalledWith('weak');
    expect(mockForget).not.toHaveBeenCalledWith('important');
  });

  it('should handle forget failures gracefully (skip individual)', async () => {
    mockRecall.mockResolvedValue([
      makeResult('keep', 'Fact A', [1, 0, 0], 0.8),
      makeResult('dup1', 'Fact A variant', [1, 0, 0], 0.3),
      makeResult('dup2', 'Fact A similar', [1, 0, 0], 0.2),
    ]);
    // First forget fails, second succeeds
    mockForget
      .mockRejectedValueOnce(new Error('Delete failed'))
      .mockResolvedValueOnce(undefined);
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, { dry_run: false });
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(true);
    expect(parsed.deleted).toBe(1); // Only 1 of 2 succeeded
  });

  it('should handle recall failure gracefully', async () => {
    mockRecall.mockRejectedValue(new Error('Network error'));
    const client = createMockClient();
    const { handleConsolidate } = require('../src/tools/consolidate');

    const result = await handleConsolidate(client, {});
    const parsed = JSON.parse(result.content[0].text);

    expect(parsed.success).toBe(false);
    expect(parsed.error).toContain('Failed to consolidate');
  });
});
