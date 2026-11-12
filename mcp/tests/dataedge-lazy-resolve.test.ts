/**
 * #689 regression: a failed/timed-out startup billing lookup must never send
 * a write to a DataEdge from another environment.
 *
 * Before the fix, mnemonic mode made ONE 5 s billing call at startup and on a
 * timeout fell back to the PRODUCTION default DataEdge for the whole process.
 * A staging server on a cold relay therefore wrote to production (observed in
 * the mcp 3.5.1-rc.1 staging smoke, tx 0xd9297d84…) and recalled nothing.
 *
 * Now: the lookup is retried with backoff; if it still fails the state stays
 * unresolved; the first write re-runs the lookup and is refused
 * (`DataEdgeUnresolvedError`) if it can't resolve. These tests drive the
 * exported pieces `index.ts` composes (`writeConfigFor` =
 * ensureWriteChainResolved + buildSubgraphOverrides + getSubgraphConfig),
 * plus a static scan that every write site in index.ts goes through
 * `writeConfigFor`.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

import {
  buildSubgraphOverrides,
  ensureWriteChainResolved,
  fetchChainConfigWithRetry,
  DataEdgeUnresolvedError,
  type FetchedChainConfig,
  type WriteChainState,
} from '../src/subgraph/chain-config.js';
import { getSubgraphConfig, DEFAULT_DATA_EDGE_ADDRESS } from '../src/subgraph/store.js';

const STAGING_URL = 'https://api-staging.totalreclaw.xyz';
const STAGING_DATA_EDGE = '0xE7a4D2677B686e13775Ba9092631089e35F0BB91';
const PROD_DATA_EDGE = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca';
// Test-only fixture signing key (not a real account).
const FIXTURE_KEY_HEX = '11'.repeat(32);
const WALLET = '0x053851fa6014dcb3a295c772abc385231da58331';

const timeoutError = () => Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' });
const okResponse = (body: Record<string, unknown>) => ({ ok: true, status: 200, json: async () => body });

const noSleep = jest.fn(async (_ms: number) => {});

function fetchOpts(fetchImpl: jest.Mock, onBillingUnavailable?: (r: string) => void) {
  return {
    serverUrl: STAGING_URL,
    smartAccountAddress: WALLET,
    authKeyHex: 'ab'.repeat(32),
    fallbackChainId: 100,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    onBillingUnavailable,
  };
}

/** Mirror of index.ts `writeConfigFor` + a write that records where it went. */
async function writeLikeServer(
  state: WriteChainState,
  refetch: () => Promise<FetchedChainConfig>,
  submit: (dataEdge: string) => void,
) {
  await ensureWriteChainResolved(state, refetch, process.env.TOTALRECLAW_DATA_EDGE_ADDRESS, STAGING_URL);
  const cfg = getSubgraphConfig(
    buildSubgraphOverrides({
      relayUrl: STAGING_URL,
      ownerPrivateKeyHex: FIXTURE_KEY_HEX,
      authKeyHex: 'ab'.repeat(32),
      walletAddress: WALLET,
      chainId: state.chainId,
      dataEdgeAddress: state.dataEdgeAddress,
      chainResolved: state.chainResolved,
    }),
  );
  submit(cfg.dataEdgeAddress);
}

const savedEnv = process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
beforeEach(() => {
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  noSleep.mockClear();
});
afterAll(() => {
  if (savedEnv === undefined) delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  else process.env.TOTALRECLAW_DATA_EDGE_ADDRESS = savedEnv;
});

describe('fetchChainConfigWithRetry (#689)', () => {
  it('a cold relay: first attempt times out, the retry resolves the staging DataEdge', async () => {
    const fetchImpl = jest
      .fn()
      .mockRejectedValueOnce(timeoutError())
      .mockResolvedValueOnce(okResponse({ tier: 'free', chain_id: 100, data_edge_address: STAGING_DATA_EDGE }));
    const failures: string[] = [];
    const r = await fetchChainConfigWithRetry(fetchOpts(fetchImpl, (m) => failures.push(m)), {
      attempts: 2,
      timeoutMs: 8000,
      backoffMs: [1000],
      sleep: noSleep,
    });
    expect(r).toEqual({ chainId: 100, dataEdgeAddress: STAGING_DATA_EDGE, billingResolved: true });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(noSleep).toHaveBeenCalledWith(1000);
    expect(failures).toEqual(['billing fetch failed: The operation was aborted due to timeout (attempt 1/2)']);
  });

  it('every attempt fails: unresolved, no DataEdge, one failure report per attempt', async () => {
    const fetchImpl = jest.fn().mockRejectedValue(timeoutError());
    const failures: string[] = [];
    const r = await fetchChainConfigWithRetry(fetchOpts(fetchImpl, (m) => failures.push(m)), {
      attempts: 3,
      timeoutMs: 10,
      backoffMs: [1000, 3000],
      sleep: noSleep,
    });
    expect(r.billingResolved).toBe(false);
    expect(r.dataEdgeAddress).toBeUndefined();
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(noSleep.mock.calls.map((c) => c[0])).toEqual([1000, 3000]);
    expect(failures.map((f) => f.slice(-13))).toEqual(['(attempt 1/3)', '(attempt 2/3)', '(attempt 3/3)']);
  });

  it('a successful first attempt makes one call (unchanged production behaviour)', async () => {
    const fetchImpl = jest.fn().mockResolvedValue(okResponse({ tier: 'pro', chain_id: 100, data_edge_address: PROD_DATA_EDGE }));
    const r = await fetchChainConfigWithRetry(fetchOpts(fetchImpl), { attempts: 2, timeoutMs: 8000, backoffMs: [1000], sleep: noSleep });
    expect(r).toEqual({ chainId: 100, dataEdgeAddress: PROD_DATA_EDGE, billingResolved: true });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(noSleep).not.toHaveBeenCalled();
  });
});

