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

describe('phrase-rail wording: point users to totalreclaw_pair, never to env/config', () => {
  const { notConfiguredResponse, setupRemovedResponse, createCallToolHandler: mk, TOOL_MANIFEST: manifest } = require('../src/dispatch');
  const { SERVER_INSTRUCTIONS, PAIR_TOOL_DESCRIPTION } = require('../src/prompts');
  const bad = /RECOVERY_PHRASE|host config|MCP host config|environment variable/i;

  test('server instructions and pair description carry no env/config phrase guidance', () => {
    expect(SERVER_INSTRUCTIONS).not.toMatch(bad);
    expect(PAIR_TOOL_DESCRIPTION).not.toMatch(bad);
    expect(SERVER_INSTRUCTIONS).toMatch(/Call totalreclaw_pair[\s\S]*link[\s\S]*PIN/);
    expect(SERVER_INSTRUCTIONS).toMatch(/in a browser|in the browser|there/);
  });

  test('not_configured and tool_removed envelopes name totalreclaw_pair', () => {
    for (const r of [notConfiguredResponse(), setupRemovedResponse()]) {
      const text = r.content[0].text as string;
      expect(text).toContain('totalreclaw_pair');
      expect(text).not.toMatch(bad);
    }
  });

  test('every tool except support/pair/setup returns a not_configured error naming totalreclaw_pair when unconfigured', async () => {
    const call = mk({
      getMode: () => 'unconfigured',
      handleSupport: async () => ({ content: [] }),
      handlePair: async () => ({ content: [] }),
      common: {},
      resolveBundle: async () => ({}),
      isQuotaExceededError: () => false,
      quotaExceededResponse: () => ({ content: [] }),
      isAuthError: () => false,
      authHintResponse: () => ({ content: [] }),
      onMutate: () => {},
    });
    const names = (manifest as Array<{ name: string }>).map((t) => t.name)
      .filter((n) => n !== 'totalreclaw_support' && n !== 'totalreclaw_pair');
    expect(names.length).toBeGreaterThan(10);
    for (const n of names) {
      const res = await call(n, {});
      expect(res.isError).toBe(true);
      expect(res.content[0].text).toContain('not_configured');
      expect(res.content[0].text).toContain('totalreclaw_pair');
    }
  });
});

describe('C8: ids documented as "from a prior recall result" exist in recall results', () => {
  const fs = require('node:fs') as typeof import('node:fs');
  const path = require('node:path');
  test('managed recall results carry memory_id alongside fact_id', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
    expect(src).toMatch(/fact_id: r\.id,\s*memory_id: r\.id,/);
  });
});

describe('full error goes to stderr, one line to the model (review finding 1)', () => {
  const { describeErrorFull, logWriteError } = require('../src/tools/write-errors');
  test('describeErrorFull keeps details and cause (e.g. AA25)', () => {
    const inner = new Error('inner cause AA25 invalid account nonce');
    const outer = viemError('"AA25 invalid account nonce"');
    (outer as Error & { cause?: unknown }).cause = inner;
    const full = describeErrorFull(outer);
    expect(full).toContain('AA25');
    expect(full).toContain('callData');
    expect(full).toContain('inner cause');
  });
  test('logWriteError writes the full text to stderr; tool message stays one line', () => {
    const spy = jest.spyOn(console, 'error').mockImplementation(() => {});
    const e = viemError('"AA25 invalid account nonce"');
    logWriteError('Batch submission failed', e);
    expect(spy.mock.calls[0][0]).toContain('AA25');
    expect(shortErrorMessage(e)).not.toContain('AA25');
    spy.mockRestore();
  });
});

describe('forget checks existence first (review finding 2)', () => {
  const { classifyForgetTarget, forgetNotFoundResponse } = require('../src/tools/forget');
  test('missing id => missing, inactive => inactive, active => exists', async () => {
    expect(await classifyForgetTarget(async () => null)).toBe('missing');
    expect(await classifyForgetTarget(async () => ({ isActive: false }))).toBe('inactive');
    expect(await classifyForgetTarget(async () => ({ isActive: true }))).toBe('exists');
  });
  test('lookup errors propagate (nothing is written)', async () => {
    await expect(classifyForgetTarget(async () => { throw new Error('relay down'); })).rejects.toThrow('relay down');
  });
  test('not-found response has deleted_count 0 and no tx_hash', () => {
    const body = JSON.parse(forgetNotFoundResponse('abc', 'missing').content[0].text);
    expect(body.deleted_count).toBe(0);
    expect(body.fact_ids).toEqual([]);
    expect(body.not_found).toBe(true);
    expect(body.tx_hash).toBeUndefined();
    expect(body.error).toMatch(/not found/);
  });
  test('managed forget runs the check before the tombstone is built (source order)', () => {
    const fs = require('node:fs'); const path = require('node:path');
    const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
    const f = src.slice(src.indexOf('async function handleForgetSubgraph'));
    expect(f.indexOf('classifyForgetTarget')).toBeGreaterThan(0);
    expect(f.indexOf('classifyForgetTarget')).toBeLessThan(f.indexOf('encodeFactProtobuf(tombstonePayload)'));
  });
});

