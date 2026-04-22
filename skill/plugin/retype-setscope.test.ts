/**
 * Tests for retype-setscope.ts — 3.3.1-rc.2 retype + set_scope operations.
 *
 * Covers:
 *   - validateRetypeArgs accepts / rejects well- and ill-formed inputs
 *   - validateSetScopeArgs accepts / rejects well- and ill-formed inputs
 *   - executeRetype: fetches existing, decrypts, mutates type, writes
 *     tombstone + new, returns previous/new type in result
 *   - executeSetScope: same, for scope
 *   - missing fact returns clear error
 *   - malformed blob returns clear error
 *   - submitBatch failure propagates as success=false
 *
 * Run with: npx tsx retype-setscope.test.ts
 */

import { Buffer } from 'node:buffer';

import {
  executeRetype,
  executeSetScope,
  validateRetypeArgs,
  validateSetScopeArgs,
  type RetypeSetScopeDeps,
} from './retype-setscope.js';

import { buildV1ClaimBlob } from './claims-helper.js';

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// validateRetypeArgs
// ---------------------------------------------------------------------------

{
  const r = validateRetypeArgs({ fact_id: 'abc-123', new_type: 'preference' });
  assert(r.ok === true, 'validateRetypeArgs: accepts fact_id + new_type');
  if (r.ok) assert(r.newType === 'preference', 'validateRetypeArgs: newType=preference');
}

{
  const r = validateRetypeArgs({ factId: 'abc-123', newType: 'claim' });
  assert(r.ok === true, 'validateRetypeArgs: accepts camelCase variants');
}

{
  const r = validateRetypeArgs({ fact_id: 'abc-123', new_type: 'banana' });
  assert(r.ok === false, 'validateRetypeArgs: rejects invalid type');
}

{
  const r = validateRetypeArgs({ new_type: 'claim' });
  assert(r.ok === false, 'validateRetypeArgs: rejects missing fact_id');
}

{
  const r = validateRetypeArgs({ fact_id: '', new_type: 'claim' });
  assert(r.ok === false, 'validateRetypeArgs: rejects empty fact_id');
}

{
  const r = validateRetypeArgs(null);
  assert(r.ok === false, 'validateRetypeArgs: rejects null');
}

// ---------------------------------------------------------------------------
// validateSetScopeArgs
// ---------------------------------------------------------------------------

{
  const r = validateSetScopeArgs({ fact_id: 'abc-123', new_scope: 'work' });
  assert(r.ok === true, 'validateSetScopeArgs: accepts fact_id + new_scope');
  if (r.ok) assert(r.newScope === 'work', 'validateSetScopeArgs: newScope=work');
}

{
  const r = validateSetScopeArgs({ fact_id: 'abc-123', new_scope: 'health' });
  assert(r.ok === true, 'validateSetScopeArgs: accepts health');
}

{
  const r = validateSetScopeArgs({ fact_id: 'abc-123', new_scope: 'banana' });
  assert(r.ok === false, 'validateSetScopeArgs: rejects invalid scope');
}

{
  const r = validateSetScopeArgs({ fact_id: '', new_scope: 'work' });
  assert(r.ok === false, 'validateSetScopeArgs: rejects empty fact_id');
}

// ---------------------------------------------------------------------------
// executeRetype / executeSetScope — integration-style with mock deps
// ---------------------------------------------------------------------------

