/**
 * Tests for `totalreclaw_retype` — change the v1 type of an existing memory.
 */

import {
  retypeToolDefinition,
  handleRetype,
  handleRetypeWithDeps,
  executeRetype,
  extractV1Fields,
  validateRetypeArgs,
  type MetadataOpDeps,
} from '../src/tools/retype';
import { buildV1ClaimBlob } from '../src/claims-helper';

// ── Tool definition ──────────────────────────────────────────────────────────

describe('retypeToolDefinition', () => {
  test('name is totalreclaw_retype', () => {
    expect(retypeToolDefinition.name).toBe('totalreclaw_retype');
  });

  test('has a description', () => {
    expect(typeof retypeToolDefinition.description).toBe('string');
    expect(retypeToolDefinition.description.length).toBeGreaterThan(20);
  });

  test('requires memory_id + new_type', () => {
    expect(retypeToolDefinition.inputSchema.required).toContain('memory_id');
    expect(retypeToolDefinition.inputSchema.required).toContain('new_type');
  });

  test('new_type enum matches v1 spec (6 values)', () => {
    const enumValues = retypeToolDefinition.inputSchema.properties.new_type.enum;
    expect(enumValues).toEqual(
      expect.arrayContaining(['claim', 'preference', 'directive', 'commitment', 'episode', 'summary']),
    );
    expect(enumValues.length).toBe(6);
  });

  test('annotated idempotent + non-destructive', () => {
    expect(retypeToolDefinition.annotations.idempotentHint).toBe(true);
    expect(retypeToolDefinition.annotations.destructiveHint).toBe(false);
  });
});

// ── Input validation ────────────────────────────────────────────────────────

describe('validateRetypeArgs', () => {
  test('rejects non-object input', () => {
    const r1 = validateRetypeArgs(null);
    expect(r1.ok).toBe(false);
    const r2 = validateRetypeArgs('string');
    expect(r2.ok).toBe(false);
  });

  test('rejects missing memory_id', () => {
    const r = validateRetypeArgs({ new_type: 'claim' });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/memory_id/);
  });

  test('rejects empty memory_id', () => {
    const r = validateRetypeArgs({ memory_id: '   ', new_type: 'claim' });
    expect(r.ok).toBe(false);
  });

  test('rejects invalid new_type', () => {
    const r = validateRetypeArgs({ memory_id: 'abc', new_type: 'fact' }); // legacy
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/new_type/);
  });

  test('accepts valid retype args', () => {
    const r = validateRetypeArgs({ memory_id: 'abc-123', new_type: 'directive' });
    expect(r.ok).toBe(true);
    expect(r.memoryId).toBe('abc-123');
    expect(r.newType).toBe('directive');
  });

  test('trims memory_id', () => {
    const r = validateRetypeArgs({ memory_id: '  spaced-id  ', new_type: 'preference' });
    expect(r.ok).toBe(true);
    expect(r.memoryId).toBe('spaced-id');
  });
});

// ── extractV1Fields — parsing legacy/v1 blobs ───────────────────────────────

