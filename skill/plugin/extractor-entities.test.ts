/**
 * Tests for entity + confidence extraction in parseFactsResponse.
 *
 * Run with: npx tsx extractor-entities.test.ts
 */

import {
  parseFactsResponse,
  normalizeConfidence,
  DEFAULT_EXTRACTION_CONFIDENCE,
  type ExtractedFact,
} from './extractor.js';

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
// Entity parsing
// ---------------------------------------------------------------------------

{
  const raw = JSON.stringify([
    {
      text: 'Pedro chose PostgreSQL because it is relational.',
      type: 'decision',
      importance: 8,
      confidence: 0.92,
      action: 'ADD',
      entities: [
        { name: 'Pedro', type: 'person', role: 'chooser' },
        { name: 'PostgreSQL', type: 'tool' },
      ],
    },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 1, 'entities: one fact parsed');
  const f = facts[0]!;
  assert(f.entities !== undefined && f.entities.length === 2, 'entities: two entities');
  assertEq(f.entities?.[0], { name: 'Pedro', type: 'person', role: 'chooser' }, 'entities: first entity has role');
  assertEq(f.entities?.[1], { name: 'PostgreSQL', type: 'tool' }, 'entities: second entity has no role');
  assert(f.confidence === 0.92, 'entities: confidence preserved');
}

// Backward compat: no entities field at all.
{
  const raw = JSON.stringify([
    { text: 'User lives in Lisbon.', type: 'fact', importance: 7, action: 'ADD' },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 1, 'backcompat: fact without entities parsed');
  assert(facts[0]!.entities === undefined, 'backcompat: entities undefined when absent');
  assert(facts[0]!.confidence === DEFAULT_EXTRACTION_CONFIDENCE, 'backcompat: default confidence 0.85');
}

// Empty entities array → undefined (dropped).
{
  const raw = JSON.stringify([
    { text: 'User likes tea.', type: 'preference', importance: 6, action: 'ADD', entities: [] },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 1, 'empty entities: parsed');
  assert(facts[0]!.entities === undefined, 'empty entities: dropped to undefined');
}

// Invalid entities are silently dropped without killing the fact.
{
  const raw = JSON.stringify([
    {
      text: 'Test mixed-validity entities.',
      type: 'fact',
      importance: 7,
      action: 'ADD',
      entities: [
        { name: 'Valid', type: 'concept' },
        { name: '', type: 'person' }, // empty name - invalid
        { name: 'NoType' }, // missing type - invalid
        { name: 'BadType', type: 'alien' }, // unknown type - invalid
        'not-an-object', // wrong shape - invalid
        { name: 'Another', type: 'tool', role: 'driver' },
      ],
    },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 1, 'mixed entities: fact survives');
  assertEq(
    facts[0]!.entities,
    [
      { name: 'Valid', type: 'concept' },
      { name: 'Another', type: 'tool', role: 'driver' },
    ],
    'mixed entities: invalid dropped, valid kept',
  );
}

// Entity type case normalization: we accept only lowercase canonical values.
{
  const raw = JSON.stringify([
    {
      text: 'Case test.',
      type: 'fact',
      importance: 7,
      action: 'ADD',
      entities: [
        { name: 'Acme', type: 'Company' }, // uppercase - we normalize to lowercase
        { name: 'Nope', type: 'PERSON' },
      ],
    },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 1, 'case test: parsed');
  assert(facts[0]!.entities?.length === 2, 'case test: both entities accepted after lowercase');
  assert(facts[0]!.entities?.[0].type === 'company', 'case test: Company → company');
  assert(facts[0]!.entities?.[1].type === 'person', 'case test: PERSON → person');
}

// ---------------------------------------------------------------------------
// Confidence handling
// ---------------------------------------------------------------------------

{
  assert(normalizeConfidence(0.5) === 0.5, 'confidence: in-range kept');
  assert(normalizeConfidence(1.0) === 1.0, 'confidence: 1.0 kept');
  assert(normalizeConfidence(0.0) === 0.0, 'confidence: 0.0 kept');
  assert(normalizeConfidence(1.7) === 1, 'confidence: > 1 clamped to 1');
  assert(normalizeConfidence(-0.3) === 0, 'confidence: < 0 clamped to 0');
  assert(normalizeConfidence(undefined) === DEFAULT_EXTRACTION_CONFIDENCE, 'confidence: undefined → 0.85');
  assert(normalizeConfidence('0.9') === DEFAULT_EXTRACTION_CONFIDENCE, 'confidence: string → default');
  assert(normalizeConfidence(NaN) === DEFAULT_EXTRACTION_CONFIDENCE, 'confidence: NaN → default');
}

{
  const raw = JSON.stringify([
    { text: 'Confidence high.', type: 'fact', importance: 7, confidence: 5, action: 'ADD' },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts[0]!.confidence === 1, 'confidence: out-of-range > 1 clamped at parse');
}

{
  const raw = JSON.stringify([
    { text: 'Confidence low.', type: 'fact', importance: 7, confidence: -2, action: 'ADD' },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts[0]!.confidence === 0, 'confidence: out-of-range < 0 clamped at parse');
}

// ---------------------------------------------------------------------------
// Importance filter still applies; entities don't bypass the 6-floor.
// ---------------------------------------------------------------------------
{
  const raw = JSON.stringify([
    {
      text: 'Low importance chatter.',
      type: 'fact',
      importance: 3,
      action: 'ADD',
      entities: [{ name: 'Thing', type: 'concept' }],
    },
  ]);
  const facts = parseFactsResponse(raw);
  assert(facts.length === 0, 'importance filter: low importance still dropped even with entities');
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
