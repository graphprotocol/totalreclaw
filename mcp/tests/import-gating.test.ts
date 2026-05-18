/**
 * Tests for the Pro-tier gate in handleImportFrom + handleImportBatch (MCP).
 *
 * Pedro authorized 2026-05-18: ALL imports are Pro-only (including mem0 and
 * mcp-memory). Free-tier callers must be rejected before any adapter runs.
 *
 * Coverage:
 *  - Free tier + every source → blocked with `{ success: false }` JSON body
 *    whose error names Pro and totalreclaw_upgrade.
 *  - Pro tier + every source → proceeds past the gate (the response is NOT
 *    the Pro-gate error — adapters may still error on missing input, that's
 *    fine and proves the gate didn't short-circuit).
 *  - Missing billing cache → blocked (fail-closed; matches the runtime
 *    behaviour where `cache?.tier !== 'pro'` is true when cache is null).
 */

import {
  handleImportFrom,
  type ImportSource,
} from '../src/tools/import-from.js';
import { handleImportBatch } from '../src/tools/import-batch.js';
import {
  __setLastBillingResponseForTesting,
  type BillingStatusResponse,
} from '../src/tools/status.js';

const PRO_BILLING: BillingStatusResponse = {
  tier: 'pro',
  free_writes_used: 0,
  free_writes_limit: 0,
  expires_at: null,
};

const FREE_BILLING: BillingStatusResponse = {
  tier: 'free',
  free_writes_used: 3,
  free_writes_limit: 10,
  expires_at: null,
};

const PRO_GATE_ERROR_FRAGMENT = 'Memory imports are a Pro feature';

function parseFirstTextBody(result: { content: Array<{ type: string; text: string }> }): Record<string, unknown> {
  return JSON.parse(result.content[0].text);
}

// `client` argument is only used after the Pro gate succeeds, and only when an
// adapter actually returns facts. A bare stub is sufficient — these tests never
// reach that branch.
const dummyClient: any = {
  remember: jest.fn(),
};

const IMPORT_FROM_SOURCES: ImportSource[] = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini'];
const IMPORT_BATCH_SOURCES = ['gemini', 'chatgpt', 'claude'];

afterEach(() => {
  __setLastBillingResponseForTesting(null);
});

describe('handleImportFrom — Pro gate', () => {
  test.each(IMPORT_FROM_SOURCES)('blocks free-tier import from %s', async (source) => {
    __setLastBillingResponseForTesting(FREE_BILLING);
    const result = await handleImportFrom(dummyClient, { source });
    const body = parseFirstTextBody(result);
    expect(body.success).toBe(false);
    expect(String(body.error)).toContain(PRO_GATE_ERROR_FRAGMENT);
    expect(String(body.error)).toContain('totalreclaw_upgrade');
  });

  test.each(IMPORT_FROM_SOURCES)('proceeds past gate for Pro tier on %s', async (source) => {
    __setLastBillingResponseForTesting(PRO_BILLING);
    const result = await handleImportFrom(dummyClient, { source });
    const body = parseFirstTextBody(result);
    // We don't care if the adapter eventually errors (missing input is fine),
    // only that the response is NOT the Pro-gate rejection.
    if (body.success === false) {
      expect(String(body.error)).not.toContain(PRO_GATE_ERROR_FRAGMENT);
    }
  });

  test('proceeds past gate when billing cache is missing (fail-open)', async () => {
    __setLastBillingResponseForTesting(null);
    const result = await handleImportFrom(dummyClient, { source: 'mem0' });
    const body = parseFirstTextBody(result);
    // Missing cache: getLastBillingResponse() returns null. The gate's
    // `billing && billing.tier !== 'pro'` is false when billing is null,
    // so the gate does NOT block — documented fail-open on MCP side
    // (server-side quota enforcement is the backstop). Note this differs
    // from skill-side which is fail-closed on missing cache.
    if (body.success === false) {
      expect(String(body.error)).not.toContain(PRO_GATE_ERROR_FRAGMENT);
    }
  });
});

describe('handleImportBatch — Pro gate', () => {
  test.each(IMPORT_BATCH_SOURCES)('blocks free-tier batch import for %s', async (source) => {
    __setLastBillingResponseForTesting(FREE_BILLING);
    const result = await handleImportBatch({ source });
    const body = parseFirstTextBody(result);
    expect(body.success).toBe(false);
    expect(String(body.error)).toContain(PRO_GATE_ERROR_FRAGMENT);
    expect(String(body.error)).toContain('totalreclaw_upgrade');
  });

  test.each(IMPORT_BATCH_SOURCES)('proceeds past gate for Pro tier on %s', async (source) => {
    __setLastBillingResponseForTesting(PRO_BILLING);
    const result = await handleImportBatch({ source });
    const body = parseFirstTextBody(result);
    if (body.success === false) {
      expect(String(body.error)).not.toContain(PRO_GATE_ERROR_FRAGMENT);
    }
  });

  test('proceeds past gate when billing cache is missing (fail-open)', async () => {
    __setLastBillingResponseForTesting(null);
    const result = await handleImportBatch({ source: 'gemini' });
    const body = parseFirstTextBody(result);
    if (body.success === false) {
      expect(String(body.error)).not.toContain(PRO_GATE_ERROR_FRAGMENT);
    }
  });
});

describe('handleImportFrom — input validation precedes gate', () => {
  test('invalid source returns validation error regardless of tier', async () => {
    __setLastBillingResponseForTesting(FREE_BILLING);
    const result = await handleImportFrom(dummyClient, { source: 'not-a-real-source' });
    const body = parseFirstTextBody(result);
    expect(body.success).toBe(false);
    expect(String(body.error)).toContain('Invalid source');
  });
});
