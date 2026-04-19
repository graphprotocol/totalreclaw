/**
 * Cross-client parity: MCP buildV1ClaimBlob + pin path produce v1.1 blobs
 * that match the plugin's output for identical inputs.
 *
 * Since the plugin helper lives in the skill/plugin tree and the MCP helper
 * lives here, a true byte-for-byte comparison requires loading both into
 * the same process. We sidestep that by capturing the canonical v1 JSON
 * that pin.ts emits and asserting its SHAPE + field semantics match what
 * plugin/pin-unpin.test.ts asserts for its parity case (`# parity: ...`).
 *
 * If this test passes AND the plugin's equivalent parity assertions pass,
 * both clients are producing the same canonical v1.1 blob for the same
 * source fact. Round-trip through `validateMemoryClaimV1` (in core) is the
 * shared single source of truth for field ordering.
 */

import { executePinOperation, type PinOpDeps } from '../src/tools/pin';
import type { SubgraphSearchFact } from '../src/subgraph/search';

function makeFact(id: string, blob: string): SubgraphSearchFact {
  return {
    id,
    encryptedBlob: Buffer.from(blob, 'utf-8').toString('hex'),
    encryptedEmbedding: null,
    decayScore: '1.0',
    timestamp: new Date().toISOString(),
    isActive: true,
  };
}

function makeDeps(overrides: Partial<PinOpDeps> & { _submitted?: Buffer[][] } = {}): PinOpDeps & { _submitted: Buffer[][] } {
  const _submitted: Buffer[][] = [];
  return {
    owner: '0xtest',
    sourceAgent: 'mcp-server-pin',
    async fetchFactById() { return null; },
    decryptBlob(hex: string) {
      return Buffer.from(hex, 'hex').toString('utf-8');
    },
    encryptBlob(plaintext: string) {
      return Buffer.from(plaintext, 'utf-8').toString('hex');
    },
    async submitBatch(payloads: Buffer[]) {
      _submitted.push(payloads);
      return { txHash: '0xparity', success: true };
    },
    async generateIndices() {
      return { blindIndices: [], encryptedEmbedding: undefined };
    },
    ...overrides,
    _submitted,
  } as PinOpDeps & { _submitted: Buffer[][] };
}

describe('Cross-client parity: MCP pin path matches plugin v1.1 shape', () => {
  test('v1.1 pin output contains only canonical v1 fields + pin_status', async () => {
    const v1Src = JSON.stringify({
      id: '01900000-0000-7000-8000-000000000200',
      text: 'parity canary',
      type: 'claim',
      source: 'user',
      created_at: '2026-04-19T12:00:00.000Z',
      schema_version: '1.0',
    });

    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1Src); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    await executePinOperation('parity-canary', 'pinned', deps);

    expect(capturedPlaintext).not.toBeNull();
    const parsed = JSON.parse(capturedPlaintext!);

    // Required v1 fields present (matches plugin parity assertions at
    // skill/plugin/pin-unpin.test.ts:~985-1005).
    expect(typeof parsed.id).toBe('string');
    expect(parsed.text).toBe('parity canary');
    expect(parsed.type).toBe('claim');
    expect(parsed.source).toBe('user');
    expect(typeof parsed.created_at).toBe('string');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.superseded_by).toBe('parity-canary');

    // NO v0 leak — same assertions the plugin makes.
    expect(Object.prototype.hasOwnProperty.call(parsed, 't')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(parsed, 'c')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(parsed, 'st')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(parsed, 'sup')).toBe(false);
  });

  test('v1.1 unpin output matches plugin unpin shape', async () => {
    const v1Pinned = JSON.stringify({
      id: '01900000-0000-7000-8000-000000000201',
      text: 'unpin parity canary',
      type: 'preference',
      source: 'user',
      created_at: '2026-04-19T12:00:00.000Z',
      schema_version: '1.0',
      pin_status: 'pinned',
    });

    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v1Pinned); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    await executePinOperation('unpin-canary', 'active', deps);

    const parsed = JSON.parse(capturedPlaintext!);
    // The plugin writes pin_status:"unpinned" on an unpin (explicit, not
    // absent) — MCP must match.
    expect(parsed.pin_status).toBe('unpinned');
    expect(parsed.superseded_by).toBe('unpin-canary');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.text).toBe('unpin parity canary');
    expect(parsed.type).toBe('preference');
    // v0 leak check.
    expect(parsed.st).toBeUndefined();
    expect(parsed.sup).toBeUndefined();
  });

  test('v0 source → v1.1 upgrade matches plugin upgrade rules', async () => {
    // v0 short-key with category "rule" → v1 type "directive".
    const v0Src = JSON.stringify({
      t: 'always use snake_case for database column names',
      c: 'rule',
      cf: 0.9,
      i: 8,
      sa: 'openclaw-plugin',
      ea: '2026-04-19T10:00:00.000Z',
    });

    let capturedPlaintext: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) { return makeFact(id, v0Src); },
      encryptBlob(plaintext: string) {
        capturedPlaintext = plaintext;
        return Buffer.from(plaintext, 'utf-8').toString('hex');
      },
    });
    await executePinOperation('v0-upgrade', 'pinned', deps);

    const parsed = JSON.parse(capturedPlaintext!);
    // Per spec §migration-from-v0: rule → directive.
    expect(parsed.type).toBe('directive');
    expect(parsed.text).toBe('always use snake_case for database column names');
    expect(parsed.pin_status).toBe('pinned');
    expect(parsed.schema_version).toBe('1.0');
    expect(parsed.superseded_by).toBe('v0-upgrade');
  });
});