describe('README leads with pairing (review finding 3)', () => {
  test('pairing precedes the legacy env-var note', () => {
    const fs = require('node:fs'); const path = require('node:path');
    const md = fs.readFileSync(path.join(__dirname, '..', 'README.md'), 'utf8');
    const pair = md.indexOf('totalreclaw_pair');
    const legacy = md.indexOf('Legacy: manual setup');
    const env = md.indexOf('TOTALRECLAW_RECOVERY_PHRASE');
    expect(pair).toBeGreaterThan(0);
    expect(pair).toBeLessThan(legacy);
    expect(legacy).toBeLessThan(env);
    expect(md).toMatch(/plain text in a config file/);
  });
});

describe('forget lookup tells exists / missing / inactive / failed apart (review, medium)', () => {
  const { fetchFactForForget } = require('../src/subgraph/search');
  const { classifyForgetTarget, forgetNotFoundResponse, forgetLookupFailedResponse } = require('../src/tools/forget');
  const OWNER = '0xAbC0000000000000000000000000000000000001';
  const realFetch = global.fetch;
  afterEach(() => { global.fetch = realFetch; });
  const mockFetch = (impl: () => Promise<unknown>) => { global.fetch = jest.fn(impl as never) as never; };
  const ok = (body: unknown) => async () => ({ ok: true, status: 200, json: async () => body });
  const run = () => classifyForgetTarget(() => fetchFactForForget(OWNER, 'id1', 'https://relay.invalid', 'aa'));

  test('(a) active fact of this owner => exists', async () => {
    mockFetch(ok({ data: { fact: { id: 'id1', owner: OWNER.toLowerCase(), isActive: true } } }));
    expect(await run()).toBe('exists');
  });
  test('(b) subgraph answers fact: null => missing', async () => {
    mockFetch(ok({ data: { fact: null } }));
    expect(await run()).toBe('missing');
  });
  test('fact owned by someone else => missing', async () => {
    mockFetch(ok({ data: { fact: { id: 'id1', owner: '0xdead', isActive: true } } }));
    expect(await run()).toBe('missing');
  });
  test('inactive fact is visible => inactive ("already forgotten")', async () => {
    mockFetch(ok({ data: { fact: { id: 'id1', owner: OWNER, isActive: false } } }));
    expect(await run()).toBe('inactive');
    const body = JSON.parse(forgetNotFoundResponse('id1', 'inactive').content[0].text);
    expect(body.error).toMatch(/already forgotten/);
    expect(body.deleted_count).toBe(0);
  });
  test.each([403, 429, 503])('(c) HTTP %i is a failed lookup, not "missing"', async (status) => {
    mockFetch(async () => ({ ok: false, status, json: async () => ({}) }));
    await expect(run()).rejects.toThrow(/HTTP/);
  });
  test('(c) network error is a failed lookup', async () => {
    mockFetch(async () => { throw new Error('ECONNRESET'); });
    await expect(run()).rejects.toThrow('ECONNRESET');
  });
  test('(c) GraphQL error / malformed answer is a failed lookup', async () => {
    mockFetch(ok({ errors: [{ message: 'boom' }] }));
    await expect(run()).rejects.toThrow(/GraphQL/);
    mockFetch(ok({}));
    await expect(run()).rejects.toThrow(/malformed/);
  });
  test('(c) failed-lookup response: isError, deleted_count 0, one line, no tombstone fields', () => {
    const r = forgetLookupFailedResponse();
    expect(r.isError).toBe(true);
    const body = JSON.parse(r.content[0].text);
    expect(body.deleted_count).toBe(0);
    expect(body.tx_hash).toBeUndefined();
    expect(body.error).not.toContain('\n');
    expect(body.error).toMatch(/nothing was deleted/);
    expect(body.error).toMatch(/Try again/);
  });
  test('not-found message carries the indexing-lag hint', () => {
    const body = JSON.parse(forgetNotFoundResponse('id1', 'missing').content[0].text);
    expect(body.error).toMatch(/wait about 20 seconds and retry/);
  });
  test('managed forget uses the error-surfacing lookup and returns the failure response', () => {
    const fs = require('node:fs'); const path = require('node:path');
    const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
    const f = src.slice(src.indexOf('async function handleForgetSubgraph'));
    expect(f.slice(0, 2500)).toContain('fetchFactForForget(');
    expect(f.slice(0, 2500)).toContain('forgetLookupFailedResponse()');
    expect(f.indexOf('forgetLookupFailedResponse()')).toBeLessThan(f.indexOf('encodeFactProtobuf(tombstonePayload)'));
  });
});
