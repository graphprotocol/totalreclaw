/**
 * Tests for the canonical Claim builder, entity trapdoors, and the
 * TOTALRECLAW_CLAIM_FORMAT feature flag.
 *
 * Run with: npx tsx claim-format.test.ts
 */

import crypto from 'node:crypto';
import {
  buildCanonicalClaim,
  buildLegacyDoc,
  computeEntityTrapdoor,
  computeEntityTrapdoors,
  mapTypeToCategory,
  readClaimFromBlob,
  resolveClaimFormat,
} from './claims-helper.js';
import type { ExtractedFact } from './extractor.js';

import * as core from '@totalreclaw/core';

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function assertEq<T>(actual: T, expected: T, name: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    console.log(`  actual:   ${JSON.stringify(actual)}`);
    console.log(`  expected: ${JSON.stringify(expected)}`);
  }
  assert(ok, name);
}

// ---------------------------------------------------------------------------
// mapTypeToCategory
// ---------------------------------------------------------------------------

assert(mapTypeToCategory('fact') === 'fact', 'category: fact → fact');
assert(mapTypeToCategory('preference') === 'pref', 'category: preference → pref');
assert(mapTypeToCategory('decision') === 'dec', 'category: decision → dec');
assert(mapTypeToCategory('episodic') === 'epi', 'category: episodic → epi');
assert(mapTypeToCategory('goal') === 'goal', 'category: goal → goal');
assert(mapTypeToCategory('context') === 'ctx', 'category: context → ctx');
assert(mapTypeToCategory('summary') === 'sum', 'category: summary → sum');

// ---------------------------------------------------------------------------
// buildCanonicalClaim
// ---------------------------------------------------------------------------

{
  const fact: ExtractedFact = {
    text: 'Pedro chose PostgreSQL because it is relational and needs ACID.',
    type: 'decision',
    importance: 8,
    confidence: 0.92,
    action: 'ADD',
    entities: [
      { name: 'Pedro', type: 'person', role: 'chooser' },
      { name: 'PostgreSQL', type: 'tool' },
    ],
  };
  const canonical = buildCanonicalClaim({
    fact,
    importance: 8,
    sourceAgent: 'openclaw-plugin',
    extractedAt: '2026-04-12T10:00:00Z',
  });

  const expected =
    '{"t":"Pedro chose PostgreSQL because it is relational and needs ACID.",' +
    '"c":"dec","cf":0.92,"i":8,"sa":"openclaw-plugin","ea":"2026-04-12T10:00:00Z",' +
    '"e":[{"n":"Pedro","tp":"person","r":"chooser"},{"n":"PostgreSQL","tp":"tool"}]}';
  assertEq(canonical, expected, 'canonical: decision with entities byte-identical');

  // Round-trip via parseClaimOrLegacy confirms the core agrees it's valid.
  const parsed = JSON.parse(core.parseClaimOrLegacy(canonical));
  assert(parsed.t === fact.text, 'canonical: round-trip preserves text');
  assert(parsed.c === 'dec', 'canonical: round-trip preserves category');
  assert(parsed.e.length === 2, 'canonical: round-trip preserves entities');
}

// Claim without entities: `e` field omitted entirely.
{
  const fact: ExtractedFact = {
    text: 'The user lives in Lisbon.',
    type: 'fact',
    importance: 7,
    action: 'ADD',
  };
  const canonical = buildCanonicalClaim({
    fact,
    importance: 7,
    sourceAgent: 'openclaw-plugin',
    extractedAt: '2026-04-12T10:00:00Z',
  });
  assert(!canonical.includes('"e":'), 'canonical: entities omitted when empty');
  assert(canonical.includes('"cf":0.85'), 'canonical: default confidence 0.85 when fact has none');
}

// Claim with empty-string role: treated as undefined (canonicalizeClaim is strict).
{
  const fact: ExtractedFact = {
    text: 'Pedro works at Acme.',
    type: 'fact',
    importance: 7,
    confidence: 0.9,
    action: 'ADD',
    entities: [{ name: 'Acme', type: 'company' }],
  };
  const canonical = buildCanonicalClaim({
    fact,
    importance: 7,
    sourceAgent: 'openclaw-plugin',
    extractedAt: '2026-04-12T10:00:00Z',
  });
  assert(canonical.includes('"e":[{"n":"Acme","tp":"company"}]'), 'canonical: role omitted when absent');
}

