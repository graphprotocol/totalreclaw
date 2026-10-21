/**
 * @jest-environment node
 *
 * DEP-15 — dual-era stdio handshake, asserted on the RAW JSON-RPC wire.
 *
 * `serveTotalReclawStdio` is driven through the SDK's real
 * `StdioServerTransport` (newline-delimited JSON over two PassThrough
 * streams standing in for the process's stdin/stdout), so these tests see
 * exactly the bytes a host sees:
 *   - legacy era: `initialize` (2025-11-25 and older) — Claude Desktop,
 *     Cursor, Claude Code with MCP_PROTOCOL_NEGOTIATION unset/legacy.
 *   - modern era: `server/discover` + per-request `_meta` (2026-07-28) —
 *     Claude Code with MCP_PROTOCOL_NEGOTIATION=auto, SDK-v2 clients.
 * Tool calls go to a fake dispatcher; no relay, chain or key material.
 */

import * as fs from 'fs';
import * as path from 'path';
import { PassThrough } from 'stream';

import type { Server } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import type { StdioServerHandle } from '@modelcontextprotocol/server/stdio';

import {
  describeStdioError,
  serveTotalReclawStdio,
  type ServerSetupDeps,
} from '../src/server-setup';
import { SERVER_INSTRUCTIONS } from '../src/prompts';
import type { ToolResponse } from '../src/tools/types';

type Json = Record<string, any>;

const GOLDEN_TOOLS = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'tools-list.golden.json'), 'utf8'),
) as Json[];

const EXPECTED_SERVER_INFO = { name: 'totalreclaw', version: '1.0.0' };
const EXPECTED_CAPABILITIES = {
  tools: {},
  prompts: {},
  resources: { subscribe: true, listChanged: true },
};

/** A valid 2026-07-28 per-request envelope. */
const MODERN_META = {
  'io.modelcontextprotocol/protocolVersion': '2026-07-28',
  'io.modelcontextprotocol/clientInfo': { name: 'dep15-wire-test', version: '0.0.0' },
  'io.modelcontextprotocol/clientCapabilities': {},
};

/** The `initialize` params shape Claude Desktop / Cursor send. */
function initializeParams(protocolVersion: string): Json {
  return {
    protocolVersion,
    capabilities: {},
    clientInfo: { name: 'dep15-legacy-host', version: '0.0.0' },
  };
}

/** Minimal line-framed JSON-RPC peer over the server's stdin/stdout. */
class WirePeer {
  private buffer = '';
  private readonly waiters = new Map<string | number, (msg: Json) => void>();
  readonly received: Json[] = [];

  constructor(
    private readonly toServer: PassThrough,
    fromServer: PassThrough,
  ) {
    fromServer.on('data', (chunk: Buffer) => {
      this.buffer += chunk.toString('utf8');
      let newline = this.buffer.indexOf('\n');
      while (newline >= 0) {
        const line = this.buffer.slice(0, newline).trim();
        this.buffer = this.buffer.slice(newline + 1);
        if (line) {
          const msg = JSON.parse(line) as Json;
          this.received.push(msg);
          const waiter = msg.id !== undefined ? this.waiters.get(msg.id) : undefined;
          if (waiter) {
            this.waiters.delete(msg.id);
            waiter(msg);
          }
        }
        newline = this.buffer.indexOf('\n');
      }
    });
  }

  request(id: string | number, method: string, params?: Json): Promise<Json> {
    const answered = new Promise<Json>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(id);
        reject(new Error(`no response to ${method} (id ${String(id)}) within 5000 ms`));
      }, 5000);
      this.waiters.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });
    this.toServer.write(
      JSON.stringify({ jsonrpc: '2.0', id, method, ...(params !== undefined ? { params } : {}) }) + '\n',
    );
    return answered;
  }

  notify(method: string, params?: Json): void {
    this.toServer.write(
      JSON.stringify({ jsonrpc: '2.0', method, ...(params !== undefined ? { params } : {}) }) + '\n',
    );
  }

  /** Resolve with the first received message matching `predicate` (polls; 5 s cap). */
  async waitFor(predicate: (msg: Json) => boolean, label: string): Promise<Json> {
    const deadline = Date.now() + 5000;
    for (;;) {
      const found = this.received.find(predicate);
      if (found) return found;
      if (Date.now() > deadline) throw new Error(`no ${label} within 5000 ms`);
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }
}

