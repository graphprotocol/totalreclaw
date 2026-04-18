/**
 * @jest-environment node
 *
 * Memory Taxonomy v1 default-path tests for NanoClaw 3.0.0.
 *
 * Verifies the extraction prompts module emits v1 types, accepts both v1
 * merged-topic shape and legacy bare-facts shape, and that the hooks store
 * facts with the correct v1 provenance tags (`source:X`, `scope:Y`).
 *
 * These tests exercise the same surface the NanoClaw agent-runner would
 * reach at runtime, plus the MCP delegation path (agent talks to
 * `@totalreclaw/mcp-server` via stdio — tool discovery is exercised
 * separately in `mcp-tool-discovery.test.js`).
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

const prompts = require('../dist/extraction/prompts.js');

describe('v1 taxonomy — extraction/prompts.ts', () => {
  describe('VALID_MEMORY_TYPES', () => {
    it('should expose the 6 canonical v1 types', () => {
      expect(prompts.VALID_MEMORY_TYPES).toEqual([
        'claim',
        'preference',
        'directive',
        'commitment',
        'episode',
        'summary',
      ]);
    });

    it('should expose a deprecated VALID_MEMORY_TYPES_V1 alias', () => {
      expect(prompts.VALID_MEMORY_TYPES_V1).toEqual(prompts.VALID_MEMORY_TYPES);
    });

    it('should still list the 8 legacy v0 types', () => {
      expect(prompts.LEGACY_V0_MEMORY_TYPES).toEqual([
        'fact',
        'preference',
        'decision',
        'episodic',
        'goal',
        'context',
        'summary',
        'rule',
      ]);
    });
  });

  describe('isValidMemoryType', () => {
    it('should accept v1 tokens', () => {
      for (const t of prompts.VALID_MEMORY_TYPES) {
        expect(prompts.isValidMemoryType(t)).toBe(true);
      }
    });

    it('should reject legacy v0-only tokens (must go through normalizeToV1Type)', () => {
      // 'preference' and 'summary' appear in BOTH v0 and v1 so isValidMemoryType
      // returns true for them. Everything else v0-only returns false.
      expect(prompts.isValidMemoryType('fact')).toBe(false);
      expect(prompts.isValidMemoryType('decision')).toBe(false);
      expect(prompts.isValidMemoryType('rule')).toBe(false);
      expect(prompts.isValidMemoryType('context')).toBe(false);
      expect(prompts.isValidMemoryType('episodic')).toBe(false);
      expect(prompts.isValidMemoryType('goal')).toBe(false);
      // Overlap tokens:
      expect(prompts.isValidMemoryType('preference')).toBe(true);
      expect(prompts.isValidMemoryType('summary')).toBe(true);
    });

    it('should reject garbage tokens', () => {
      expect(prompts.isValidMemoryType('whatever')).toBe(false);
      expect(prompts.isValidMemoryType(null)).toBe(false);
      expect(prompts.isValidMemoryType(undefined)).toBe(false);
      expect(prompts.isValidMemoryType(42)).toBe(false);
    });
  });

  describe('V0_TO_V1_TYPE mapping', () => {
    it('should mirror the plugin / Python / core mapping', () => {
      expect(prompts.V0_TO_V1_TYPE).toEqual({
        fact: 'claim',
        preference: 'preference',
        decision: 'claim',
        episodic: 'episode',
        goal: 'commitment',
        context: 'claim',
        summary: 'summary',
        rule: 'directive',
      });
    });
  });

  describe('normalizeToV1Type', () => {
    it('should pass through valid v1 tokens', () => {
      expect(prompts.normalizeToV1Type('claim')).toBe('claim');
      expect(prompts.normalizeToV1Type('directive')).toBe('directive');
      expect(prompts.normalizeToV1Type('commitment')).toBe('commitment');
      expect(prompts.normalizeToV1Type('episode')).toBe('episode');
    });

    it('should coerce v0 tokens via V0_TO_V1_TYPE', () => {
      expect(prompts.normalizeToV1Type('fact')).toBe('claim');
      expect(prompts.normalizeToV1Type('decision')).toBe('claim');
      expect(prompts.normalizeToV1Type('rule')).toBe('directive');
      expect(prompts.normalizeToV1Type('goal')).toBe('commitment');
      expect(prompts.normalizeToV1Type('episodic')).toBe('episode');
      expect(prompts.normalizeToV1Type('context')).toBe('claim');
    });

    it('should default unknown tokens to "claim"', () => {
      expect(prompts.normalizeToV1Type('unknown')).toBe('claim');
      expect(prompts.normalizeToV1Type(null)).toBe('claim');
      expect(prompts.normalizeToV1Type(undefined)).toBe('claim');
      expect(prompts.normalizeToV1Type(42)).toBe('claim');
    });

    it('should be case-insensitive', () => {
      expect(prompts.normalizeToV1Type('CLAIM')).toBe('claim');
      expect(prompts.normalizeToV1Type('Rule')).toBe('directive');
    });
  });

  describe('VALID_MEMORY_SOURCES / SCOPES / VOLATILITIES', () => {
    it('should expose the 5 v1 sources', () => {
      expect(prompts.VALID_MEMORY_SOURCES).toEqual([
        'user',
        'user-inferred',
        'assistant',
        'external',
        'derived',
      ]);
    });

    it('should expose the 8 v1 scopes', () => {
      expect(prompts.VALID_MEMORY_SCOPES).toEqual([
        'work',
        'personal',
        'health',
        'family',
        'creative',
        'finance',
        'misc',
        'unspecified',
      ]);
    });

    it('should expose the 3 v1 volatilities', () => {
      expect(prompts.VALID_MEMORY_VOLATILITIES).toEqual([
        'stable',
        'updatable',
        'ephemeral',
      ]);
    });
  });
});

describe('validateExtractionResponse — v1 merged shape', () => {
  const { validateExtractionResponse } = prompts;

  it('accepts v1 merged-topic shape { topics, facts }', () => {
    const response = {
      topics: ['web dev', 'coffee'],
      facts: [
        {
          text: 'User prefers TypeScript over JavaScript',
          type: 'preference',
          source: 'user',
          scope: 'work',
          importance: 8,
          action: 'ADD',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.topics).toEqual(['web dev', 'coffee']);
    expect(res.facts).toHaveLength(1);
    expect(res.facts[0].type).toBe('preference');
    expect(res.facts[0].source).toBe('user');
    expect(res.facts[0].scope).toBe('work');
  });

  it('accepts bare legacy shape { facts } (no topics) and returns topics=[]', () => {
    const response = {
      facts: [
        {
          text: 'fact A',
          type: 'claim',
          importance: 7,
          action: 'ADD',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.topics).toEqual([]);
    expect(res.facts).toHaveLength(1);
  });

  it('accepts bare array shape [ {...} ]', () => {
    const response = [
      { text: 'raw array fact', type: 'claim', importance: 7, action: 'ADD' },
    ];
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.topics).toEqual([]);
    expect(res.facts).toHaveLength(1);
    expect(res.facts[0].text).toBe('raw array fact');
  });

  it('coerces legacy v0 types to v1', () => {
    const response = {
      facts: [
        { text: 'a', type: 'fact', importance: 7, action: 'ADD' },
        { text: 'b', type: 'rule', importance: 8, action: 'ADD' },
        { text: 'c', type: 'decision', importance: 9, action: 'ADD' },
        { text: 'd', type: 'goal', importance: 7, action: 'ADD' },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts.map((f) => f.type)).toEqual([
      'claim',
      'directive',
      'claim',
      'commitment',
    ]);
  });

  it('defaults missing source to "user-inferred" and missing scope to "unspecified"', () => {
    const response = {
      facts: [{ text: 'a', type: 'claim', importance: 7, action: 'ADD' }],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].source).toBe('user-inferred');
    expect(res.facts[0].scope).toBe('unspecified');
  });

  it('drops illegal type:summary + source:user combinations', () => {
    const response = {
      facts: [
        {
          text: 'a self-proclaimed summary',
          type: 'summary',
          source: 'user',
          importance: 8,
          action: 'ADD',
        },
        {
          text: 'a real claim',
          type: 'claim',
          source: 'user',
          importance: 8,
          action: 'ADD',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts).toHaveLength(1);
    expect(res.facts[0].type).toBe('claim');
  });

  it('preserves reasoning for claim-type facts', () => {
    const response = {
      facts: [
        {
          text: 'Chose PostgreSQL over MySQL',
          type: 'claim',
          source: 'user',
          scope: 'work',
          importance: 9,
          action: 'ADD',
          reasoning: 'because data is relational and needs ACID',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].reasoning).toBe(
      'because data is relational and needs ACID',
    );
  });

  it('truncates reasoning at 256 chars', () => {
    const long = 'a'.repeat(500);
    const response = {
      facts: [
        {
          text: 'x',
          type: 'claim',
          importance: 7,
          action: 'ADD',
          reasoning: long,
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].reasoning).toHaveLength(256);
  });

  it('rejects invalid shapes', () => {
    expect(validateExtractionResponse(null).valid).toBe(false);
    expect(validateExtractionResponse('string').valid).toBe(false);
    expect(validateExtractionResponse({}).valid).toBe(false);
    expect(validateExtractionResponse({ facts: 'not-array' }).valid).toBe(false);
  });

  it('still accepts factText as a fallback for text (robustness)', () => {
    const response = {
      facts: [
        { factText: 'legacy alias', type: 'claim', importance: 7, action: 'ADD' },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].text).toBe('legacy alias');
  });

  it('falls back to unspecified when scope is not in the v1 list', () => {
    const response = {
      facts: [
        {
          text: 'x',
          type: 'claim',
          scope: 'invalid-scope',
          importance: 7,
          action: 'ADD',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].scope).toBe('unspecified');
  });

  it('falls back to user-inferred when source is not in the v1 list', () => {
    const response = {
      facts: [
        {
          text: 'x',
          type: 'claim',
          source: 'invalid-source',
          importance: 7,
          action: 'ADD',
        },
      ],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].source).toBe('user-inferred');
  });

  it('clamps importance to 1-10', () => {
    const response = {
      facts: [
        { text: 'low', type: 'claim', importance: -5, action: 'ADD' },
      ],
    };
    const res = validateExtractionResponse(response);
    // -5 fails validation (must be 1-10)
    expect(res.valid).toBe(false);
  });

  it('truncates text at 512 chars', () => {
    const long = 'x'.repeat(1000);
    const response = {
      facts: [{ text: long, type: 'claim', importance: 7, action: 'ADD' }],
    };
    const res = validateExtractionResponse(response);
    expect(res.valid).toBe(true);
    expect(res.facts[0].text).toHaveLength(512);
  });
});

describe('hooks — v1 metadata tags', () => {
  function createMockClient(overrides = {}) {
    return {
      recall: jest.fn().mockResolvedValue([]),
      remember: jest.fn().mockResolvedValue('fact-id'),
      forget: jest.fn().mockResolvedValue(undefined),
      isReady: jest.fn().mockReturnValue(true),
      ...overrides,
    };
  }

  it('agentEnd tags stored facts with v1 type + source:X + scope:Y', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(
        JSON.stringify({
          topics: ['tools'],
          facts: [
            {
              text: 'User prefers VSCode for Python work',
              type: 'preference',
              source: 'user',
              scope: 'work',
              importance: 8,
              action: 'ADD',
            },
          ],
        }),
      ),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'I use VSCode' }],
      groupFolder: 'main',
      // default extraction_interval is 3; 3 % 3 == 0 triggers extraction.
      turnCount: 3,
    });

    expect(mockClient.remember).toHaveBeenCalledWith(
      'User prefers VSCode for Python work',
      expect.objectContaining({
        tags: expect.arrayContaining([
          'namespace:main',
          'preference',
          'source:user',
          'scope:work',
        ]),
        importance: 0.8,
        source: 'agent_end_extraction',
      }),
    );
  });

  it('agentEnd defaults missing source to user-inferred (write-path safety net)', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(
        JSON.stringify({
          facts: [
            {
              text: 'User lives in Lisbon',
              type: 'claim',
              importance: 8,
              action: 'ADD',
              // source missing on purpose
            },
          ],
        }),
      ),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'I live in Lisbon' }],
      groupFolder: 'main',
      turnCount: 3,
    });

    expect(mockClient.remember).toHaveBeenCalledWith(
      'User lives in Lisbon',
      expect.objectContaining({
        tags: expect.arrayContaining([
          'namespace:main',
          'claim',
          'source:user-inferred',
        ]),
      }),
    );
  });

  it('agentEnd does NOT emit scope tag when scope is unspecified', async () => {
    const mockClient = createMockClient();
    const mockLLMClient = {
      generate: jest.fn().mockResolvedValue(
        JSON.stringify({
          facts: [
            {
              text: 'x',
              type: 'claim',
              source: 'user',
              // scope missing → defaults to 'unspecified'
              importance: 8,
              action: 'ADD',
            },
          ],
        }),
      ),
    };

    const { agentEnd } = require('../dist/hooks/agent-end.js');
    await agentEnd(mockClient, mockLLMClient, {
      conversationHistory: [{ role: 'user', content: 'x' }],
      groupFolder: 'main',
      turnCount: 3,
    });

    const call = mockClient.remember.mock.calls[0];
    const tags = call[1].tags;
    expect(tags).toContain('namespace:main');
    expect(tags).toContain('claim');
    expect(tags).toContain('source:user');
    // No scope tag when it's unspecified
    expect(tags.some((t) => t.startsWith('scope:'))).toBe(false);
  });

  it('preCompact uses v1 type coercion for debrief items', async () => {
    // Debrief emits legacy "context" and "summary" literally; NanoClaw coerces
    // them to v1 types before tagging.
    const mockClient = createMockClient();
    let callCount = 0;
    const mockLLMClient = {
      generate: jest.fn().mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          // Main extraction
          return Promise.resolve(
            JSON.stringify({
              facts: [
                {
                  text: 'User wrapped up TotalReclaw v3.0 release',
                  type: 'episode',
                  source: 'user',
                  importance: 8,
                  action: 'ADD',
                },
              ],
            }),
          );
        }
        // Debrief
        return Promise.resolve(
          JSON.stringify([
            {
              text: 'Session focused on v1 taxonomy cross-client wiring',
              type: 'summary',
              importance: 8,
            },
            {
              text: 'Still open: benchmark validation for v1 vs v0 quality',
              type: 'context',
              importance: 7,
            },
          ]),
        );
      }),
    };

    const { preCompact } = require('../dist/hooks/pre-compact.js');
    await preCompact(mockClient, mockLLMClient, {
      transcript: 'User and assistant discussed the v3.0 release',
      groupFolder: 'main',
    });

    // All calls made — primary ADD + 2 debrief items
    expect(mockClient.remember).toHaveBeenCalledTimes(3);

    // Find the debrief calls by source
    const debriefCalls = mockClient.remember.mock.calls.filter(
      (c) => c[1].source === 'nanoclaw_debrief',
    );
    expect(debriefCalls).toHaveLength(2);

    const summaryCall = debriefCalls.find((c) => c[1].tags.includes('summary'));
    const contextCall = debriefCalls.find((c) => c[1].tags.includes('claim'));
    expect(summaryCall).toBeDefined();
    expect(contextCall).toBeDefined();

    // The debrief context item should be coerced to v1 "claim" via V0_TO_V1_TYPE
    expect(contextCall[1].tags).toContain('claim');
    expect(contextCall[1].tags).toContain('source:derived');

    // The summary item passes through as "summary"
    expect(summaryCall[1].tags).toContain('summary');
    expect(summaryCall[1].tags).toContain('source:derived');
  });
});

describe('beforeAgentStart — v1 + v0 type tag detection', () => {
  function createMockClient(overrides = {}) {
    return {
      recall: jest.fn().mockResolvedValue([]),
      remember: jest.fn().mockResolvedValue('fact-id'),
      forget: jest.fn().mockResolvedValue(undefined),
      isReady: jest.fn().mockReturnValue(true),
      ...overrides,
    };
  }

  it('reads v1 type tag "directive" from stored fact', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'f1',
            text: 'Always tag PRs with JIRA ticket',
            embedding: [],
            metadata: {
              tags: ['namespace:main', 'directive', 'source:user', 'scope:work'],
            },
            decayScore: 0.9,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.9,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'pr policy',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].type).toBe('directive');
  });

  it('reads legacy v0 type tag "rule" from pre-v3 stored fact', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'f1',
            text: 'Always check systemd unit file',
            embedding: [],
            metadata: { tags: ['namespace:main', 'rule'] },
            decayScore: 0.9,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.9,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'x',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].type).toBe('rule'); // raw v0 tag preserved on read
  });

  it('defaults to "claim" when no type tag is present', async () => {
    const mockClient = createMockClient({
      recall: jest.fn().mockResolvedValue([
        {
          fact: {
            id: 'f1',
            text: 'untagged fact',
            embedding: [],
            metadata: { tags: ['namespace:main'] },
            decayScore: 0.9,
            createdAt: new Date(),
          },
          score: 0.9,
          vectorScore: 0.9,
          textScore: 0.9,
          decayAdjustedScore: 0.9,
        },
      ]),
    });

    const { beforeAgentStart } = require('../dist/hooks/before-agent-start.js');
    const result = await beforeAgentStart(mockClient, {
      userMessage: 'x',
      groupFolder: 'main',
    });

    expect(result.memories).toHaveLength(1);
    expect(result.memories[0].type).toBe('claim');
  });
});

// ---------------------------------------------------------------------------
// Extraction prompt content
// ---------------------------------------------------------------------------

describe('Extraction prompts — v1 content', () => {
  it('system prompt mentions v1 type list', () => {
    const { POST_TURN_PROMPT } = prompts;
    const system = POST_TURN_PROMPT.system;
    expect(system).toContain('Memory Taxonomy v1');
    expect(system).toContain('claim');
    expect(system).toContain('directive');
    expect(system).toContain('commitment');
    expect(system).toContain('episode');
    expect(system).toContain('summary');
    expect(system).toContain('preference');
  });

  it('system prompt mentions the 5 v1 sources', () => {
    const { POST_TURN_PROMPT } = prompts;
    const system = POST_TURN_PROMPT.system;
    expect(system).toContain('user-inferred');
    expect(system).toContain('assistant');
    expect(system).toContain('external');
    expect(system).toContain('derived');
  });

  it('system prompt mentions the v1 scope domains', () => {
    const { POST_TURN_PROMPT } = prompts;
    const system = POST_TURN_PROMPT.system;
    expect(system).toContain('work');
    expect(system).toContain('personal');
    expect(system).toContain('health');
    expect(system).toContain('finance');
    expect(system).toContain('unspecified');
  });

  it('pre-compaction prompt uses the same v1 system content', () => {
    expect(prompts.PRE_COMPACTION_PROMPT.system).toBe(prompts.POST_TURN_PROMPT.system);
  });

  it('explicit-command prompt uses the same v1 system content', () => {
    expect(prompts.EXPLICIT_COMMAND_PROMPT.system).toBe(prompts.POST_TURN_PROMPT.system);
  });

  it('format() produces a prompt containing the conversation and existing memories', () => {
    const { POST_TURN_PROMPT } = prompts;
    const result = POST_TURN_PROMPT.format({
      conversationHistory: 'turn 1\nturn 2',
      existingMemories: '[ID: abc] prior fact',
    });
    expect(result.user).toContain('turn 1');
    expect(result.user).toContain('[ID: abc] prior fact');
    expect(result.user).toContain('dedup');
  });

  it('debrief prompt lists v1 + v0 debrief types', () => {
    // Debrief prompt uses summary/context tokens per the canonical cross-client spec.
    // "context" coerces to v1 "claim" at write time via V0_TO_V1_TYPE.
    const debrief = prompts.DEBRIEF_SYSTEM_PROMPT;
    expect(debrief).toContain('summary');
    expect(debrief).toContain('context');
  });
});
