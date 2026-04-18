/**
 * Tests for the MCP server's canonical Claim builder, entity trapdoors,
 * and digest helpers.
 *
 * Mirrors skill/plugin/claim-format.test.ts so any behavioral drift between
 * the plugin and MCP implementations shows up as a failing test.
 *
 * v1 env var cleanup: the TOTALRECLAW_CLAIM_FORMAT and TOTALRECLAW_DIGEST_MODE
 * env vars were removed. `resolveDigestMode` still exists and unconditionally
 * returns `'on'`; `resolveClaimFormat` and `buildLegacyDoc` were deleted.
 */

import crypto from 'node:crypto';
import {
  buildCanonicalClaim,
  buildDigestClaim,
  computeEntityTrapdoor,
  computeEntityTrapdoors,
  DIGEST_CATEGORY,
  DIGEST_TRAPDOOR,
  extractDigestFromClaim,
  isDigestBlob,
  mapTypeToCategory,
  readClaimFromBlob,
  resolveDigestMode,
  type ClaimInput,
} from '../src/claims-helper.js';

import * as core from '@totalreclaw/core';

describe('mapTypeToCategory', () => {
  test.each([
    ['fact', 'fact'],
    ['preference', 'pref'],
    ['decision', 'dec'],
    ['episodic', 'epi'],
    ['goal', 'goal'],
    ['context', 'ctx'],
    ['summary', 'sum'],
  ] as const)('maps %s -> %s', (input, expected) => {
    expect(mapTypeToCategory(input)).toBe(expected);
  });

  test('maps undefined -> fact (default)', () => {
    expect(mapTypeToCategory(undefined)).toBe('fact');
  });
});

describe('buildCanonicalClaim', () => {
  test('byte-identical for a decision-with-entities claim', () => {
    const fact: ClaimInput = {
      text: 'Pedro chose PostgreSQL because it is relational and needs ACID.',
      type: 'decision',
      confidence: 0.92,
      entities: [
        { name: 'Pedro', type: 'person', role: 'chooser' },
        { name: 'PostgreSQL', type: 'tool' },
      ],
    };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 8,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });

    const expected =
      '{"t":"Pedro chose PostgreSQL because it is relational and needs ACID.",' +
      '"c":"dec","cf":0.92,"i":8,"sa":"mcp-server","ea":"2026-04-12T10:00:00Z",' +
      '"e":[{"n":"Pedro","tp":"person","r":"chooser"},{"n":"PostgreSQL","tp":"tool"}]}';
    expect(canonical).toBe(expected);
  });

  test('round-trips through core.parseClaimOrLegacy', () => {
    const fact: ClaimInput = {
      text: 'Pedro chose PostgreSQL because it is relational and needs ACID.',
      type: 'decision',
      confidence: 0.92,
      entities: [
        { name: 'Pedro', type: 'person', role: 'chooser' },
        { name: 'PostgreSQL', type: 'tool' },
      ],
    };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 8,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    const parsed = JSON.parse(core.parseClaimOrLegacy(canonical));
    expect(parsed.t).toBe(fact.text);
    expect(parsed.c).toBe('dec');
    expect(parsed.e).toHaveLength(2);
  });

  test('omits e field when no entities', () => {
    const fact: ClaimInput = {
      text: 'The user lives in Lisbon.',
      type: 'fact',
    };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 7,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    expect(canonical).not.toContain('"e":');
    expect(canonical).toContain('"cf":0.85'); // default confidence
  });

  test('omits role when absent on entity', () => {
    const fact: ClaimInput = {
      text: 'Pedro works at Acme.',
      type: 'fact',
      confidence: 0.9,
      entities: [{ name: 'Acme', type: 'company' }],
    };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 7,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    expect(canonical).toContain('"e":[{"n":"Acme","tp":"company"}]');
  });

  test('defaults type to fact when absent', () => {
    const fact: ClaimInput = { text: 'Hello world.' };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 5,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    const parsed = JSON.parse(canonical);
    expect(parsed.c).toBe('fact');
  });
});

