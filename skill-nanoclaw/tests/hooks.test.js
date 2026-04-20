/**
 * @jest-environment node
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

describe('beforeAgentStart', () => {
  it('should retrieve and format memories', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'User prefers TypeScript',
            embedding: [],
            metadata: { importance: 0.8, tags: ['namespace:main', 'preference'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.85,
          textScore: 0.9,
          decayAdjustedScore: 0.8,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'What language should I use?',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].text).toBe('User prefers TypeScript');
    expect(result.contextString).toContain('## Relevant Memories');
  });

  it('should filter by namespace', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Work memory',
            embedding: [],
            metadata: { importance: 0.8, tags: ['namespace:work'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.8,
        },
        {
          fact: {
            id: 'fact-2',
            text: 'Main memory',
            embedding: [],
            metadata: { importance: 0.8, tags: ['namespace:main'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.85,
          vectorScore: 0.85,
          textScore: 0.85,
          decayAdjustedScore: 0.8,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].text).toBe('Main memory');
  });

  it('should handle errors gracefully', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockRejectedValue(new Error('Network error')),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    });

    expect(result.memories).toEqual([]);
    expect(result.latencyMs).toBeGreaterThanOrEqual(0);
  });
});

describe('agentEnd', () => {
  it('should skip extraction when turn count not at interval', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = { generate: jest.fn() };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'test' }],
      groupFolder: 'main',
      turnCount: 3,
    });

    expect(result.factsExtracted).toBe(0);
    expect(result.factsStored).toBe(0);
    expect(mockLLMClient.generate).not.toHaveBeenCalled();
  });

  it('should extract at interval', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'User likes TypeScript',
          type: 'preference',
          importance: 8,
          confidence: 0.9,
          action: 'ADD',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'I like TypeScript' },
        { role: 'assistant', content: 'Got it' },
        { role: 'user', content: 'Remember that' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(result.factsExtracted).toBe(1);
    expect(result.factsStored).toBe(1);
    expect(mockClient.remember).toHaveBeenCalled();
  });
});

describe('preCompact', () => {
  it('should perform comprehensive extraction', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User works on TotalReclaw',
            type: 'fact',
            importance: 7,
            confidence: 0.9,
            action: 'ADD',
            entities: [],
            relations: [],
          },
          {
            factText: 'Old fact',
            type: 'fact',
            importance: 5,
            confidence: 0.9,
            action: 'DELETE',
            existingFactId: 'old-fact-id',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Long conversation...',
      groupFolder: 'main',
    });

    expect(result.factsExtracted).toBe(2);
    expect(result.factsStored).toBe(1);
  });

  // NanoClaw 3.1.0: ADD-only alignment. The hoisted core prompt
  // (rust/totalreclaw-core/src/prompts/*.md) no longer emits
  // UPDATE/DELETE/NOOP — any stray tokens that slip through are
  // silently ignored. preCompact no longer forget-then-remember's on
  // UPDATE, and no longer forget's on DELETE.
  it('should silently ignore UPDATE action (ADD-only alignment)', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'Updated fact',
          type: 'fact',
          importance: 7,
          confidence: 0.9,
          action: 'UPDATE',
          existingFactId: 'old-fact-id',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    expect(mockClient.forget).not.toHaveBeenCalled();
    expect(mockClient.remember).not.toHaveBeenCalled();
    expect(result.factsStored).toBe(0);
  });
});

describe('Extraction Prompts', () => {
  describe('validateExtractionResponse', () => {
    it('should validate correct response', () => {
      const { validateExtractionResponse } = require('../dist/extraction/prompts.js');
      const response = {
        facts: [{
          factText: 'Test fact',
          type: 'preference',
          importance: 7,
          confidence: 0.9,
          action: 'ADD',
          entities: [],
          relations: [],
        }],
      };

      const result = validateExtractionResponse(response);

      expect(result.valid).toBe(true);
      expect(result.facts).toHaveLength(1);
      expect(result.facts[0].text).toBe('Test fact');
    });

    it('should reject invalid response', () => {
      const { validateExtractionResponse } = require('../dist/extraction/prompts.js');
      const response = {
        facts: [{
          factText: '',
          type: 'invalid',
          importance: 15,
          confidence: 2,
          action: 'INVALID',
        }],
      };

      const result = validateExtractionResponse(response);

      expect(result.valid).toBe(false);
      expect(result.errors.length).toBeGreaterThan(0);
    });

    it('should require facts array', () => {
      const { validateExtractionResponse } = require('../dist/extraction/prompts.js');
      const result = validateExtractionResponse({});

      expect(result.valid).toBe(false);
      expect(result.errors).toContain('Response must have a "facts" array');
    });
  });

  describe('formatConversationHistory', () => {
    it('should format turns correctly', () => {
      const { formatConversationHistory } = require('../dist/extraction/prompts.js');
      const turns = [
        { role: 'user', content: 'Hello', timestamp: new Date('2024-01-01T10:00:00Z') },
        { role: 'assistant', content: 'Hi there', timestamp: new Date('2024-01-01T10:00:05Z') },
      ];

      const result = formatConversationHistory(turns);

      expect(result).toContain('[1] USER');
      expect(result).toContain('[2] ASSISTANT');
      expect(result).toContain('Hello');
      expect(result).toContain('Hi there');
    });
  });
});

describe('beforeAgentStart extended', () => {
  it('should handle default namespace correctly', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Fact without explicit namespace',
            embedding: [],
            metadata: { importance: 0.8, tags: ['preference'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.8,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'default',
    });

    expect(result.memories).toHaveLength(1);
  });

  it('should respect maxMemories limit', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue(
        Array(15).fill(null).map((_, i) => ({
          fact: {
            id: `fact-${i}`,
            text: `Memory ${i}`,
            embedding: [],
            metadata: { tags: ['namespace:main'] },
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.5,
        }))
      ),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    }, 5);

    expect(mockClient.recall).toHaveBeenCalledWith('test', 5);
  });

  it('should include decay-adjusted info in context', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'High importance memory',
            embedding: [],
            metadata: { tags: ['namespace:main'], importance: 0.9 },
            decayScore: 0.95,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.85,
          textScore: 0.92,
          decayAdjustedScore: 0.88,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    });

    expect(result.memories[0].score).toBe(0.9);
    expect(result.contextString).toContain('High importance memory');
  });

  it('should return undefined contextString when no memories', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    });

    expect(result.memories).toEqual([]);
    expect(result.contextString).toBeUndefined();
  });
});

describe('agentEnd extended', () => {
  it('should skip when llmClient is null', async () => {
    const mockClient = createMockClient();

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, null, {
      conversationHistory: [{ role: 'user', content: 'test' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(result.factsExtracted).toBe(0);
    expect(result.factsStored).toBe(0);
  });

  it('should store facts with correct namespace tags', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'User prefers VSCode',
          type: 'preference',
          importance: 8,
          confidence: 0.9,
          action: 'ADD',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'I use VSCode' },
      ],
      groupFolder: 'work-project',
      turnCount: 5,
    });

    expect(mockClient.remember).toHaveBeenCalledWith(
      'User prefers VSCode',
      expect.objectContaining({
        tags: expect.arrayContaining(['namespace:work-project', 'preference']),
        importance: 0.8,
        source: 'agent_end_extraction',
      })
    );
  });

  it('should filter out low importance facts', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'Important fact',
            type: 'fact',
            importance: 8,
            confidence: 0.9,
            action: 'ADD',
            entities: [],
            relations: [],
          },
          {
            factText: 'Low importance note',
            type: 'fact',
            importance: 3,
            confidence: 0.9,
            action: 'ADD',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'test' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(result.factsExtracted).toBe(2);
    expect(result.factsStored).toBe(1);
  });

  it('should handle invalid JSON from LLM', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue('not valid json'),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'test' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(result.factsExtracted).toBe(0);
    expect(result.factsStored).toBe(0);
  });
});

describe('preCompact extended', () => {
  it('should silently ignore DELETE action (ADD-only alignment)', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'Outdated info',
          type: 'fact',
          importance: 5,
          confidence: 0.9,
          action: 'DELETE',
          existingFactId: 'old-fact-123',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    // NanoClaw 3.1.0: DELETE is silently ignored — the hoisted core
    // prompt no longer emits DELETE, and any stray DELETE tokens do NOT
    // trigger a forget.
    expect(mockClient.forget).not.toHaveBeenCalled();
    expect(result.factsStored).toBe(0);
  });

  it('should handle NOOP action', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'Already known',
          type: 'fact',
          importance: 5,
          confidence: 0.9,
          action: 'NOOP',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    expect(result.factsExtracted).toBe(1);
    expect(result.factsStored).toBe(0);
    expect(mockClient.remember).not.toHaveBeenCalled();
    expect(mockClient.forget).not.toHaveBeenCalled();
  });

  it('should skip when llmClient is null', async () => {
    const mockClient = createMockClient();

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, null, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    expect(result.factsExtracted).toBe(0);
    expect(result.factsStored).toBe(0);
    expect(result.claudeMdUpdated).toBe(false);
  });

  it('should handle invalid extraction response', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: '',
          type: 'invalid-type',
          importance: 15,
          confidence: 2,
          action: 'INVALID',
        }],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    expect(result.factsExtracted).toBe(0);
    expect(result.factsStored).toBe(0);
  });

  it('should use existing memories for context', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'existing-1',
            text: 'User likes Python',
            embedding: [],
            metadata: { tags: ['namespace:main'] },
            decayScore: 0.5,
            createdAt: new Date(),
          },
          score: 1,
          vectorScore: 1,
          textScore: 1,
          decayAdjustedScore: 0.5,
        },
        {
          fact: {
            id: 'existing-2',
            text: 'Other namespace fact',
            embedding: [],
            metadata: { tags: ['namespace:other'] },
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
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [],
      })),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    await preCompact(mockClient, mockLLMClient, {
      transcript: 'Conversation',
      groupFolder: 'main',
    });

    const callArgs = mockLLMClient.generate.mock.calls[0];
    expect(callArgs[1]).toContain('[ID: existing-1]');
    expect(callArgs[1]).not.toContain('Other namespace fact');
  });
});

describe('Cross-Namespace Isolation', () => {
  it('should not leak memories across namespaces in beforeAgentStart', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Personal secret',
            embedding: [],
            metadata: { tags: ['namespace:personal'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.95,
          vectorScore: 0.95,
          textScore: 0.95,
          decayAdjustedScore: 0.8,
        },
        {
          fact: {
            id: 'fact-2',
            text: 'Work info',
            embedding: [],
            metadata: { tags: ['namespace:work'] },
            decayScore: 0.8,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.8,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'secret',
      groupFolder: 'work',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].text).toBe('Work info');
  });

  it('should store to correct namespace in agentEnd', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [{
          factText: 'Namespace-specific fact',
          type: 'fact',
          importance: 7,
          confidence: 0.9,
          action: 'ADD',
          entities: [],
          relations: [],
        }],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'test' }],
      groupFolder: 'project-alpha',
      turnCount: 5,
    });

    expect(mockClient.remember).toHaveBeenCalledWith(
      'Namespace-specific fact',
      expect.objectContaining({
        tags: expect.arrayContaining(['namespace:project-alpha']),
      })
    );
  });
});

describe('Decay Score Handling', () => {
  it('should include decayScore in memory format', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'fact-1',
            text: 'Fresh memory',
            embedding: [],
            metadata: { tags: ['namespace:main'], importance: 0.8 },
            decayScore: 0.95,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.85,
        },
        {
          fact: {
            id: 'fact-2',
            text: 'Decayed memory',
            embedding: [],
            metadata: { tags: ['namespace:main'], importance: 0.8 },
            decayScore: 0.3,
            createdAt: new Date(Date.now() - 30 * 24 * 60 * 60 * 1000),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.27,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'test',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(2);
  });
});

/**
 * agentEnd dedup interaction tests.
 *
 * Design insight: agent-end is a LIGHTWEIGHT extraction pass that runs every
 * N turns (default 5). It intentionally only processes ADD actions with
 * importance >= MIN_IMPORTANCE (default 6). UPDATE, DELETE, and NOOP actions
 * are silently ignored.
 *
 * This is by design:
 * - agent-end fires frequently, so full CRUD would be expensive
 * - Near-duplicate protection is delegated to the MCP layer's store-time dedup
 *   (cosine similarity + content fingerprint), which fires when client.remember()
 *   is called
 * - Full CRUD (UPDATE/DELETE) is handled by pre-compaction, which runs once
 *   before context loss
 */
