/**
 * @jest-environment node
 */

/**
 * Resource & Cache Tests
 *
 * Validates the memory context resource, caching behavior, and
 * cache invalidation triggered by remember/forget operations.
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

const {
  memoryContextResource,
  readMemoryContext,
  invalidateMemoryContextCache,
} = require('../dist/resources/memory-context.js');

const { setOnRememberCallback } = require('../dist/tools/remember.js');

function createMockClient(overrides = {}) {
  return {
    recall: jest.fn().mockResolvedValue([]),
    remember: jest.fn().mockResolvedValue('fact-id'),
    forget: jest.fn().mockResolvedValue(undefined),
    isReady: jest.fn().mockReturnValue(true),
    ...overrides,
  };
}

function makeFact(overrides = {}) {
  return {
    fact: {
      id: overrides.id || 'fact-1',
      text: overrides.text || 'Sample fact',
      embedding: [],
      metadata: {
        importance: overrides.importance ?? 0.7,
        tags: overrides.tags || ['namespace:default'],
        ...(overrides.metadata || {}),
      },
      decayScore: overrides.decayScore ?? 0.7,
      createdAt: overrides.createdAt || new Date(),
    },
    score: overrides.score ?? 0.9,
    vectorScore: overrides.vectorScore ?? 0.85,
    textScore: overrides.textScore ?? 0.9,
    decayAdjustedScore: overrides.decayAdjustedScore ?? 0.7,
  };
}

describe('Memory Context Resource Definition', () => {
  it('has correct URI', () => {
    expect(memoryContextResource.uri).toBe('memory://context/summary');
  });

  it('has a name and title', () => {
    expect(typeof memoryContextResource.name).toBe('string');
    expect(memoryContextResource.name.length).toBeGreaterThan(0);
    expect(typeof memoryContextResource.title).toBe('string');
    expect(memoryContextResource.title.length).toBeGreaterThan(0);
  });

  it('has text/markdown mimeType', () => {
    expect(memoryContextResource.mimeType).toBe('text/markdown');
  });

  it('has annotations with assistant audience and high priority', () => {
    expect(memoryContextResource.annotations.audience).toContain('assistant');
    expect(memoryContextResource.annotations.priority).toBeGreaterThanOrEqual(0.8);
  });
});

describe('readMemoryContext', () => {
  beforeEach(() => {
    // Always invalidate before each test to avoid cross-test cache interference
    invalidateMemoryContextCache();
  });

  it('returns empty-state message when no facts exist', async () => {
    const mockClient = createMockClient();
    const content = await readMemoryContext(mockClient);
    expect(content).toContain('Your Memory Context');
    expect(content).toContain('No memories stored yet');
  });

  it('returns markdown with high-priority facts', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        makeFact({ id: 'f1', text: 'User is vegan', importance: 0.9 }),
        makeFact({ id: 'f2', text: 'User lives in Lisbon', importance: 0.8 }),
      ]),
    });

    const content = await readMemoryContext(mockClient);
    expect(content).toContain('High Priority');
    expect(content).toContain('User is vegan');
    expect(content).toContain('User lives in Lisbon');
  });

  it('partitions facts into high-priority and recent', async () => {
    const now = new Date();
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        makeFact({ id: 'f1', text: 'Core preference', importance: 0.9, createdAt: new Date(now - 86400000) }),
        makeFact({ id: 'f2', text: 'Minor detail', importance: 0.3, createdAt: now }),
      ]),
    });

    const content = await readMemoryContext(mockClient);
    expect(content).toContain('High Priority');
    expect(content).toContain('Core preference');
    expect(content).toContain('Recent');
    expect(content).toContain('Minor detail');
  });

  it('includes total count footer', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        makeFact({ id: 'f1', text: 'Fact one' }),
        makeFact({ id: 'f2', text: 'Fact two' }),
        makeFact({ id: 'f3', text: 'Fact three' }),
      ]),
    });

    const content = await readMemoryContext(mockClient);
    expect(content).toContain('3 total memories stored');
  });

  it('handles client error gracefully', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockRejectedValue(new Error('Server unreachable')),
    });

    const content = await readMemoryContext(mockClient);
    expect(content).toContain('Error loading memories');
    expect(content).toContain('Server unreachable');
  });
});

describe('Cache behavior', () => {
  beforeEach(() => {
    invalidateMemoryContextCache();
  });

  it('caches the result and returns it on second call without re-fetching', async () => {
    const recallMock = jest.fn().mockResolvedValue([
      makeFact({ id: 'f1', text: 'Cached fact' }),
    ]);
    const mockClient = createMockClient({ recall: recallMock });

    const first = await readMemoryContext(mockClient);
    const second = await readMemoryContext(mockClient);

    expect(first).toBe(second);
    expect(recallMock).toHaveBeenCalledTimes(1); // Only called once due to cache
  });

  it('invalidateMemoryContextCache forces re-fetch', async () => {
    const recallMock = jest.fn().mockResolvedValue([
      makeFact({ id: 'f1', text: 'First version' }),
    ]);
    const mockClient = createMockClient({ recall: recallMock });

    await readMemoryContext(mockClient);
    expect(recallMock).toHaveBeenCalledTimes(1);

    // Invalidate cache
    invalidateMemoryContextCache();

    // Now change what recall returns
    recallMock.mockResolvedValue([
      makeFact({ id: 'f1', text: 'Updated version' }),
    ]);

    const second = await readMemoryContext(mockClient);
    expect(recallMock).toHaveBeenCalledTimes(2);
    expect(second).toContain('Updated version');
  });

  it('cache is invalidated when setOnRememberCallback fires', async () => {
    let cacheInvalidated = false;
    // Wire up the remember callback to invalidate the resource cache
    setOnRememberCallback(() => {
      invalidateMemoryContextCache();
      cacheInvalidated = true;
    });

    const recallMock = jest.fn().mockResolvedValue([
      makeFact({ id: 'f1', text: 'Initial fact' }),
    ]);
    const mockClient = createMockClient({ recall: recallMock });

    // Prime the cache
    await readMemoryContext(mockClient);
    expect(recallMock).toHaveBeenCalledTimes(1);

    // Simulate a remember operation (which triggers the callback).
    // Note: store-time dedup calls recall() once before storing to check for
    // near-duplicates, so handleRemember adds +1 recall call.
    const { handleRemember } = require('../dist/tools/remember.js');
    await handleRemember(mockClient, { fact: 'New fact' });

    expect(cacheInvalidated).toBe(true);

    // Next readMemoryContext should re-fetch (cache was invalidated by the callback)
    recallMock.mockResolvedValue([
      makeFact({ id: 'f1', text: 'Initial fact' }),
      makeFact({ id: 'f2', text: 'New fact' }),
    ]);

    await readMemoryContext(mockClient);
    // Total: 1 (prime) + 1 (dedup search in remember) + 1 (post-invalidation re-fetch) = 3
    expect(recallMock).toHaveBeenCalledTimes(3);

    // Clean up the callback
    setOnRememberCallback(() => {});
  });

  it('cache also applies for empty state', async () => {
    const recallMock = jest.fn().mockResolvedValue([]);
    const mockClient = createMockClient({ recall: recallMock });

    const first = await readMemoryContext(mockClient);
    const second = await readMemoryContext(mockClient);

    expect(first).toBe(second);
    expect(first).toContain('No memories stored yet');
    expect(recallMock).toHaveBeenCalledTimes(1);
  });
});

describe('Resource with no facts (empty states)', () => {
  beforeEach(() => {
    invalidateMemoryContextCache();
  });

  it('returns guidance message for empty vault', async () => {
    const mockClient = createMockClient();
    const content = await readMemoryContext(mockClient);
    expect(content).toContain('Memories will appear here');
  });

});
