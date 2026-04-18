/**
 * Tests for `totalreclaw_set_scope` — change the v1 scope of an existing memory.
 */

import {
  setScopeToolDefinition,
  handleSetScope,
  handleSetScopeWithDeps,
  executeSetScope,
  validateSetScopeArgs,
} from '../src/tools/set-scope';
import type { MetadataOpDeps } from '../src/tools/retype';
import { buildV1ClaimBlob } from '../src/claims-helper';

describe('setScopeToolDefinition', () => {
  test('name', () => {
    expect(setScopeToolDefinition.name).toBe('totalreclaw_set_scope');
  });

  test('scope enum matches v1 spec (8 values)', () => {
    const enumValues = setScopeToolDefinition.inputSchema.properties.scope.enum;
    expect(enumValues).toEqual(
      expect.arrayContaining([
        'work',
        'personal',
        'health',
        'family',
        'creative',
        'finance',
        'misc',
        'unspecified',
      ]),
    );
    expect(enumValues.length).toBe(8);
  });

  test('requires memory_id + scope', () => {
    expect(setScopeToolDefinition.inputSchema.required).toContain('memory_id');
    expect(setScopeToolDefinition.inputSchema.required).toContain('scope');
  });
});

describe('validateSetScopeArgs', () => {
  test('rejects invalid scope', () => {
    const r = validateSetScopeArgs({ memory_id: 'abc', scope: 'travel' });
    expect(r.ok).toBe(false);
  });

  test('accepts valid input', () => {
    const r = validateSetScopeArgs({ memory_id: 'abc', scope: 'work' });
    expect(r.ok).toBe(true);
    expect(r.memoryId).toBe('abc');
    expect(r.scope).toBe('work');
  });

  test('rejects missing scope', () => {
    const r = validateSetScopeArgs({ memory_id: 'abc' });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/scope/);
  });
});

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
      return { txHash: '0xabc123', success: true };
    },
    async generateIndices() {
      return { blindIndices: ['m1'], encryptedEmbedding: 'e1' };
    },
  };
  const merged = { ...base, ...overrides } as MetadataOpDeps & { _submitted: Buffer[][] };
  merged._submitted = submitted;
  return merged;
}

describe('executeSetScope', () => {
  test('set scope on a v1 claim with no scope emits a new blob with scope=work', async () => {
    const blob = buildV1ClaimBlob({
      text: 'uses Postgres',
      type: 'claim',
      source: 'user',
    });
    let captured: string | null = null;
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
      encryptBlob(pt) {
        captured = pt;
        return Buffer.from(pt).toString('hex');
      },
    });

    const res = await executeSetScope('old-id', 'work', deps);
    expect(res.success).toBe(true);
    expect(res.previous_value).toBe('unspecified');
    expect(res.new_value).toBe('work');
    expect(res.new_memory_id).toBeDefined();
    expect(deps._submitted[0]).toHaveLength(2);

    const parsed = JSON.parse(captured!);
    expect(parsed.scope).toBe('work');
    // schema_version omitted when default (per core skip_serializing_if).
    expect(
      parsed.schema_version === '1.0' || parsed.schema_version === undefined,
    ).toBe(true);
    expect(parsed.superseded_by).toBe('old-id');
  });

  test('idempotent when scope already matches', async () => {
    const blob = buildV1ClaimBlob({
      text: 'already personal',
      type: 'claim',
      source: 'user',
      scope: 'personal',
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
    const res = await executeSetScope('id', 'personal', deps);
    expect(res.success).toBe(true);
    expect(res.idempotent).toBe(true);
    expect(deps._submitted).toHaveLength(0);
  });

  test('set scope on legacy v0 blob upgrades to v1', async () => {
    const v0 = JSON.stringify({ text: 'legacy', metadata: { type: 'fact', importance: 0.6 } });
    let captured: string | null = null;
    const deps = makeDeps({
      async fetchFactById(id) {
        return {
          id,
          encryptedBlob: v0,
          encryptedEmbedding: null,
          decayScore: '0.6',
          timestamp: '1700000000',
          isActive: true,
        };
      },
      encryptBlob(pt) {
        captured = pt;
        return Buffer.from(pt).toString('hex');
      },
    });

    const res = await executeSetScope('legacy-id', 'finance', deps);
    expect(res.success).toBe(true);

    const parsed = JSON.parse(captured!);
    // schema_version omitted when default (per core skip_serializing_if).
    expect(
      parsed.schema_version === '1.0' || parsed.schema_version === undefined,
    ).toBe(true);
    expect(parsed.scope).toBe('finance');
    expect(parsed.type).toBe('claim'); // mapped from legacy 'fact'
  });

  test('returns fact_not_found when missing', async () => {
    const deps = makeDeps();
    const res = await executeSetScope('missing', 'work', deps);
    expect(res.success).toBe(false);
    expect(res.error).toMatch(/not found/i);
  });
});

describe('handleSetScope (HTTP mode)', () => {
  test('returns managed-service-only error', async () => {
    const out = await handleSetScope({ memory_id: 'a', scope: 'work' });
    const body = JSON.parse(out.content[0].text);
    expect(body.success).toBe(false);
    expect(body.error).toMatch(/managed service/i);
  });
});

describe('handleSetScopeWithDeps', () => {
  test('validates before dispatching', async () => {
    const deps = makeDeps();
    const out = await handleSetScopeWithDeps({ memory_id: '', scope: 'work' }, deps);
    const body = JSON.parse(out.content[0].text);
    expect(body.success).toBe(false);
  });
});
