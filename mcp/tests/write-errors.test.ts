/**
 * @jest-environment node
 *
 * Issue #711: at the monthly write cap the relay answers 403 quota_exceeded.
 * Every write path must surface the standard quota response; any other write
 * failure must be a single line with no request dump (callData, signatures).
 */
import {
  isQuotaExceededError,
  rethrowIfQuota,
  shortErrorMessage,
} from '../src/tools/write-errors';
import { executePinOperation } from '../src/tools/pin';
import { executeRetype } from '../src/tools/retype';
import { buildV1ClaimBlob } from '../src/claims-helper';
import { createCallToolHandler, type DispatchDeps } from '../src/dispatch';
import type { ToolResponse } from '../src/tools/types';

const HEX = '0x' + 'ab'.repeat(300);

/** Mimics viem's UserOperationExecutionError for a relay 403. */
function viemError(details: string): Error {
  const e = new Error(
    [
      'An error occurred while executing user operation: HTTP request failed.',
      '',
      'Request Arguments:',
      `  callData: ${HEX}`,
      `  signature: ${HEX}`,
      '  nonce: 12',
      '',
      `Details: ${details}`,
    ].join('\n'),
  ) as Error & { shortMessage?: string; details?: string };
  e.shortMessage = 'An error occurred while executing user operation: HTTP request failed.';
  e.details = details;
  return e;
}

describe('write-errors helpers', () => {
  test('quota_exceeded in the tail of a viem dump is detected', () => {
    expect(isQuotaExceededError(viemError('"quota_exceeded"'))).toBe(true);
  });
  test('quota detected through the cause chain', () => {
    const outer = new Error('wrapped');
    (outer as Error & { cause?: unknown }).cause = viemError('{"error":"quota_exceeded"}');
    expect(isQuotaExceededError(outer)).toBe(true);
  });
  test('other errors are not quota errors', () => {
    expect(isQuotaExceededError(viemError('"insufficient funds"'))).toBe(false);
    expect(isQuotaExceededError('quota_exceeded')).toBe(false);
  });
  test('rethrowIfQuota throws only for quota errors', () => {
    expect(() => rethrowIfQuota(viemError('"quota_exceeded"'))).toThrow();
    expect(() => rethrowIfQuota(viemError('"nope"'))).not.toThrow();
  });
  test('shortErrorMessage uses shortMessage, one line, no hex', () => {
    const out = shortErrorMessage(viemError('"nope"'));
    expect(out).toBe('An error occurred while executing user operation: HTTP request failed.');
    expect(out).not.toContain('\n');
    expect(out).not.toMatch(/0x[0-9a-f]{20,}/i);
  });
  test('shortErrorMessage falls back to first line of message', () => {
    expect(shortErrorMessage(new Error('first line\nsecond line 0x' + 'ab'.repeat(50)))).toBe('first line');
    expect(shortErrorMessage('plain')).toBe('plain');
  });
});

function opDeps(submitError: Error) {
  const blob = buildV1ClaimBlob({ text: 'always check nil first', type: 'claim', source: 'user', id: 'old-id' });
  return {
    owner: '0x1234567890abcdef1234567890abcdef12345678',
    sourceAgent: 'mcp-server',
    async fetchFactById(id: string) {
      return { id, encryptedBlob: blob, encryptedEmbedding: null, decayScore: '0.8', timestamp: '1700000000', isActive: true };
    },
    decryptBlob: (hex: string) => hex,
    encryptBlob: (pt: string) => Buffer.from(pt, 'utf-8').toString('hex'),
    async submitBatch(): Promise<{ txHash: string; success: boolean }> { throw submitError; },
    async generateIndices() {
      return { blindIndices: ['t1', 't2'], encryptedEmbedding: 'enc' };
    },
  };
}

describe('pin / retype submission failures', () => {
  test('retype: quota error is rethrown for the dispatch guard', async () => {
    await expect(executeRetype('old-id', 'directive', opDeps(viemError('"quota_exceeded"')) as never))
      .rejects.toMatchObject({ details: '"quota_exceeded"' });
  });
  test('pin: quota error is rethrown for the dispatch guard', async () => {
    await expect(executePinOperation('old-id', 'pinned', opDeps(viemError('"quota_exceeded"')) as never))
      .rejects.toMatchObject({ details: '"quota_exceeded"' });
  });
  test('retype: non-quota failure is one line, no hex', async () => {
    const res = await executeRetype('old-id', 'directive', opDeps(viemError('"boom"')) as never);
    expect(res.success).toBe(false);
    expect(res.error).not.toContain('\n');
    expect(res.error).not.toMatch(/0x[0-9a-f]{20,}/i);
  });
  test('pin: non-quota failure is one line, no hex', async () => {
    const res = await executePinOperation('old-id', 'pinned', opDeps(viemError('"boom"')) as never);
    expect(res.success).toBe(false);
    expect(res.error).not.toContain('\n');
    expect(res.error).not.toMatch(/0x[0-9a-f]{20,}/i);
  });
});