describe('computeEntityTrapdoor', () => {
  test('deterministic for same input', () => {
    const a = computeEntityTrapdoor('PostgreSQL');
    const b = computeEntityTrapdoor('PostgreSQL');
    expect(a).toBe(b);
    expect(/^[0-9a-f]{64}$/.test(a)).toBe(true);
  });

  test('case and whitespace normalized', () => {
    const a = computeEntityTrapdoor('PostgreSQL');
    const b = computeEntityTrapdoor('postgresql');
    const c = computeEntityTrapdoor('  POSTGRESQL  ');
    expect(a).toBe(b);
    expect(a).toBe(c);
  });

  test('entity: namespace distinct from raw word hash', () => {
    const entityTd = computeEntityTrapdoor('postgresql');
    const wordHash = crypto.createHash('sha256').update('postgresql').digest('hex');
    expect(entityTd).not.toBe(wordHash);
  });

  test('equals sha256("entity:" + normalized) (known-answer)', () => {
    const entityTd = computeEntityTrapdoor('PostgreSQL');
    const expected = crypto.createHash('sha256').update('entity:postgresql').digest('hex');
    expect(entityTd).toBe(expected);
  });
});

describe('computeEntityTrapdoors', () => {
  test('dedups aliases by normalized name', () => {
    const td = computeEntityTrapdoors([
      { name: 'Pedro', type: 'person' },
      { name: 'pedro', type: 'person' },
      { name: '  PEDRO ', type: 'person' },
    ]);
    expect(td).toHaveLength(1);
  });

  test('returns empty array for undefined / empty input', () => {
    expect(computeEntityTrapdoors(undefined)).toEqual([]);
    expect(computeEntityTrapdoors([])).toEqual([]);
  });

  test('preserves distinct names', () => {
    const td = computeEntityTrapdoors([
      { name: 'Pedro', type: 'person' },
      { name: 'PostgreSQL', type: 'tool' },
    ]);
    expect(td).toHaveLength(2);
  });
});

describe('resolveDigestMode (v1 — env var removed, always on)', () => {
  const savedEnv = process.env.TOTALRECLAW_DIGEST_MODE;
  afterEach(() => {
    if (savedEnv === undefined) delete process.env.TOTALRECLAW_DIGEST_MODE;
    else process.env.TOTALRECLAW_DIGEST_MODE = savedEnv;
  });

  // TOTALRECLAW_DIGEST_MODE was removed in v1. The function must unconditionally
  // return 'on' regardless of what the env var is set to.
  test.each([
    [undefined, 'on'],
    ['', 'on'],
    ['on', 'on'],
    ['off', 'on'],       // was 'off' — now ignored
    ['template', 'on'],  // was 'template' — now ignored
    ['nonsense', 'on'],
  ] as const)('env=%s -> resolveDigestMode=%s (env var has no effect)', (value, expected) => {
    if (value === undefined) delete process.env.TOTALRECLAW_DIGEST_MODE;
    else process.env.TOTALRECLAW_DIGEST_MODE = value;
    expect(resolveDigestMode()).toBe(expected);
  });
});

describe('v1 env var cleanup — removed vars have no effect', () => {
  test('TOTALRECLAW_CLAIM_FORMAT=legacy does not flip the write path', () => {
    const saved = process.env.TOTALRECLAW_CLAIM_FORMAT;
    try {
      process.env.TOTALRECLAW_CLAIM_FORMAT = 'legacy';
      // buildCanonicalClaim should produce the v0 canonical blob regardless
      // — there is no longer a legacy raw-text fallback exposed.
      const blob = buildCanonicalClaim({
        fact: { text: 'test', type: 'fact' },
        importance: 7,
        sourceAgent: 'mcp-server',
      });
      const parsed = JSON.parse(blob);
      expect(parsed.t).toBe('test');
      expect(parsed.c).toBe('fact');
    } finally {
      if (saved === undefined) delete process.env.TOTALRECLAW_CLAIM_FORMAT;
      else process.env.TOTALRECLAW_CLAIM_FORMAT = saved;
    }
  });
});