describe('write path with an unresolved DataEdge (#689)', () => {
  it('startup timed out and the write-time lookup also fails: the write is refused, nothing is sent to the fallback address', async () => {
    const state: WriteChainState = { chainId: 100, dataEdgeAddress: undefined, chainResolved: false };
    const refetch = jest.fn(async (): Promise<FetchedChainConfig> => ({ chainId: 100, billingResolved: false }));
    const submit = jest.fn();
    await expect(writeLikeServer(state, refetch, submit)).rejects.toBeInstanceOf(DataEdgeUnresolvedError);
    expect(submit).not.toHaveBeenCalled();
    expect(state).toEqual({ chainId: 100, dataEdgeAddress: undefined, chainResolved: false });
  });

  it('the refusal message says nothing was stored and how to recover', async () => {
    const state: WriteChainState = { chainId: 100, chainResolved: false };
    await expect(
      ensureWriteChainResolved(state, async () => ({ chainId: 100, billingResolved: false }), undefined, STAGING_URL),
    ).rejects.toThrow(/was NOT sent\. Nothing was stored\..*TOTALRECLAW_DATA_EDGE_ADDRESS/);
  });

  it('startup timed out, the write-time lookup succeeds: the write goes to the staging DataEdge and the state stays resolved', async () => {
    const state: WriteChainState = { chainId: 100, dataEdgeAddress: undefined, chainResolved: false };
    const refetch = jest.fn(async (): Promise<FetchedChainConfig> => ({
      chainId: 100,
      dataEdgeAddress: STAGING_DATA_EDGE,
      billingResolved: true,
    }));
    const submit = jest.fn();
    await writeLikeServer(state, refetch, submit);
    expect(submit).toHaveBeenCalledWith(STAGING_DATA_EDGE);
    expect(submit).not.toHaveBeenCalledWith(DEFAULT_DATA_EDGE_ADDRESS);
    expect(state.chainResolved).toBe(true);
    // Second write: no new lookup.
    await writeLikeServer(state, refetch, submit);
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenLastCalledWith(STAGING_DATA_EDGE);
  });

  it('a failed write-time lookup is retried on the next write', async () => {
    const state: WriteChainState = { chainId: 100, chainResolved: false };
    const refetch = jest
      .fn<Promise<FetchedChainConfig>, []>()
      .mockResolvedValueOnce({ chainId: 100, billingResolved: false })
      .mockResolvedValueOnce({ chainId: 100, dataEdgeAddress: STAGING_DATA_EDGE, billingResolved: true });
    const submit = jest.fn();
    await expect(writeLikeServer(state, refetch, submit)).rejects.toBeInstanceOf(DataEdgeUnresolvedError);
    await writeLikeServer(state, refetch, submit);
    expect(submit.mock.calls).toEqual([[STAGING_DATA_EDGE]]);
  });

  it('concurrent writes share one in-flight lookup', async () => {
    const state: WriteChainState = { chainId: 100, chainResolved: false };
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const refetch = jest.fn(async (): Promise<FetchedChainConfig> => {
      await gate;
      return { chainId: 100, dataEdgeAddress: STAGING_DATA_EDGE, billingResolved: true };
    });
    const submit = jest.fn();
    const writes = [writeLikeServer(state, refetch, submit), writeLikeServer(state, refetch, submit)];
    release();
    await Promise.all(writes);
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(submit.mock.calls).toEqual([[STAGING_DATA_EDGE], [STAGING_DATA_EDGE]]);
  });

  it('an explicit TOTALRECLAW_DATA_EDGE_ADDRESS counts as resolved: no lookup, the override is used', async () => {
    process.env.TOTALRECLAW_DATA_EDGE_ADDRESS = STAGING_DATA_EDGE;
    const state: WriteChainState = { chainId: 100, chainResolved: false };
    const refetch = jest.fn(async (): Promise<FetchedChainConfig> => ({ chainId: 100, billingResolved: false }));
    const submit = jest.fn();
    await writeLikeServer(state, refetch, submit);
    expect(refetch).not.toHaveBeenCalled();
    expect(submit).toHaveBeenCalledWith(STAGING_DATA_EDGE);
  });

  it('buildSubgraphOverrides refuses an unresolved state on its own (defence in depth)', () => {
    expect(() =>
      buildSubgraphOverrides({
        relayUrl: STAGING_URL,
        ownerPrivateKeyHex: FIXTURE_KEY_HEX,
        authKeyHex: 'ab'.repeat(32),
        walletAddress: WALLET,
        chainId: 100,
        chainResolved: false,
      }),
    ).toThrow(DataEdgeUnresolvedError);
  });
});

describe('index.ts write sites (#689 static guard)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');

  it('every on-chain write config is built by writeConfigFor (which resolves first)', () => {
    const direct = src.match(/getSubgraphConfig\(stateWriteOverrides\(/g) ?? [];
    expect(direct).toHaveLength(1); // the one inside writeConfigFor itself
    const body = src.slice(src.indexOf('async function writeConfigFor('));
    expect(body.indexOf('ensureWriteChainResolved(')).toBeGreaterThan(-1);
    expect(body.indexOf('ensureWriteChainResolved(')).toBeLessThan(body.indexOf('getSubgraphConfig(stateWriteOverrides('));
    expect((src.match(/await writeConfigFor\(state\)/g) ?? []).length).toBeGreaterThanOrEqual(5);
  });

  it('the old "proceed on the production default" fallback is gone', () => {
    expect(src).not.toMatch(/proceeding without a confirmed DataEdge/);
  });
});