describe('extractV1Fields', () => {
  test('parses a v1 canonical blob', () => {
    const v1Json = buildV1ClaimBlob({
      text: 'prefers coffee',
      type: 'preference',
      source: 'user',
      id: 'fixed-id',
      createdAt: '2026-01-01T00:00:00Z',
    });
    const out = extractV1Fields(v1Json);
    expect(out.text).toBe('prefers coffee');
    expect(out.type).toBe('preference');
    expect(out.source).toBe('user');
    expect(out.createdAt).toBe('2026-01-01T00:00:00Z');
  });

  test('v0 short-key canonical maps to v1 type', () => {
    const v0Blob = JSON.stringify({
      t: 'always check d.get(errors)',
      c: 'rule',
      cf: 0.9,
      i: 8,
      sa: 'mcp-server',
      ea: '2026-03-01T00:00:00Z',
    });
    const out = extractV1Fields(v0Blob);
    expect(out.text).toBe('always check d.get(errors)');
    expect(out.type).toBe('directive'); // `rule` → directive in v1
    expect(out.source).toBe('user-inferred');
    expect(out.importance).toBe(8);
  });

  test('plugin-legacy {text, metadata} maps correctly', () => {
    const legacyBlob = JSON.stringify({
      text: 'lives in Lisbon',
      metadata: { type: 'fact', importance: 0.8 },
    });
    const out = extractV1Fields(legacyBlob);
    expect(out.text).toBe('lives in Lisbon');
    expect(out.type).toBe('claim'); // fact → claim
    expect(out.importance).toBe(8);
  });

  test('raw text falls back to claim', () => {
    const out = extractV1Fields('not json just raw');
    expect(out.text).toBe('not json just raw');
    expect(out.type).toBe('claim');
  });
});

// ── executeRetype — full supersede flow ─────────────────────────────────────

function makeDeps(overrides: Partial<MetadataOpDeps> = {}): MetadataOpDeps & { _submitted: Buffer[][] } {
  const submitted: Buffer[][] = [];
  const base: MetadataOpDeps = {
    owner: '0x1234567890abcdef1234567890abcdef12345678',
    sourceAgent: 'mcp-server',
    async fetchFactById() {
      return null;
    },
    decryptBlob(b) {
      return b;
    },
    encryptBlob(pt) {
      return Buffer.from(pt, 'utf-8').toString('hex');
    },
    async submitBatch(payloads) {
      submitted.push(payloads);
      return { txHash: '0xdeadbeef', success: true };
    },
    async generateIndices() {
      return { blindIndices: ['mock-1', 'mock-2'], encryptedEmbedding: 'mock-emb' };
    },
  };
  const merged = { ...base, ...overrides } as MetadataOpDeps & { _submitted: Buffer[][] };
  merged._submitted = submitted;
  return merged;
}

