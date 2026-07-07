/**
 * @jest-environment node
 *
 * Direct coverage for the single tool-dispatch table (`src/dispatch.ts`) that
 * replaced the two parallel per-mode `switch` statements in `index.ts`. The
 * entry point itself boots a stdio server + poller on import, so it stays
 * untested directly; the routing logic lives here behind injected handler
 * bundles, which is exactly what these tests exercise with fakes.
 */

import {
  TOOL_MANIFEST,
  createCallToolHandler,
  SUBGRAPH_POLICY,
  HTTP_POLICY,
  type DispatchDeps,
  type HandlerBundle,
  type ServerMode,
} from '../src/dispatch';
import type { ToolResponse } from '../src/tools/types';

// The manifest is mode-independent: ListTools advertises the same tools in
// both storage modes; gating happens at dispatch time, not in the manifest.
const EXPECTED_TOOL_NAMES = [
  'totalreclaw_remember',
  'totalreclaw_recall',
  'totalreclaw_forget',
  'totalreclaw_export',
  'totalreclaw_import',
  'totalreclaw_import_from',
  'totalreclaw_import_batch',
  'totalreclaw_consolidate',
  'totalreclaw_status',
  'totalreclaw_upgrade',
  'totalreclaw_debrief',
  'totalreclaw_support',
  'totalreclaw_account',
  'totalreclaw_pin',
  'totalreclaw_unpin',
  'totalreclaw_retype',
  'totalreclaw_set_scope',
  'totalreclaw_pair',
];

function ok(payload: Record<string, unknown>): ToolResponse {
  return { content: [{ type: 'text', text: JSON.stringify(payload) }] };
}

function parse(res: ToolResponse): Record<string, unknown> {
  return JSON.parse(res.content[0].text) as Record<string, unknown>;
}

class QuotaError extends Error {
  constructor() {
    super('QUOTA_EXCEEDED');
  }
}
class AuthError extends Error {
  constructor() {
    super('401 UNAUTHORIZED');
  }
}

interface HarnessOptions {
  mode?: ServerMode;
  common?: HandlerBundle;
  http?: HandlerBundle;
  subgraph?: HandlerBundle;
  /** Force resolveBundle to reject (simulates e.g. self-hosted client init failure). */
  bundleError?: Error;
}

interface Harness {
  dispatch: (name: string, args: unknown) => Promise<ToolResponse>;
  mutations: number;
  supportCalls: unknown[];
  pairCalls: unknown[];
}

function makeHarness(opts: HarnessOptions = {}): Harness {
  const state = { mutations: 0, supportCalls: [] as unknown[], pairCalls: [] as unknown[] };
  const deps: DispatchDeps = {
    getMode: () => opts.mode ?? 'subgraph',
    handleSupport: async (args) => {
      state.supportCalls.push(args);
      return ok({ tool: 'support' });
    },
    handlePair: async (args) => {
      state.pairCalls.push(args);
      return ok({ tool: 'pair' });
    },
    common: opts.common ?? {},
    resolveBundle: async (mode) => {
      if (opts.bundleError) throw opts.bundleError;
      return (mode === 'subgraph' ? opts.subgraph : opts.http) ?? {};
    },
    isQuotaExceededError: (e) => e instanceof QuotaError,
    quotaExceededResponse: () => ({ ...ok({ error: 'quota_exceeded' }), isError: true }),
    isAuthError: (e) => e instanceof AuthError,
    authHintResponse: () => ({ ...ok({ error: 'auth_hint' }), isError: true }),
    onMutate: () => {
      state.mutations += 1;
    },
  };
  return {
    dispatch: createCallToolHandler(deps),
    get mutations() {
      return state.mutations;
    },
    supportCalls: state.supportCalls,
    pairCalls: state.pairCalls,
  };
}