describe('agentEnd dedup interaction', () => {
  it('should ONLY store ADD actions — UPDATE is silently ignored', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User now prefers Rust over TypeScript',
            type: 'preference',
            importance: 9,
            confidence: 0.95,
            action: 'UPDATE',
            existingFactId: 'old-ts-fact',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'Actually I switched to Rust' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    // UPDATE fact is extracted but NOT stored — agent-end only stores ADDs
    expect(result.factsExtracted).toBe(1);
    expect(result.factsStored).toBe(0);
    expect(mockClient.remember).not.toHaveBeenCalled();
    expect(mockClient.forget).not.toHaveBeenCalled();
  });

  it('should ONLY store ADD actions — DELETE is silently ignored', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User no longer uses Python',
            type: 'fact',
            importance: 8,
            confidence: 0.9,
            action: 'DELETE',
            existingFactId: 'python-fact-id',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'I stopped using Python' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    // DELETE fact is extracted but NOT acted upon — no forget, no remember
    expect(result.factsExtracted).toBe(1);
    expect(result.factsStored).toBe(0);
    expect(mockClient.forget).not.toHaveBeenCalled();
    expect(mockClient.remember).not.toHaveBeenCalled();
  });

  it('should silently skip NOOP facts without any action', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User likes TypeScript',
            type: 'preference',
            importance: 8,
            confidence: 0.9,
            action: 'NOOP',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'I still like TypeScript' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    // NOOP passes through — counted as extracted but not stored
    expect(result.factsExtracted).toBe(1);
    expect(result.factsStored).toBe(0);
    expect(mockClient.remember).not.toHaveBeenCalled();
    expect(mockClient.forget).not.toHaveBeenCalled();
  });

  it('should store ADD and ignore UPDATE/DELETE/NOOP in a mixed batch', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User started learning Go',
            type: 'fact',
            importance: 8,
            confidence: 0.9,
            action: 'ADD',
            entities: [],
            relations: [],
          },
          {
            factText: 'User now prefers dark mode',
            type: 'preference',
            importance: 9,
            confidence: 0.95,
            action: 'UPDATE',
            existingFactId: 'light-mode-fact',
            entities: [],
            relations: [],
          },
          {
            factText: 'User stopped using Vim',
            type: 'fact',
            importance: 7,
            confidence: 0.85,
            action: 'DELETE',
            existingFactId: 'vim-fact',
            entities: [],
            relations: [],
          },
          {
            factText: 'User uses macOS',
            type: 'fact',
            importance: 7,
            confidence: 0.9,
            action: 'NOOP',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const result = await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'Lots of changes today' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    // All 4 extracted, but only the ADD is stored
    expect(result.factsExtracted).toBe(4);
    expect(result.factsStored).toBe(1);
    expect(mockClient.remember).toHaveBeenCalledTimes(1);
    expect(mockClient.remember).toHaveBeenCalledWith(
      'User started learning Go',
      expect.objectContaining({ source: 'agent_end_extraction' })
    );
    // No forget calls — UPDATE/DELETE are not processed
    expect(mockClient.forget).not.toHaveBeenCalled();
  });

  it('should delegate near-dup protection to MCP layer via client.remember()', async () => {
    // When agent-end calls client.remember(), the MCP server's store-time
    // dedup pipeline fires automatically (cosine search + content fingerprint).
    // This test verifies agent-end calls remember() directly without any
    // client-side dedup logic.
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(JSON.stringify({
        facts: [
          {
            factText: 'User prefers dark themes',
            type: 'preference',
            importance: 8,
            confidence: 0.9,
            action: 'ADD',
            entities: [],
            relations: [],
          },
        ],
      })),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [
        { role: 'user', content: 'I like dark themes' },
      ],
      groupFolder: 'main',
      turnCount: 5,
    });

    // agent-end calls remember() directly — no findNearDuplicate or dedup logic
    expect(mockClient.remember).toHaveBeenCalledTimes(1);
    expect(mockClient.remember).toHaveBeenCalledWith(
      'User prefers dark themes',
      expect.objectContaining({
        tags: expect.arrayContaining(['namespace:main', 'preference']),
        importance: 0.8,
        source: 'agent_end_extraction',
      })
    );
    // No dedup-related calls on the client side
    expect(mockClient.forget).not.toHaveBeenCalled();
  });
});