describe('executeRetype', () => {
  test('returns not_found when fact is missing', async () => {
    const deps = makeDeps();
    const res = await executeRetype('missing-id', 'directive', deps);
    expect(res.success).toBe(false);
    expect(res.error).toMatch(/not found/i);
  });

  test('retype from claim → directive emits tombstone + new v1 blob with superseded_by', async () => {
    const existing = buildV1ClaimBlob({
      text: 'always check nil first',
      type: 'claim',
      source: 'user',
      id: 'old-id',
    });
    let captured: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: existing,
          encryptedEmbedding: null,
          decayScore: '0.8',
          timestamp: '1700000000',
          isActive: true,
        };
      },
      encryptBlob(pt) {
        captured = pt;
        return Buffer.from(pt).toString('hex');
      },
    });

    const res = await executeRetype('old-id', 'directive', deps);

    expect(res.success).toBe(true);
    expect(res.previous_value).toBe('claim');
    expect(res.new_value).toBe('directive');
    expect(res.tx_hash).toBe('0xdeadbeef');
    expect(res.new_memory_id).toBeDefined();
    expect(res.new_memory_id).not.toBe('old-id');
    // Two payloads: tombstone + new
    expect(deps._submitted).toHaveLength(1);
    expect(deps._submitted[0]).toHaveLength(2);

    expect(captured).not.toBeNull();
    const parsed = JSON.parse(captured!);
    expect(parsed.type).toBe('directive');
    // schema_version is omitted on serialize when it equals the default (core's
    // skip_serializing_if). That's the v1 canonical shape — presence of text+type
    // with a v1 enum value is sufficient to distinguish.
    expect(
      parsed.schema_version === '1.0' || parsed.schema_version === undefined,
    ).toBe(true);
    expect(parsed.superseded_by).toBe('old-id');
  });

  test('retype is idempotent when type already matches', async () => {
    const existing = buildV1ClaimBlob({
      text: 'already directive',
      type: 'directive',
      source: 'user',
    });
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: existing,
          encryptedEmbedding: null,
          decayScore: '0.8',
          timestamp: '1700000000',
          isActive: true,
        };
      },
    });

    const res = await executeRetype('id-1', 'directive', deps);
    expect(res.success).toBe(true);
    expect(res.idempotent).toBe(true);
    expect(deps._submitted).toHaveLength(0); // no chain write
  });

  test('retype on a v0 legacy blob upgrades to v1', async () => {
    const v0Blob = JSON.stringify({
      text: 'ships v2 next week',
      metadata: { type: 'goal', importance: 0.7 },
    });
    let captured: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: v0Blob,
          encryptedEmbedding: null,
          decayScore: '0.7',
          timestamp: '1700000000',
          isActive: true,
        };
      },
      encryptBlob(pt) {
        captured = pt;
        return Buffer.from(pt).toString('hex');
      },
    });

    const res = await executeRetype('legacy-goal', 'commitment', deps);

    // Previous is already 'commitment' (goal → commitment mapping in extractV1Fields)
    expect(res.success).toBe(true);
    expect(res.idempotent).toBe(true);

    // Run a retype that ACTUALLY changes the type
    const res2 = await executeRetype('legacy-goal', 'claim', deps);
    expect(res2.success).toBe(true);
    expect(res2.previous_value).toBe('commitment');
    expect(res2.new_value).toBe('claim');
    expect(captured).not.toBeNull();
    const parsed = JSON.parse(captured!);
    // schema_version is omitted on serialize when it equals the default (core's
    // skip_serializing_if). That's the v1 canonical shape — presence of text+type
    // with a v1 enum value is sufficient to distinguish.
    expect(
      parsed.schema_version === '1.0' || parsed.schema_version === undefined,
    ).toBe(true);
    expect(parsed.type).toBe('claim');
  });

  test('returns error when batch submission fails', async () => {
    const existing = buildV1ClaimBlob({
      text: 'test',
      type: 'claim',
      source: 'user',
    });
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: existing,
          encryptedEmbedding: null,
          decayScore: '0.8',
          timestamp: '1700000000',
          isActive: true,
        };
      },
      async submitBatch() {
        return { txHash: '0xbad', success: false };
      },
    });

    const res = await executeRetype('id', 'directive', deps);
    expect(res.success).toBe(false);
    expect(res.error).toMatch(/submission failed/i);
    expect(res.tx_hash).toBe('0xbad');
  });
});

// ── handleRetype (HTTP mode) ────────────────────────────────────────────────

describe('handleRetype (HTTP mode)', () => {
  test('returns a not-supported error for self-hosted', async () => {
    const out = await handleRetype({ memory_id: 'abc', new_type: 'claim' });
    const body = JSON.parse(out.content[0].text);
    expect(body.success).toBe(false);
    expect(body.error).toMatch(/managed service/i);
  });

  test('validates args before dispatching', async () => {
    const out = await handleRetype({ memory_id: 'abc', new_type: 'fact' }); // invalid
    const body = JSON.parse(out.content[0].text);
    expect(body.success).toBe(false);
    expect(body.error).toMatch(/new_type/);
  });
});

describe('handleRetypeWithDeps (subgraph mode)', () => {
  test('returns success envelope on valid input', async () => {
    const blob = buildV1ClaimBlob({
      text: 'test claim',
      type: 'claim',
      source: 'user',
    });
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: blob,
          encryptedEmbedding: null,
          decayScore: '0.8',
          timestamp: '1700000000',
          isActive: true,
        };
      },
    });
    const out = await handleRetypeWithDeps({ memory_id: 'old', new_type: 'episode' }, deps);
    const body = JSON.parse(out.content[0].text);
    expect(body.success).toBe(true);
    expect(body.previous_value).toBe('claim');
    expect(body.new_value).toBe('episode');
  });
});