describe('dispatch: tool manifest', () => {
  it('advertises exactly the expected tool names', () => {
    const names = TOOL_MANIFEST.map((t) => t.name);
    expect(names).toEqual(EXPECTED_TOOL_NAMES);
  });

  it('has no duplicate tool names', () => {
    const names = TOOL_MANIFEST.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it('is identical across storage modes (gating is at dispatch time)', () => {
    // There is a single manifest constant; both modes serve it verbatim.
    expect(TOOL_MANIFEST.map((t) => t.name)).toEqual(EXPECTED_TOOL_NAMES);
  });
});

describe('dispatch: routing to handlers', () => {
  it('routes a known tool to its bound handler and returns the result', async () => {
    const h = makeHarness({
      mode: 'http',
      http: {
        totalreclaw_recall: async (args) => ok({ routed: 'recall', echoed: args }),
      },
    });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    const body = parse(res);
    expect(body.routed).toBe('recall');
    expect(body.echoed).toEqual({ query: 'x' });
  });

  it('routes the same tool name to different handlers per mode', async () => {
    const http = makeHarness({
      mode: 'http',
      http: { totalreclaw_export: async () => ok({ from: 'http' }) },
      subgraph: { totalreclaw_export: async () => ok({ from: 'subgraph' }) },
    });
    const sub = makeHarness({
      mode: 'subgraph',
      http: { totalreclaw_export: async () => ok({ from: 'http' }) },
      subgraph: { totalreclaw_export: async () => ok({ from: 'subgraph' }) },
    });
    expect(parse(await http.dispatch('totalreclaw_export', {})).from).toBe('http');
    expect(parse(await sub.dispatch('totalreclaw_export', {})).from).toBe('subgraph');
  });

  it('routes mode-independent common tools before resolving the mode bundle', async () => {
    // resolveBundle rejects, yet the common handler still answers — proving
    // status/upgrade/account never depend on the (possibly failing) client.
    const h = makeHarness({
      mode: 'http',
      bundleError: new Error('client init failed'),
      common: { totalreclaw_status: async () => ok({ tool: 'status' }) },
    });
    const res = await h.dispatch('totalreclaw_status', {});
    expect(parse(res).tool).toBe('status');
  });
});

describe('dispatch: unknown tool', () => {
  it('returns the standard unknown-tool error envelope', async () => {
    const h = makeHarness({ mode: 'http', http: {} });
    const res = await h.dispatch('totalreclaw_does_not_exist', {});
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('Unknown tool: totalreclaw_does_not_exist');
  });
});

describe('dispatch: mode gating', () => {
  it('serves support and pair in every mode, including unconfigured', async () => {
    for (const mode of ['unconfigured', 'http', 'subgraph'] as ServerMode[]) {
      const h = makeHarness({ mode });
      expect(parse(await h.dispatch('totalreclaw_support', { a: 1 })).tool).toBe('support');
      expect(parse(await h.dispatch('totalreclaw_pair', { b: 2 })).tool).toBe('pair');
    }
  });

  it('returns not-configured for non-support/pair tools when unconfigured', async () => {
    const h = makeHarness({ mode: 'unconfigured' });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('not_configured');
  });

  it('returns the removed-tool envelope for totalreclaw_setup in any mode', async () => {
    const h = makeHarness({ mode: 'subgraph' });
    const res = await h.dispatch('totalreclaw_setup', {});
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('tool_removed');
  });

  it('a tool present in one mode bundle but absent in the other is gated', async () => {
    // Mirrors the real wiring: totalreclaw_import_batch exists only in the HTTP
    // bundle, so managed-service (subgraph) dispatch falls through to unknown.
    const http = makeHarness({
      mode: 'http',
      http: { totalreclaw_import_batch: async () => ok({ routed: true }) },
      subgraph: {},
    });
    const sub = makeHarness({
      mode: 'subgraph',
      http: { totalreclaw_import_batch: async () => ok({ routed: true }) },
      subgraph: {},
    });
    expect(parse(await http.dispatch('totalreclaw_import_batch', {})).routed).toBe(true);

    const gated = await sub.dispatch('totalreclaw_import_batch', {});
    expect(gated.isError).toBe(true);
    expect(parse(gated).error).toBe('Unknown tool: totalreclaw_import_batch');
  });
});

describe('dispatch: cross-cutting policy', () => {
  it('invalidates the cache after a successful mutating tool', async () => {
    const h = makeHarness({
      mode: 'subgraph',
      subgraph: { totalreclaw_forget: async () => ok({ deleted: 1 }) },
    });
    expect(SUBGRAPH_POLICY.totalreclaw_forget.invalidateCache).toBe(true);
    await h.dispatch('totalreclaw_forget', { fact_id: 'f1' });
    expect(h.mutations).toBe(1);
  });

  it('does not invalidate the cache for a read-only tool', async () => {
    const h = makeHarness({
      mode: 'subgraph',
      subgraph: { totalreclaw_recall: async () => ok({ memories: [] }) },
    });
    await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(h.mutations).toBe(0);
  });

  it('traps a quota error into the quota envelope for quota-guarded tools', async () => {
    const h = makeHarness({
      mode: 'http',
      http: {
        totalreclaw_remember: async () => {
          throw new QuotaError();
        },
      },
    });
    expect(HTTP_POLICY.totalreclaw_remember.quotaGuard).toBe(true);
    const res = await h.dispatch('totalreclaw_remember', { fact: 'x' });
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('quota_exceeded');
    // No cache invalidation on the quota failure path.
    expect(h.mutations).toBe(0);
  });

  it('does not invalidate the cache when a mutating tool throws quota', async () => {
    const h = makeHarness({
      mode: 'subgraph',
      subgraph: {
        totalreclaw_remember: async () => {
          throw new QuotaError();
        },
      },
    });
    // remember is both quota-guarded and cache-invalidating in subgraph mode.
    expect(SUBGRAPH_POLICY.totalreclaw_remember).toEqual({ quotaGuard: true, invalidateCache: true });
    const res = await h.dispatch('totalreclaw_remember', { fact: 'x' });
    expect(parse(res).error).toBe('quota_exceeded');
    expect(h.mutations).toBe(0);
  });
});

describe('dispatch: error funnel', () => {
  it('maps quota errors from non-guarded tools to the quota envelope', async () => {
    const h = makeHarness({
      mode: 'http',
      http: {
        totalreclaw_recall: async () => {
          throw new QuotaError();
        },
      },
    });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(parse(res).error).toBe('quota_exceeded');
  });

  it('maps auth errors to the auth-hint envelope', async () => {
    const h = makeHarness({
      mode: 'http',
      http: {
        totalreclaw_recall: async () => {
          throw new AuthError();
        },
      },
    });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('auth_hint');
  });

  it('maps any other handler error to a generic error envelope', async () => {
    const h = makeHarness({
      mode: 'http',
      http: {
        totalreclaw_recall: async () => {
          throw new Error('boom');
        },
      },
    });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('boom');
  });

  it('maps a bundle-resolution failure (e.g. client init) to a generic error', async () => {
    const h = makeHarness({ mode: 'http', bundleError: new Error('client down') });
    const res = await h.dispatch('totalreclaw_recall', { query: 'x' });
    expect(res.isError).toBe(true);
    expect(parse(res).error).toBe('client down');
  });
});