describe('dispatch guard turns a rethrown quota error into the quota response', () => {
  function deps(handler: () => Promise<ToolResponse>): DispatchDeps {
    return {
      getMode: () => 'subgraph',
      handleSupport: async () => ({ content: [] }),
      handlePair: async () => ({ content: [] }),
      common: {},
      resolveBundle: async () => ({ totalreclaw_remember: handler }),
      isQuotaExceededError,
      quotaExceededResponse: () => ({
        content: [{ type: 'text', text: JSON.stringify({ error: 'quota_exceeded' }) }],
        isError: true,
      }),
      isAuthError: () => false,
      authHintResponse: () => ({ content: [], isError: true }),
      onMutate: () => {},
    };
  }

  test('403 quota_exceeded from the bundler gives isError quota response', async () => {
    const call = createCallToolHandler(deps(async () => { throw viemError('"quota_exceeded"'); }));
    const res = await call('totalreclaw_remember', {});
    expect(res.isError).toBe(true);
    expect(JSON.parse(res.content[0].text).error).toBe('quota_exceeded');
  });

  test('another viem error gives a one-line error with no hex blobs', async () => {
    const call = createCallToolHandler(deps(async () => { throw viemError('"boom"'); }));
    const res = await call('totalreclaw_remember', {});
    expect(res.isError).toBe(true);
    const msg = JSON.parse(res.content[0].text).error as string;
    expect(msg).not.toBe('quota_exceeded');
    expect(msg).not.toContain('\n');
    expect(msg).not.toMatch(/0x[0-9a-f]{20,}/i);
  });
});

describe('quota and tier copy (Stripe is the source of truth: Free 250, Pro 3,000)', () => {
  test('no source file still says Pro is 1,500', () => {
    const { execSync } = require('node:child_process') as typeof import('node:child_process');
    const out = execSync('grep -rnE "1,500|1500" src README.md SKILL.md || true', { cwd: __dirname + '/..' }).toString();
    expect(out).toBe('');
  });
});

describe('set_scope triggers (#712)', () => {
  const { setScopeToolDefinition } = require('../src/tools/set-scope');
  const { SERVER_INSTRUCTIONS } = require('../src/prompts');
  test.each(['file that under', 'put that in my work/personal memories', 'categorize as', "that\\'s a work thing".replace('\\', '')])(
    'description carries trigger %s',
    (phrase: string) => {
      expect(setScopeToolDefinition.description).toContain(phrase);
    },
  );
  test('description stays within 500 chars', () => {
    expect(setScopeToolDefinition.description.length).toBeLessThanOrEqual(500);
  });
  test('server instructions tell the agent to use set_scope for filing', () => {
    expect(SERVER_INSTRUCTIONS).toMatch(/file that under X[\s\S]*totalreclaw_set_scope/);
  });
});

describe('#702 contract gaps', () => {
  const fs = require('node:fs') as typeof import('node:fs');
  const path = require('node:path') as typeof import('node:path');
  const { debriefToolDefinition } = require('../src/tools/debrief');
  const { forgetToolDefinition } = require('../src/tools/forget');
  const { exportToolDefinition } = require('../src/tools/export');
  const { importToolDefinition } = require('../src/tools/import');
  const { importFromToolDefinition } = require('../src/tools/import-from');
  const { importBatchToolDefinition } = require('../src/tools/import-batch');
  const { consolidateToolDefinition } = require('../src/tools/consolidate');
  const { SERVER_VERSION } = require('../src/server-setup');
  const { TOOL_MANIFEST } = require('../src/dispatch');
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));

  test('serverInfo.version is the package version', () => {
    expect(SERVER_VERSION).toBe(pkg.version);
  });

  test('debrief schema is flat: no top-level oneOf/anyOf/allOf, optional crystal + facts', () => {
    const schema = debriefToolDefinition.inputSchema as Record<string, unknown>;
    expect(schema.oneOf).toBeUndefined();
    expect(schema.anyOf).toBeUndefined();
    expect(schema.allOf).toBeUndefined();
    expect(Object.keys(schema.properties as object).sort()).toEqual(['crystal', 'facts']);
    expect(schema.required).toBeUndefined();
  });

  test('every manifest tool schema is a top-level object without combinators', () => {
    for (const t of TOOL_MANIFEST as Array<{ name: string; inputSchema: Record<string, unknown> }>) {
      expect(t.inputSchema.type).toBe('object');
      expect(t.inputSchema.oneOf).toBeUndefined();
      expect(t.inputSchema.anyOf).toBeUndefined();
    }
  });

  test.each([
    ['export', exportToolDefinition],
    ['import', importToolDefinition],
    ['import_from', importFromToolDefinition],
    ['import_batch', importBatchToolDefinition],
    ['consolidate', consolidateToolDefinition],
  ])('%s says it is self-hosted only', (_n: string, def: { description: string }) => {
    expect(def.description).toMatch(/self-hosted only/i);
    expect(def.description.length).toBeLessThanOrEqual(500);
  });

  test('forget description names memory_id, the recall fact_id and the managed-mode limit', () => {
    const d: string = forgetToolDefinition.description;
    expect(d).toMatch(/memory_id/);
    expect(d).toMatch(/fact_id from a recall result/);
    expect(d).toMatch(/managed service needs an id/);
    expect(d.length).toBeLessThanOrEqual(500);
  });

  test('managed bundle registers import_batch and forget accepts memory_id (source contract)', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
    const sub = src.slice(src.indexOf('function subgraphBundle'), src.indexOf('function httpBundle'));
    expect(sub).toContain('totalreclaw_import_batch');
    const fg = src.slice(src.indexOf('async function handleForgetSubgraph'));
    expect(fg.slice(0, 600)).toContain('pickMemoryId(input)');
  });
});

describe('restart guidance after pairing', () => {
  const { pairInstructions } = require('../src/tools/pair');
  const { SERVER_INSTRUCTIONS } = require('../src/prompts');
  test.each(['generate', 'import'])('pair completion text (%s) asks for a full quit/reopen or new session', (mode: string) => {
    const text = pairInstructions(mode, 10).join('\n');
    expect(text).toMatch(/ask the user to fully quit and reopen/);
    expect(text).toMatch(/start a new session/);
    expect(text).toMatch(/only at startup/);
  });
  test('server instructions say the same', () => {
    expect(SERVER_INSTRUCTIONS).toMatch(/After Pairing[\s\S]*fully quit and reopen[\s\S]*new session/);
  });
});
