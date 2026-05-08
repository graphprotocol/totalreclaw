/**
 * Tests for `src/extraction/extractor.ts` — the canonical v1 fact parser
 * + lexical-bump heuristics. These cover pure-function paths only (no
 * LLM calls, no network) so they run fast and deterministically. The
 * end-to-end LLM path is exercised on the VPS during RC QA.
 */

import {
  parseFactsResponse,
  parseMergedResponseV1,
  parseDebriefResponse,
  computeLexicalImportanceBump,
  applyProvenanceFilterLax,
  defaultVolatility,
  isValidMemoryType,
  VALID_MEMORY_TYPES,
  type ExtractedFact,
} from '../src/extraction/extractor.js';

describe('extraction/extractor — pure parsers', () => {
  describe('parseMergedResponseV1', () => {
    it('parses canonical {topics, facts} JSON', () => {
      const llmOutput = JSON.stringify({
        topics: ['user identity', 'tool preferences'],
        facts: [
          { text: 'User lives in Porto', type: 'claim', source: 'user', importance: 9, confidence: 0.95, action: 'ADD' },
          { text: 'User prefers PostgreSQL over MySQL', type: 'preference', source: 'user', importance: 8, action: 'ADD' },
        ],
      });
      const { topics, facts } = parseMergedResponseV1(llmOutput);
      expect(topics).toEqual(['user identity', 'tool preferences']);
      expect(facts).toHaveLength(2);
      expect(facts[0].text).toBe('User lives in Porto');
      expect(facts[0].type).toBe('claim');
      expect(facts[1].type).toBe('preference');
    });

    it('strips markdown code fences', () => {
      const wrapped = '```json\n' + JSON.stringify({
        topics: [],
        facts: [{ text: 'Pedro works at The Graph Foundation', type: 'claim', source: 'user', importance: 8, action: 'ADD' }],
      }) + '\n```';
      const { facts } = parseMergedResponseV1(wrapped);
      expect(facts).toHaveLength(1);
    });

    it('strips <think>...</think> reasoning blocks', () => {
      const withThink = '<think>let me consider...</think>\n' + JSON.stringify({
        topics: [],
        facts: [{ text: 'User likes morning runs', type: 'preference', source: 'user', importance: 7, action: 'ADD' }],
      });
      const { facts } = parseMergedResponseV1(withThink);
      expect(facts).toHaveLength(1);
    });

    it('drops facts below importance 6 (unless DELETE)', () => {
      const out = JSON.stringify({
        topics: [],
        facts: [
          { text: 'User said hello', type: 'episode', source: 'user', importance: 3, action: 'ADD' },
          { text: 'User favourite color is cobalt blue', type: 'preference', source: 'user', importance: 8, action: 'ADD' },
          { text: 'Old fact', type: 'claim', source: 'user', importance: 2, action: 'DELETE' },
        ],
      });
      const { facts } = parseMergedResponseV1(out);
      expect(facts).toHaveLength(2);
      const texts = facts.map((f) => f.text);
      expect(texts).toContain('User favourite color is cobalt blue');
      expect(texts.find((t) => t.startsWith('Old'))).toBeDefined();
      expect(texts).not.toContain('User said hello');
    });

    it('rejects illegal type:summary + source:user combination', () => {
      const out = JSON.stringify({
        topics: [],
        facts: [
          { text: 'Session synthesis', type: 'summary', source: 'user', importance: 8, action: 'ADD' },
          { text: 'Session synthesis (legal)', type: 'summary', source: 'derived', importance: 8, action: 'ADD' },
        ],
      });
      const { facts } = parseMergedResponseV1(out);
      expect(facts).toHaveLength(1);
      expect(facts[0].source).toBe('derived');
    });

    it('coerces unknown types to claim', () => {
      const out = JSON.stringify({
        topics: [],
        facts: [{ text: 'Some fact text here', type: 'made_up_type', source: 'user', importance: 7, action: 'ADD' }],
      });
      const { facts } = parseMergedResponseV1(out);
      expect(facts[0].type).toBe('claim');
    });

    it('parseFactsResponse is the .facts shortcut', () => {
      const out = JSON.stringify({
        topics: ['x'],
        facts: [{ text: 'User has a dog named Beans', type: 'claim', source: 'user', importance: 7, action: 'ADD' }],
      });
      const facts = parseFactsResponse(out);
      expect(facts).toHaveLength(1);
    });

    it('returns empty on unparseable input', () => {
      expect(parseFactsResponse('totally not JSON')).toEqual([]);
    });
  });

  describe('parseDebriefResponse', () => {
    it('parses a debrief array and clamps importance', () => {
      const out = JSON.stringify([
        { text: 'Session debrief about postgres migration', type: 'summary', importance: 8 },
        { text: 'Open thread: investigate read replica lag', type: 'context', importance: 11 }, // clamps to 10
        { text: 'shrt', type: 'summary', importance: 7 }, // < 5 chars text -> dropped
      ]);
      const items = parseDebriefResponse(out);
      expect(items).toHaveLength(2);
      expect(items[1].importance).toBeLessThanOrEqual(10);
    });
  });

  describe('computeLexicalImportanceBump', () => {
    it('bumps for "remember this" intent phrase', () => {
      const bump = computeLexicalImportanceBump(
        'User name is Pedro',
        '[user]: remember this — my name is Pedro',
      );
      expect(bump).toBeGreaterThanOrEqual(1);
    });

    it('bumps for double-exclamation emphasis', () => {
      const bump = computeLexicalImportanceBump(
        'User dislikes flaky tests',
        'flaky tests are the worst!!',
      );
      expect(bump).toBeGreaterThanOrEqual(1);
    });

    it('bumps for repetition of content words', () => {
      const bump = computeLexicalImportanceBump(
        'User prefers PostgreSQL',
        'I prefer PostgreSQL. Yeah, PostgreSQL is the right choice.',
      );
      expect(bump).toBeGreaterThanOrEqual(1);
    });

    it('caps total bump at 2', () => {
      const bump = computeLexicalImportanceBump(
        'User prefers PostgreSQL',
        'remember this!! PostgreSQL is critical, PostgreSQL above all.',
      );
      expect(bump).toBeLessThanOrEqual(2);
    });

    it('no bump on bland conversation', () => {
      const bump = computeLexicalImportanceBump(
        'User has a meeting at 3pm',
        'sure thing',
      );
      expect(bump).toBe(0);
    });
  });

  describe('applyProvenanceFilterLax', () => {
    it('caps assistant-sourced facts at importance 7', () => {
      const facts: ExtractedFact[] = [
        { text: 'AI hallucinated this', type: 'claim', source: 'assistant', importance: 9, action: 'ADD' },
      ];
      const filtered = applyProvenanceFilterLax(facts, '[assistant]: ai output');
      expect(filtered[0].importance).toBe(7);
      expect(filtered[0].source).toBe('assistant');
    });

    it('drops facts whose words do not appear in user turns AND retags as assistant', () => {
      const facts: ExtractedFact[] = [
        { text: 'irrelevant nonsense facts about widgets', type: 'claim', source: 'user-inferred', importance: 8, action: 'ADD' },
      ];
      const filtered = applyProvenanceFilterLax(
        facts,
        '[user]: hello there\n[assistant]: hi back',
      );
      expect(filtered[0].source).toBe('assistant');
      expect(filtered[0].importance).toBe(7);
    });

    it('keeps user-sourced facts at full importance', () => {
      const facts: ExtractedFact[] = [
        { text: 'User name is Pedro', type: 'claim', source: 'user', importance: 10, action: 'ADD' },
      ];
      const filtered = applyProvenanceFilterLax(facts, '[user]: my name is Pedro');
      expect(filtered[0].importance).toBe(10);
    });
  });

  describe('defaultVolatility', () => {
    it('commitment → updatable', () => {
      expect(defaultVolatility({ text: 't', type: 'commitment', source: 'user', importance: 8, action: 'ADD' })).toBe('updatable');
    });
    it('episode → stable', () => {
      expect(defaultVolatility({ text: 't', type: 'episode', source: 'user', importance: 8, action: 'ADD' })).toBe('stable');
    });
    it('directive → stable', () => {
      expect(defaultVolatility({ text: 't', type: 'directive', source: 'user', importance: 8, action: 'ADD' })).toBe('stable');
    });
  });

  describe('VALID_MEMORY_TYPES', () => {
    it('exposes the v1 6-type closed enum', () => {
      expect(VALID_MEMORY_TYPES).toEqual(['claim', 'preference', 'directive', 'commitment', 'episode', 'summary']);
    });
    it('isValidMemoryType narrows correctly', () => {
      expect(isValidMemoryType('claim')).toBe(true);
      expect(isValidMemoryType('fact')).toBe(false);
      expect(isValidMemoryType(undefined)).toBe(false);
    });
  });
});
