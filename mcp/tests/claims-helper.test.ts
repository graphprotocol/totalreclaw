/**
 * Tests for the MCP server's canonical Claim builder, entity trapdoors,
 * digest helpers, and the TOTALRECLAW_CLAIM_FORMAT feature flag.
 *
 * Mirrors skill/plugin/claim-format.test.ts so any behavioral drift between
 * the plugin and MCP implementations shows up as a failing test.
 */

import crypto from 'node:crypto';
import {
  buildCanonicalClaim,
  buildDigestClaim,
  buildLegacyDoc,
  computeEntityTrapdoor,
  computeEntityTrapdoors,
  DIGEST_CATEGORY,
  DIGEST_TRAPDOOR,
  extractDigestFromClaim,
  isDigestBlob,
  mapTypeToCategory,
  readClaimFromBlob,
  resolveClaimFormat,
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

describe('resolveClaimFormat', () => {
  const savedEnv = process.env.TOTALRECLAW_CLAIM_FORMAT;
  afterEach(() => {
    if (savedEnv === undefined) delete process.env.TOTALRECLAW_CLAIM_FORMAT;
    else process.env.TOTALRECLAW_CLAIM_FORMAT = savedEnv;
  });

  test.each([
    [undefined, 'claim'],
    ['', 'claim'],
    ['claim', 'claim'],
    ['CLAIM', 'claim'],
    ['legacy', 'legacy'],
    ['LEGACY', 'legacy'],
    ['nonsense', 'claim'],
  ] as const)('resolves %s -> %s', (value, expected) => {
    if (value === undefined) delete process.env.TOTALRECLAW_CLAIM_FORMAT;
    else process.env.TOTALRECLAW_CLAIM_FORMAT = value;
    expect(resolveClaimFormat()).toBe(expected);
  });
});

describe('resolveDigestMode', () => {
  const savedEnv = process.env.TOTALRECLAW_DIGEST_MODE;
  afterEach(() => {
    if (savedEnv === undefined) delete process.env.TOTALRECLAW_DIGEST_MODE;
    else process.env.TOTALRECLAW_DIGEST_MODE = savedEnv;
  });

  test.each([
    [undefined, 'on'],
    ['', 'on'],
    ['on', 'on'],
    ['ON', 'on'],
    ['off', 'off'],
    ['OFF', 'off'],
    ['template', 'template'],
    ['TEMPLATE', 'template'],
    ['nonsense', 'on'],
  ] as const)('resolves %s -> %s', (value, expected) => {
    if (value === undefined) delete process.env.TOTALRECLAW_DIGEST_MODE;
    else process.env.TOTALRECLAW_DIGEST_MODE = value;
    expect(resolveDigestMode()).toBe(expected);
  });
});

describe('buildLegacyDoc', () => {
  test('byte-identical to historical MCP format', () => {
    const fact: ClaimInput = { text: 'Hello world.', type: 'fact' };
    const doc = buildLegacyDoc({
      fact,
      importance: 7,
      source: 'mcp_remember',
      createdAt: '2026-04-12T10:00:00Z',
    });
    const expected =
      '{"text":"Hello world.","metadata":{"type":"fact","importance":0.7,' +
      '"source":"mcp_remember","created_at":"2026-04-12T10:00:00Z"}}';
    expect(doc).toBe(expected);
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