// ---------------------------------------------------------------------------
// Entity trapdoors
// ---------------------------------------------------------------------------

// Deterministic: same input → same output.
{
  const a = computeEntityTrapdoor('PostgreSQL');
  const b = computeEntityTrapdoor('PostgreSQL');
  assert(a === b, 'trapdoor: deterministic for same input');
  assert(/^[0-9a-f]{64}$/.test(a), 'trapdoor: 64-hex-char SHA-256');
}

// Case / whitespace normalization: identical trapdoors.
{
  const a = computeEntityTrapdoor('PostgreSQL');
  const b = computeEntityTrapdoor('postgresql');
  const c = computeEntityTrapdoor('  POSTGRESQL  ');
  assert(a === b, 'trapdoor: case-insensitive (Postgres mixed == lower)');
  assert(a === c, 'trapdoor: whitespace trimmed');
}

// `entity:` prefix namespaces from word trapdoors.
{
  const entityTd = computeEntityTrapdoor('postgresql');
  const wordHash = crypto.createHash('sha256').update('postgresql').digest('hex');
  assert(entityTd !== wordHash, 'trapdoor: entity prefix distinct from raw word hash');

  // Independently derive: sha256('entity:postgresql')
  const expected = crypto.createHash('sha256').update('entity:postgresql').digest('hex');
  assert(entityTd === expected, 'trapdoor: equals sha256("entity:" + normalized)');
}

// Multi-entity dedup: two references to the same name → one trapdoor.
{
  const td = computeEntityTrapdoors([
    { name: 'Pedro', type: 'person' },
    { name: 'pedro', type: 'person' },
    { name: '  PEDRO ', type: 'person' },
  ]);
  assert(td.length === 1, 'trapdoors: three aliases dedup to one');
}

// Empty / undefined inputs → empty array.
{
  assert(computeEntityTrapdoors(undefined).length === 0, 'trapdoors: undefined → []');
  assert(computeEntityTrapdoors([]).length === 0, 'trapdoors: empty array → []');
}

// ---------------------------------------------------------------------------
// Claim format feature flag
// ---------------------------------------------------------------------------

{
  const original = process.env.TOTALRECLAW_CLAIM_FORMAT;
  try {
    delete process.env.TOTALRECLAW_CLAIM_FORMAT;
    assert(resolveClaimFormat() === 'claim', 'flag: unset → claim (default)');

    process.env.TOTALRECLAW_CLAIM_FORMAT = 'claim';
    assert(resolveClaimFormat() === 'claim', 'flag: explicit claim');

    process.env.TOTALRECLAW_CLAIM_FORMAT = 'CLAIM';
    assert(resolveClaimFormat() === 'claim', 'flag: case-insensitive CLAIM');

    process.env.TOTALRECLAW_CLAIM_FORMAT = 'legacy';
    assert(resolveClaimFormat() === 'legacy', 'flag: legacy');

    process.env.TOTALRECLAW_CLAIM_FORMAT = 'LEGACY';
    assert(resolveClaimFormat() === 'legacy', 'flag: case-insensitive LEGACY');

    process.env.TOTALRECLAW_CLAIM_FORMAT = 'nonsense';
    assert(resolveClaimFormat() === 'claim', 'flag: unknown value → claim');
  } finally {
    if (original === undefined) delete process.env.TOTALRECLAW_CLAIM_FORMAT;
    else process.env.TOTALRECLAW_CLAIM_FORMAT = original;
  }
}

// Legacy doc shape matches the pre-KG format byte-for-byte.
{
  const fact: ExtractedFact = {
    text: 'Hello world.',
    type: 'fact',
    importance: 7,
    action: 'ADD',
  };
  const doc = buildLegacyDoc({
    fact,
    importance: 7,
    source: 'auto-extraction',
    createdAt: '2026-04-12T10:00:00Z',
  });
  const expected =
    '{"text":"Hello world.","metadata":{"type":"fact","importance":0.7,' +
    '"source":"auto-extraction","created_at":"2026-04-12T10:00:00Z"}}';
  assertEq(doc, expected, 'legacy: byte-identical doc shape');
}