describe('readClaimFromBlob', () => {
  test('new canonical Claim: extracts text / importance / category', () => {
    const out = readClaimFromBlob(
      JSON.stringify({ t: 'prefers PostgreSQL', c: 'pref', cf: 0.9, i: 8, sa: 'mcp' }),
    );
    expect(out.text).toBe('prefers PostgreSQL');
    expect(out.importance).toBe(8);
    expect(out.category).toBe('pref');
  });

  test('new canonical Claim with entities', () => {
    const out = readClaimFromBlob(
      JSON.stringify({
        t: 'lives in Lisbon',
        c: 'fact',
        cf: 0.95,
        i: 9,
        sa: 'mcp',
        e: [{ n: 'Lisbon', tp: 'place' }],
      }),
    );
    expect(out.text).toBe('lives in Lisbon');
    expect(out.importance).toBe(9);
  });

  test('clamps importance > 10 and < 1', () => {
    const hi = readClaimFromBlob(
      JSON.stringify({ t: 'x', c: 'fact', cf: 0.9, i: 99, sa: 'mcp' }),
    );
    expect(hi.importance).toBe(10);
    const lo = readClaimFromBlob(
      JSON.stringify({ t: 'x', c: 'fact', cf: 0.9, i: 0, sa: 'mcp' }),
    );
    expect(lo.importance).toBe(1);
  });

  test('legacy {text, metadata} format: rescales importance 0.7 -> 7', () => {
    const out = readClaimFromBlob(
      JSON.stringify({
        text: 'legacy fact',
        metadata: { type: 'fact', importance: 0.7, source: 'mcp_remember' },
      }),
    );
    expect(out.text).toBe('legacy fact');
    expect(out.importance).toBe(7);
    expect(out.category).toBe('fact');
  });

  test('legacy with 0.85 rounds to 9', () => {
    const out = readClaimFromBlob(
      JSON.stringify({
        text: 'prefers dark mode',
        metadata: { type: 'preference', importance: 0.85 },
      }),
    );
    expect(out.importance).toBe(9);
    expect(out.category).toBe('preference');
  });

  test('bare legacy doc (no metadata) gets default importance', () => {
    const out = readClaimFromBlob(JSON.stringify({ text: 'bare' }));
    expect(out.text).toBe('bare');
    expect(out.importance).toBe(5);
  });

  test('malformed JSON falls through to raw text', () => {
    const out = readClaimFromBlob('not valid json');
    expect(out.text).toBe('not valid json');
    expect(out.importance).toBe(5);
  });

  test('empty object falls through to raw text', () => {
    const out = readClaimFromBlob('{}');
    expect(out.text).toBe('{}');
  });

  test('digest blob surfaces category and importance', () => {
    const out = readClaimFromBlob(
      JSON.stringify({
        t: '{"prompt_text":"You are..."}',
        c: 'dig',
        cf: 1.0,
        i: 10,
        sa: 'mcp-server-digest',
      }),
    );
    expect(out.category).toBe('dig');
    expect(out.importance).toBe(10);
  });
});

describe('digest helpers', () => {
  test('DIGEST_TRAPDOOR == sha256("type:digest")', () => {
    const expected = crypto.createHash('sha256').update('type:digest').digest('hex');
    expect(DIGEST_TRAPDOOR).toBe(expected);
  });

  test('buildDigestClaim round-trips through extractDigestFromClaim', () => {
    const digestJson = JSON.stringify({ prompt_text: 'Hello, user.' });
    const canonical = buildDigestClaim({
      digestJson,
      compiledAt: '2026-04-12T10:00:00Z',
    });
    const digest = extractDigestFromClaim(canonical);
    expect(digest).not.toBeNull();
    expect(digest.prompt_text).toBe('Hello, user.');
  });

  test('extractDigestFromClaim returns null for non-digest claims', () => {
    const fact: ClaimInput = { text: 'not a digest', type: 'fact' };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 5,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    expect(extractDigestFromClaim(canonical)).toBeNull();
  });

  test('extractDigestFromClaim returns null for malformed wrapper', () => {
    expect(extractDigestFromClaim('not json')).toBeNull();
    expect(extractDigestFromClaim(JSON.stringify({ c: 'dig', t: 'not json' }))).toBeNull();
    expect(
      extractDigestFromClaim(JSON.stringify({ c: 'dig', t: '{"no":"prompt_text"}' })),
    ).toBeNull();
  });

  test('isDigestBlob identifies digest claims', () => {
    const digestCanonical = buildDigestClaim({
      digestJson: JSON.stringify({ prompt_text: 'x' }),
      compiledAt: '2026-04-12T10:00:00Z',
    });
    expect(isDigestBlob(digestCanonical)).toBe(true);
  });

  test('isDigestBlob rejects regular canonical claims', () => {
    const fact: ClaimInput = { text: 'not a digest', type: 'fact' };
    const canonical = buildCanonicalClaim({
      fact,
      importance: 5,
      sourceAgent: 'mcp-server',
      extractedAt: '2026-04-12T10:00:00Z',
    });
    expect(isDigestBlob(canonical)).toBe(false);
  });

  test('isDigestBlob rejects legacy docs', () => {
    const legacy = JSON.stringify({ text: 'legacy', metadata: { type: 'fact' } });
    expect(isDigestBlob(legacy)).toBe(false);
  });

  test('isDigestBlob returns false on malformed JSON', () => {
    expect(isDigestBlob('not json')).toBe(false);
  });

  test('DIGEST_CATEGORY constant is "dig"', () => {
    expect(DIGEST_CATEGORY).toBe('dig');
  });
});