interface Harness {
  peer: WirePeer;
  handle: StdioServerHandle;
  calls: Array<{ name: string; args: unknown }>;
  instances: Server[];
}

let active: Harness | undefined;

function startServer(): Harness {
  const toServer = new PassThrough();
  const fromServer = new PassThrough();
  const calls: Array<{ name: string; args: unknown }> = [];
  const instances: Server[] = [];
  const deps: ServerSetupDeps = {
    callTool: async (name: string, args: unknown): Promise<ToolResponse> => {
      calls.push({ name, args });
      return { content: [{ type: 'text', text: JSON.stringify({ ok: true, tool: name }) }] };
    },
    isManagedMode: () => true,
    getClient: async () => {
      throw new Error('getClient must not be called in these tests');
    },
  };
  const handle = serveTotalReclawStdio(deps, {
    transport: new StdioServerTransport(toServer, fromServer),
    onServerCreated: (server) => instances.push(server),
  });
  active = { peer: new WirePeer(toServer, fromServer), handle, calls, instances };
  return active;
}

let stderrSpy: jest.SpyInstance;

beforeEach(() => {
  // serveStdio reports rejected openings through onerror → console.error.
  stderrSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(async () => {
  if (active) {
    await active.handle.close();
    active = undefined;
  }
  stderrSpy.mockRestore();
});

describe('legacy era (2025-11-25 initialize) — Claude Desktop / Cursor path', () => {
  it('answers initialize with the requested version, unchanged serverInfo, capabilities and instructions', async () => {
    const { peer } = startServer();
    const res = await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    expect(res.error).toBeUndefined();
    expect(res.result).toEqual({
      protocolVersion: '2025-11-25',
      capabilities: EXPECTED_CAPABILITIES,
      serverInfo: EXPECTED_SERVER_INFO,
      instructions: SERVER_INSTRUCTIONS,
    });
    // Happy paths additionally assert stderr stayed clean: serveStdio reports
    // rejected openings through onerror → console.error, and a spurious one
    // here would mean a healthy-looking exchange was internally failed.
    expect(console.error).not.toHaveBeenCalled();
  });

  it.each(['2025-06-18', '2025-03-26', '2024-11-05', '2024-10-07'])(
    'echoes the older legacy version %s a pre-2025-11-25 host asks for',
    async (version) => {
      const { peer } = startServer();
      const res = await peer.request(1, 'initialize', initializeParams(version));
      expect(res.result.protocolVersion).toBe(version);
      expect(console.error).not.toHaveBeenCalled();
    },
  );

  it('answers an unknown initialize version with 2025-11-25 (the latest legacy revision)', async () => {
    const { peer } = startServer();
    const res = await peer.request(1, 'initialize', initializeParams('2099-01-01'));
    expect(res.result.protocolVersion).toBe('2025-11-25');
    expect(console.error).not.toHaveBeenCalled();
  });

  it('serves tools/list with the golden tool list and no 2026-only result fields', async () => {
    const { peer } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    peer.notify('notifications/initialized');
    const res = await peer.request(2, 'tools/list', {});
    expect(res.result.tools).toEqual(GOLDEN_TOOLS);
    expect(Object.keys(res.result)).toEqual(['tools']);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('routes tools/call through the injected dispatcher and returns its content unchanged', async () => {
    const { peer, calls } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    peer.notify('notifications/initialized');
    const res = await peer.request(2, 'tools/call', {
      name: 'totalreclaw_recall',
      arguments: { query: 'coffee preferences' },
    });
    expect(calls).toEqual([{ name: 'totalreclaw_recall', args: { query: 'coffee preferences' } }]);
    expect(res.result).toEqual({
      content: [{ type: 'text', text: JSON.stringify({ ok: true, tool: 'totalreclaw_recall' }) }],
    });
    expect(console.error).not.toHaveBeenCalled();
  });

  it('answers ping (2025-era keepalive hosts send)', async () => {
    const { peer } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    const res = await peer.request(2, 'ping');
    expect(res.result).toEqual({});
    expect(console.error).not.toHaveBeenCalled();
  });

  it('answers server/discover on a legacy-pinned connection with plain -32601 "Method not found"', async () => {
    // Claude Code 2.1.283 mis-negotiates when this error names a protocol
    // version (anthropics/claude-code#97391); the plain message is safe.
    const { peer } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    const res = await peer.request(2, 'server/discover', { _meta: MODERN_META });
    expect(res.error).toEqual({ code: -32601, message: 'Method not found' });
  });
});

describe('hosts that mix the eras (anthropics/claude-code#97189 shape)', () => {
  it('answers initialize carrying protocolVersion 2026-07-28 as legacy 2025-11-25, then still serves _meta-bearing requests', async () => {
    const { peer, calls } = startServer();
    const init = await peer.request(1, 'initialize', initializeParams('2026-07-28'));
    expect(init.result.protocolVersion).toBe('2025-11-25');
    peer.notify('notifications/initialized');
    const tools = await peer.request(2, 'tools/list', { _meta: MODERN_META });
    expect(tools.result.tools).toEqual(GOLDEN_TOOLS);
    const call = await peer.request(3, 'tools/call', {
      name: 'totalreclaw_status',
      arguments: {},
      _meta: MODERN_META,
    });
    expect(call.result.content).toEqual([
      { type: 'text', text: JSON.stringify({ ok: true, tool: 'totalreclaw_status' }) },
    ]);
    expect(calls).toEqual([{ name: 'totalreclaw_status', args: {} }]);
    expect(console.error).not.toHaveBeenCalled();
  });
});

describe('describeStdioError (stderr phrase-safety)', () => {
  it('never echoes a JSON.parse SyntaxError, which quotes the offending line', () => {
    const err = new SyntaxError('Unexpected token \'a\', "abandon ab"... is not valid JSON');
    expect(describeStdioError(err)).toBe('discarded a stdin line that is not valid JSON');
  });

  it('collapses a multi-line message to one line', () => {
    expect(describeStdioError(new Error('first line\n   second line'))).toBe('first line second line');
  });

  it('caps the message at 300 characters', () => {
    expect(describeStdioError(new Error('x'.repeat(500)))).toHaveLength(300);
  });
});

describe('modern era (2026-07-28 server/discover + per-request _meta)', () => {
  it('answers server/discover with versions, capabilities, instructions and serverInfo _meta', async () => {
    const { peer } = startServer();
    const res = await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    expect(res.error).toBeUndefined();
    expect(res.result).toEqual({
      resultType: 'complete',
      supportedVersions: ['2026-07-28'],
      capabilities: EXPECTED_CAPABILITIES,
      instructions: SERVER_INSTRUCTIONS,
      ttlMs: 0,
      cacheScope: 'private',
      _meta: { 'io.modelcontextprotocol/serverInfo': EXPECTED_SERVER_INFO },
    });
    expect(console.error).not.toHaveBeenCalled();
  });

  it('serves tools/list with the golden tool list plus resultType/ttlMs/cacheScope', async () => {
    const { peer } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    const res = await peer.request(2, 'tools/list', { _meta: MODERN_META });
    expect(res.result.tools).toEqual(GOLDEN_TOOLS);
    expect(res.result.resultType).toBe('complete');
    expect(res.result.ttlMs).toBe(0);
    expect(res.result.cacheScope).toBe('private');
    expect(res.result._meta).toEqual({ 'io.modelcontextprotocol/serverInfo': EXPECTED_SERVER_INFO });
    expect(console.error).not.toHaveBeenCalled();
  });

  it('routes tools/call with content identical to the legacy era', async () => {
    const { peer, calls } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    const res = await peer.request(2, 'tools/call', {
      name: 'totalreclaw_recall',
      arguments: { query: 'coffee preferences' },
      _meta: MODERN_META,
    });
    expect(calls).toEqual([{ name: 'totalreclaw_recall', args: { query: 'coffee preferences' } }]);
    expect(res.result.content).toEqual([
      { type: 'text', text: JSON.stringify({ ok: true, tool: 'totalreclaw_recall' }) },
    ]);
    expect(res.result.resultType).toBe('complete');
    expect(console.error).not.toHaveBeenCalled();
  });

  it('serves a modern request that was not preceded by server/discover (discover is optional)', async () => {
    const { peer } = startServer();
    const res = await peer.request(1, 'tools/list', { _meta: MODERN_META });
    expect(res.result.tools).toEqual(GOLDEN_TOOLS);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('answers ping with plain -32601 "Method not found" once the connection is pinned modern (ping was removed in 2026-07-28)', async () => {
    const { peer } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    const res = await peer.request(2, 'ping', { _meta: MODERN_META });
    expect(res.error).toEqual({ code: -32601, message: 'Method not found' });
  });

  it('rejects an unsupported modern protocolVersion with -32022 naming 2026-07-28', async () => {
    const { peer, instances } = startServer();
    const res = await peer.request(1, 'server/discover', {
      _meta: { ...MODERN_META, 'io.modelcontextprotocol/protocolVersion': '2099-01-01' },
    });
    expect(res.error.code).toBe(-32022);
    expect(res.error.data).toEqual({ supported: ['2026-07-28'], requested: '2099-01-01' });
    expect(instances).toHaveLength(0);
  });

  it('rejects an envelope missing clientCapabilities with -32602', async () => {
    const { peer } = startServer();
    const res = await peer.request(1, 'server/discover', {
      _meta: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' },
    });
    expect(res.error.code).toBe(-32602);
    expect(res.error.data).toEqual({
      envelope: { key: 'io.modelcontextprotocol/clientCapabilities', problem: 'missing' },
    });
  });

  it('rejects a legacy initialize once the connection is pinned modern (-32022, supported: 2026-07-28)', async () => {
    const { peer } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    await peer.request(2, 'tools/list', { _meta: MODERN_META });
    const res = await peer.request(3, 'initialize', initializeParams('2025-11-25'));
    expect(res.error.code).toBe(-32022);
    expect(res.error.data).toEqual({ supported: ['2026-07-28'], requested: '2025-11-25' });
  });
});

describe('memory-context change notifications (remember → sendResourceUpdated)', () => {
  const RESOURCE_URI = 'memory://context/summary';

  it('legacy era: delivered unsolicited, exactly as the v1-SDK server did', async () => {
    const { peer, instances } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    peer.notify('notifications/initialized');
    await instances[instances.length - 1].sendResourceUpdated({ uri: RESOURCE_URI });
    const note = await peer.waitFor(
      (m) => m.method === 'notifications/resources/updated',
      'notifications/resources/updated',
    );
    expect(note.params).toEqual({ uri: RESOURCE_URI });
    expect(console.error).not.toHaveBeenCalled();
  });

  it('modern era: delivered on the subscriptions/listen stream the host opened, stamped with its id', async () => {
    const { peer, instances } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    peer.request('listen-1', 'subscriptions/listen', {
      notifications: { toolsListChanged: true, resourceSubscriptions: [RESOURCE_URI] },
      _meta: MODERN_META,
    }).catch(() => undefined); // the listen result only arrives when the stream closes
    const ack = await peer.waitFor(
      (m) => m.method === 'notifications/subscriptions/acknowledged',
      'subscriptions ack',
    );
    // tools.listChanged is not advertised, so only the resource subscription is honoured.
    expect(ack.params).toEqual({
      notifications: { resourceSubscriptions: [RESOURCE_URI] },
      _meta: { 'io.modelcontextprotocol/subscriptionId': 'listen-1' },
    });
    await instances[instances.length - 1].sendResourceUpdated({ uri: RESOURCE_URI });
    const note = await peer.waitFor(
      (m) => m.method === 'notifications/resources/updated',
      'notifications/resources/updated',
    );
    expect(note.params).toEqual({
      uri: RESOURCE_URI,
      _meta: { 'io.modelcontextprotocol/subscriptionId': 'listen-1' },
    });
    expect(console.error).not.toHaveBeenCalled();
  });
});

describe('probe fallback (server/discover answered, client still falls back to initialize)', () => {
  it('discards the probe instance and serves the legacy era from a fresh instance', async () => {
    const { peer, instances } = startServer();
    const probe = await peer.request('probe', 'server/discover', { _meta: MODERN_META });
    expect(probe.result.supportedVersions).toEqual(['2026-07-28']);

    const init = await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    expect(init.result.protocolVersion).toBe('2025-11-25');
    peer.notify('notifications/initialized');
    const tools = await peer.request(2, 'tools/list', {});
    expect(tools.result.tools).toEqual(GOLDEN_TOOLS);

    // One probe instance + one legacy instance; the last one is live.
    expect(instances).toHaveLength(2);
    expect(console.error).not.toHaveBeenCalled();
  });
});

describe('client attribution (the input seam getClientIdentifier() reads)', () => {
  // `mcp/src/index.ts` `getClientIdentifier()` builds `X-TotalReclaw-Client`
  // as `mcp-server:<name>` from `server.getClientVersion()` (index.ts, the
  // "Client identification" section). That function is module-private and
  // imports of index.ts boot the server, so these tests pin its INPUT on the
  // live instance instead: after the era's handshake, `getClientVersion()`
  // must reflect the client the host declared.

  it('legacy era: initialize populates getClientVersion() with the clientInfo the host sent', async () => {
    const { peer, instances } = startServer();
    await peer.request(1, 'initialize', initializeParams('2025-11-25'));
    const live = instances[instances.length - 1];
    expect(live.getClientVersion()).toEqual({ name: 'dep15-legacy-host', version: '0.0.0' });
    expect(console.error).not.toHaveBeenCalled();
  });

  // SKIPPED — finding from the PR #668 review, verified against
  // @modelcontextprotocol/server 2.2.0: the SDK's stdio entry (`serveStdio`)
  // never seeds the connection-scoped client identity from the per-request
  // `_meta` envelope (`seedClientIdentityFromEnvelope` is called only by the
  // HTTP entry, `createMcpHandler`). So on a modern stdio connection
  // `getClientVersion()` stays undefined and `getClientIdentifier()`
  // (mcp/src/index.ts) keeps the default `mcp-server` id instead of
  // `mcp-server:<host>`. Product code is unchanged per the review decision;
  // the fix belongs to the follow-up that moves client attribution to
  // `ctx.mcpReq.envelope` (docs/specs/totalreclaw/mcp-dual-era.md §5 known
  // gaps). Un-skip when that lands.
  it.skip('modern era: a tools/call envelope carrying clientInfo is reflected in getClientVersion()', async () => {
    const { peer, instances } = startServer();
    await peer.request('discover-1', 'server/discover', { _meta: MODERN_META });
    await peer.request(2, 'tools/call', {
      name: 'totalreclaw_status',
      arguments: {},
      _meta: MODERN_META,
    });
    const live = instances[instances.length - 1];
    expect(live.getClientVersion()?.name).toBe(MODERN_META['io.modelcontextprotocol/clientInfo'].name);
  });
});
