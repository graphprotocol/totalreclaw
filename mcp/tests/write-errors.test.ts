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