function buildMockDeps(opts: {
  existingV1Blob: string;
  submitShouldFail?: boolean;
  fetchReturnsNull?: boolean;
}): RetypeSetScopeDeps & {
  _captured: { payloads: Buffer[] | null; fetchCalls: string[] };
} {
  const captured = { payloads: null as Buffer[] | null, fetchCalls: [] as string[] };
  return {
    owner: '0xowner',
    sourceAgent: 'test',
    fetchFactById: async (factId: string) => {
      captured.fetchCalls.push(factId);
      if (opts.fetchReturnsNull) return null;
      return {
        id: factId,
        encryptedBlob: '0x' + Buffer.from(opts.existingV1Blob, 'utf-8').toString('hex'),
        encryptedEmbedding: null,
        decayScore: '1000000',
        timestamp: new Date().toISOString(),
        isActive: true,
      };
    },
    decryptBlob: (hex: string) => {
      // "decrypt" = raw hex → utf-8 (mock)
      return Buffer.from(hex, 'hex').toString('utf-8');
    },
    encryptBlob: (plaintext: string) => {
      // "encrypt" = utf-8 → hex (mock)
      return Buffer.from(plaintext, 'utf-8').toString('hex');
    },
    submitBatch: async (payloads: Buffer[]) => {
      captured.payloads = payloads;
      if (opts.submitShouldFail) return { txHash: '0xfail', success: false };
      return { txHash: '0xfeedfeed', success: true };
    },
    generateIndices: async (_text: string, _entities: string[]) => ({
      blindIndices: ['test-trapdoor-1', 'test-trapdoor-2'],
      encryptedEmbedding: 'testembedding',
    }),
    _captured: captured,
  };
}

// Happy path — retype a claim to a preference
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-123',
    text: 'I prefer PostgreSQL over MySQL',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
    importance: 7,
    confidence: 0.9,
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob });
  const r = await executeRetype('abc-123', 'preference', deps);
  assert(r.success, 'executeRetype: success');
  assert(r.fact_id === 'abc-123', 'executeRetype: fact_id preserved');
  assert(r.new_fact_id !== undefined && r.new_fact_id !== 'abc-123', 'executeRetype: new_fact_id allocated');
  assert(r.previous_type === 'claim', 'executeRetype: previous_type captured');
  assert(r.new_type === 'preference', 'executeRetype: new_type set');
  assert(r.tx_hash === '0xfeedfeed', 'executeRetype: tx_hash propagated');
  assert(deps._captured.payloads?.length === 2, 'executeRetype: submitted 2 payloads (tombstone + new)');
}

// Happy path — set scope to work
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-456',
    text: 'My manager is Alice',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
    importance: 5,
    confidence: 0.9,
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob });
  const r = await executeSetScope('abc-456', 'work', deps);
  assert(r.success, 'executeSetScope: success');
  assert(r.new_scope === 'work', 'executeSetScope: new_scope set');
  // readV1Blob normalizes missing scope to 'unspecified' → that's what projectFromDecrypted reports.
  assert(r.previous_scope === 'unspecified', 'executeSetScope: previous_scope=unspecified (default when not set)');
  assert(deps._captured.payloads?.length === 2, 'executeSetScope: submitted 2 payloads');
}

// Fact not found
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-789',
    text: 'doesnt matter',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob, fetchReturnsNull: true });
  const r = await executeRetype('abc-789', 'preference', deps);
  assert(!r.success, 'executeRetype: not-found → success=false');
  assert(r.error?.includes('not found') ?? false, 'executeRetype: error mentions not-found');
}

// Submit batch fails
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-err',
    text: 'stay here',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob, submitShouldFail: true });
  const r = await executeSetScope('abc-err', 'work', deps);
  assert(!r.success, 'executeSetScope: submit-fails → success=false');
  assert(r.tx_hash === '0xfail', 'executeSetScope: failed tx hash surfaced');
}

// Invalid new_type
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-1',
    text: 'x',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob });
  const r = await executeRetype('abc-1', 'bogus' as unknown as 'claim', deps);
  assert(!r.success, 'executeRetype: invalid type → error');
}

// Invalid new_scope
{
  const v1Blob = buildV1ClaimBlob({
    id: 'abc-2',
    text: 'x',
    type: 'claim',
    source: 'user',
    createdAt: new Date().toISOString(),
  });
  const deps = buildMockDeps({ existingV1Blob: v1Blob });
  const r = await executeSetScope('abc-2', 'bogus' as unknown as 'work', deps);
  assert(!r.success, 'executeSetScope: invalid scope → error');
}

// Malformed blob (not parseable)
{
  const deps = buildMockDeps({ existingV1Blob: 'not json at all' });
  const r = await executeRetype('abc-mal', 'preference', deps);
  assert(!r.success, 'executeRetype: malformed blob → success=false');
  assert(r.error?.toLowerCase().includes('blob') ?? false, 'executeRetype: error mentions blob');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