/**
 * preCompact vs agentEnd: complementary hooks, both ADD-only (NanoClaw 3.1.0).
 *
 * Key design:
 * - agent-end is LIGHTWEIGHT: runs every N turns, ADD-only, importance >= 6.
 * - pre-compaction is COMPREHENSIVE: runs once before context loss, also
 *   ADD-only (3.1.0 alignment) but with no importance floor.
 *
 * As of NanoClaw 3.1.0 (see
 * `rust/totalreclaw-core/src/prompts/extraction.md` +
 * `docs/notes/NANOCLAW-ACTION-FREQUENCY-20260419.md`):
 * - Both hooks silently ignore UPDATE / DELETE / NOOP actions that might
 *   leak through from cached LLM outputs — the hoisted core prompt no
 *   longer emits them, and the investigation found that pre-3.1
 *   UPDATE/DELETE code paths were never hit in production.
 * - Both can ADD. agent-end filters by importance (>=6), pre-compaction
 *   does not.
 * - Both rely on MCP store-time dedup for near-duplicate detection on ADDs.
 */
describe('preCompact vs agentEnd: complementary hooks (ADD-only)', () => {
  it('both hooks silently ignore UPDATE (ADD-only alignment)', async () => {
    const updateFacts = JSON.stringify({
      facts: [{
        factText: 'User now prefers Rust over TypeScript',
        type: 'preference',
        importance: 9,
        confidence: 0.95,
        action: 'UPDATE',
        existingFactId: 'old-ts-fact',
        entities: [],
        relations: [],
      }],
    });

    // Test agentEnd — should silently ignore UPDATE.
    const agentEndClient = createMockClient();
    const agentEndLLM = { generate: jest.fn().mockResolvedValue(updateFacts) };
    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const agentEndResult = await agentEnd(agentEndClient, agentEndLLM, {
      conversationHistory: [{ role: 'user', content: 'I switched to Rust' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(agentEndResult.factsStored).toBe(0);
    expect(agentEndClient.forget).not.toHaveBeenCalled();
    expect(agentEndClient.remember).not.toHaveBeenCalled();

    // Test preCompact — 3.1.0: also silently ignores UPDATE.
    const preCompactClient = createMockClient();
    const preCompactLLM = { generate: jest.fn().mockResolvedValue(updateFacts) };
    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const preCompactResult = await preCompact(preCompactClient, preCompactLLM, {
      transcript: 'User said they switched to Rust',
      groupFolder: 'main',
    });

    expect(preCompactResult.factsStored).toBe(0);
    expect(preCompactClient.forget).not.toHaveBeenCalled();
    expect(preCompactClient.remember).not.toHaveBeenCalled();
  });

  it('both hooks silently ignore DELETE (ADD-only alignment)', async () => {
    const deleteFacts = JSON.stringify({
      facts: [{
        factText: 'User no longer uses Python',
        type: 'fact',
        importance: 8,
        confidence: 0.9,
        action: 'DELETE',
        existingFactId: 'python-fact-id',
        entities: [],
        relations: [],
      }],
    });

    // Test agentEnd — should silently ignore DELETE.
    const agentEndClient = createMockClient();
    const agentEndLLM = { generate: jest.fn().mockResolvedValue(deleteFacts) };
    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const agentEndResult = await agentEnd(agentEndClient, agentEndLLM, {
      conversationHistory: [{ role: 'user', content: 'I stopped using Python' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(agentEndResult.factsStored).toBe(0);
    expect(agentEndClient.forget).not.toHaveBeenCalled();

    // Test preCompact — 3.1.0: also silently ignores DELETE.
    const preCompactClient = createMockClient();
    const preCompactLLM = { generate: jest.fn().mockResolvedValue(deleteFacts) };
    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const preCompactResult = await preCompact(preCompactClient, preCompactLLM, {
      transcript: 'User said they stopped using Python',
      groupFolder: 'main',
    });

    expect(preCompactResult.factsStored).toBe(0);
    expect(preCompactClient.forget).not.toHaveBeenCalled();
    expect(preCompactClient.remember).not.toHaveBeenCalled();
  });

  it('both hooks store ADDs but agentEnd filters by importance', async () => {
    const lowImportanceAdd = JSON.stringify({
      facts: [{
        factText: 'User mentioned the weather is nice',
        type: 'fact',
        importance: 4,
        confidence: 0.7,
        action: 'ADD',
        entities: [],
        relations: [],
      }],
    });

    // agentEnd — should NOT store (importance 4 < MIN_IMPORTANCE 6)
    const agentEndClient = createMockClient();
    const agentEndLLM = { generate: jest.fn().mockResolvedValue(lowImportanceAdd) };
    const { agentEnd } = require('../dist/hooks/agent-end.js');
    const agentEndResult = await agentEnd(agentEndClient, agentEndLLM, {
      conversationHistory: [{ role: 'user', content: 'Nice weather today' }],
      groupFolder: 'main',
      turnCount: 5,
    });

    expect(agentEndResult.factsExtracted).toBe(1);
    expect(agentEndResult.factsStored).toBe(0);
    expect(agentEndClient.remember).not.toHaveBeenCalled();

    // preCompact — SHOULD store (no importance filter for ADDs)
    const preCompactClient = createMockClient();
    const preCompactLLM = { generate: jest.fn().mockResolvedValue(lowImportanceAdd) };
    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const preCompactResult = await preCompact(preCompactClient, preCompactLLM, {
      transcript: 'User mentioned the weather',
      groupFolder: 'main',
    });

    expect(preCompactResult.factsExtracted).toBe(1);
    expect(preCompactResult.factsStored).toBe(1);
    expect(preCompactClient.remember).toHaveBeenCalled();
  });

  it('preCompact stores only ADDs in a mixed-action batch (ADD-only alignment)', async () => {
    // NanoClaw 3.1.0: the hoisted core prompt only emits ADD. A real
    // LLM following the current prompt wouldn't produce this mixed batch,
    // but cached outputs or custom drivers might — this test verifies
    // the hook quietly drops the non-ADD actions without erroring.
    const fullCrudFacts = JSON.stringify({
      facts: [
        {
          factText: 'User started learning Go',
          type: 'fact',
          importance: 7,
          confidence: 0.9,
          action: 'ADD',
          entities: [],
          relations: [],
        },
        {
          factText: 'User now prefers dark mode',
          type: 'preference',
          importance: 8,
          confidence: 0.95,
          action: 'UPDATE',
          existingFactId: 'light-mode-fact',
          entities: [],
          relations: [],
        },
        {
          factText: 'User stopped using Vim',
          type: 'fact',
          importance: 6,
          confidence: 0.85,
          action: 'DELETE',
          existingFactId: 'vim-fact',
          entities: [],
          relations: [],
        },
        {
          factText: 'User uses macOS',
          type: 'fact',
          importance: 7,
          confidence: 0.9,
          action: 'NOOP',
          entities: [],
          relations: [],
        },
      ],
    });

    const mockClient = createMockClient();
    const mockLLMClient = { generate: jest.fn().mockResolvedValue(fullCrudFacts) };
    const { preCompact } = require('../dist/hooks/pre-compact.js');
    const result = await preCompact(mockClient, mockLLMClient, {
      transcript: 'Long conversation with many changes',
      groupFolder: 'main',
    });

    // All 4 extracted by the validator.
    expect(result.factsExtracted).toBe(4);
    // Only the ADD stores — UPDATE/DELETE/NOOP are silently skipped.
    expect(result.factsStored).toBe(1);

    // ADD: remember called for the new fact.
    expect(mockClient.remember).toHaveBeenCalledWith(
      'User started learning Go',
      expect.objectContaining({ source: 'pre_compaction' })
    );

    // UPDATE/DELETE: no forget-then-remember, no forget-only.
    expect(mockClient.forget).not.toHaveBeenCalled();

    // Total: 1 remember call (the ADD), 0 forget calls.
    expect(mockClient.remember).toHaveBeenCalledTimes(1);
    expect(mockClient.forget).toHaveBeenCalledTimes(0);
  });
});