// ---------------------------------------------------------------------------
// readClaimFromBlob — decrypted blob reader (handles new + legacy formats)
// ---------------------------------------------------------------------------

{
  // New canonical Claim format
  const outNew = readClaimFromBlob(
    JSON.stringify({ t: 'prefers PostgreSQL', c: 'pref', cf: 0.9, i: 8, sa: 'oc' }),
  );
  assertEq(outNew.text, 'prefers PostgreSQL', 'readClaim: new format text');
  assertEq(outNew.importance, 8, 'readClaim: new format importance');
  assertEq(outNew.category, 'pref', 'readClaim: new format category');

  // New format with entities
  const outEntities = readClaimFromBlob(
    JSON.stringify({
      t: 'lives in Lisbon', c: 'fact', cf: 0.95, i: 9, sa: 'oc',
      e: [{ n: 'Lisbon', tp: 'place' }],
    }),
  );
  assertEq(outEntities.text, 'lives in Lisbon', 'readClaim: new+entities text');
  assertEq(outEntities.importance, 9, 'readClaim: new+entities importance');

  // Importance clamping (defensive — importance should be 1..10)
  const outHigh = readClaimFromBlob(JSON.stringify({ t: 'x', c: 'fact', cf: 0.9, i: 99, sa: 'oc' }));
  assertEq(outHigh.importance, 10, 'readClaim: clamps importance > 10');
  const outLow = readClaimFromBlob(JSON.stringify({ t: 'x', c: 'fact', cf: 0.9, i: 0, sa: 'oc' }));
  assertEq(outLow.importance, 1, 'readClaim: clamps importance < 1');

  // Legacy plugin {text, metadata} format
  const outLegacy = readClaimFromBlob(
    JSON.stringify({
      text: 'legacy fact',
      metadata: { type: 'fact', importance: 0.7, source: 'auto-extraction' },
    }),
  );
  assertEq(outLegacy.text, 'legacy fact', 'readClaim: legacy text');
  assertEq(outLegacy.importance, 7, 'readClaim: legacy importance 0.7 → 7');
  assertEq(outLegacy.category, 'fact', 'readClaim: legacy category from metadata.type');

  // Legacy with 0.85 rounds to 9
  const outRound = readClaimFromBlob(
    JSON.stringify({ text: 'prefers dark mode', metadata: { type: 'preference', importance: 0.85 } }),
  );
  assertEq(outRound.importance, 9, 'readClaim: legacy 0.85 → 9 (rounded)');
  assertEq(outRound.category, 'preference', 'readClaim: legacy preference category');

  // Bare legacy — no metadata
  const outBare = readClaimFromBlob(JSON.stringify({ text: 'bare' }));
  assertEq(outBare.text, 'bare', 'readClaim: bare legacy text');
  assertEq(outBare.importance, 5, 'readClaim: bare legacy default importance');

  // Malformed JSON → fallback to raw string
  const outBad = readClaimFromBlob('not valid json');
  assertEq(outBad.text, 'not valid json', 'readClaim: malformed → raw text');
  assertEq(outBad.importance, 5, 'readClaim: malformed default importance');

  // Empty object
  const outEmpty = readClaimFromBlob('{}');
  assertEq(outEmpty.text, '{}', 'readClaim: empty object → raw fallback');

  // Digest blob (new canonical with c='dig')
  const outDigest = readClaimFromBlob(
    JSON.stringify({
      t: '{"prompt_text":"You are..."}',
      c: 'dig',
      cf: 1.0,
      i: 10,
      sa: 'openclaw-plugin-digest',
    }),
  );
  assertEq(outDigest.category, 'dig', 'readClaim: digest blob category');
  assertEq(outDigest.importance, 10, 'readClaim: digest blob importance');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
} else {
  console.log('\nALL TESTS PASSED');
}

export {};
