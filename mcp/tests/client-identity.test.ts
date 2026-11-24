/**
 * @jest-environment node
 *
 * One client-identity answer in both MCP eras. The Claude Desktop tip gate, the
 * X-TotalReclaw-Client header and the support log all read it. Regression: on a
 * 2026-07-28 stdio connection `server.getClientVersion()` is never filled (no
 * `initialize`), so the tip was dropped for Claude Desktop's extension session.
 */
import { PassThrough } from 'node:stream';

import type { Server } from '@modelcontextprotocol/server';
import { StdioServerTransport, type StdioServerHandle } from '@modelcontextprotocol/server/stdio';

import { getClientInfo, observeClientInfo, resetClientIdentity } from '../src/client-identity';
import { serveTotalReclawStdio, type ServerSetupDeps } from '../src/server-setup';
import { armSetupNote, resetSetupNote, withSetupNote, SETUP_NOTE_TEXT } from '../src/setup-note';
import type { ToolResponse } from '../src/tools/types';

type Json = Record<string, any>;

const INFO_KEY = 'io.modelcontextprotocol/clientInfo';
const modernMeta = (clientInfo?: unknown): Json => ({
  'io.modelcontextprotocol/protocolVersion': '2026-07-28',
  ...(clientInfo !== undefined ? { [INFO_KEY]: clientInfo } : {}),
  'io.modelcontextprotocol/clientCapabilities': {},
});

beforeEach(() => {
  resetClientIdentity();
  resetSetupNote();
});

describe('client-identity helper', () => {
  const legacy = (info?: { name?: string; version?: string }) => ({ getClientVersion: () => info });

  it('legacy era: returns the initialize clientInfo', () => {
    expect(getClientInfo(legacy({ name: 'claude-ai', version: '1.2.3' }))).toEqual({ name: 'claude-ai', version: '1.2.3' });
  });

  it('modern era: falls back to the clientInfo observed in a request envelope', () => {
    observeClientInfo({ [INFO_KEY]: { name: 'claude-ai', version: '9' } });
    expect(getClientInfo(legacy(undefined))).toEqual({ name: 'claude-ai', version: '9' });
    expect(getClientInfo(undefined)).toEqual({ name: 'claude-ai', version: '9' });
  });

  it('the initialize value wins over an observed one', () => {
    observeClientInfo({ [INFO_KEY]: { name: 'from-envelope' } });
    expect(getClientInfo(legacy({ name: 'from-initialize' }))?.name).toBe('from-initialize');
  });

  it('unknown client: undefined', () => {
    expect(getClientInfo(legacy(undefined))).toBeUndefined();
    expect(getClientInfo(undefined)).toBeUndefined();
  });

  it.each([[undefined], [{}], [{ [INFO_KEY]: undefined }], [{ [INFO_KEY]: 'claude-ai' }], [{ [INFO_KEY]: { name: '' } }], [{ [INFO_KEY]: { name: 7 } }]])(
    'ignores a missing or malformed envelope (%j)',
    (envelope) => {
      observeClientInfo(envelope as object | undefined);
      expect(getClientInfo(undefined)).toBeUndefined();
    },
  );
});

/** Server wired like index.ts: the tip gate reads getClientInfo(server). */
function startServer() {
  const toServer = new PassThrough();
  const fromServer = new PassThrough();
  let server: Server | undefined;
  const deps: ServerSetupDeps = {
    callTool: async (): Promise<ToolResponse> =>
      withSetupNote({ content: [{ type: 'text', text: '{"tier":"free"}' }] }, getClientInfo(server)?.name),
    isManagedMode: () => true,
    getClient: async () => {
      throw new Error('unused');
    },
  };
  const handle: StdioServerHandle = serveTotalReclawStdio(deps, {
    transport: new StdioServerTransport(toServer, fromServer),
    onServerCreated: (s) => {
      server = s;
    },
  });
  const waiters = new Map<number, (m: Json) => void>();
  let buf = '';
  fromServer.on('data', (c: Buffer) => {
    buf += c.toString('utf8');
    for (let i = buf.indexOf('\n'); i >= 0; i = buf.indexOf('\n')) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) {
        const m = JSON.parse(line) as Json;
        waiters.get(m.id)?.(m);
      }
    }
  });
  const request = (id: number, method: string, params: Json): Promise<Json> =>
    new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(`no response to ${method}`)), 5000);
      waiters.set(id, (m) => {
        clearTimeout(t);
        resolve(m);
      });
      toServer.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  const notify = (method: string, params?: Json) =>
    toServer.write(JSON.stringify({ jsonrpc: '2.0', method, ...(params ? { params } : {}) }) + '\n');
  return { handle, request, notify };
}

const textOf = (res: Json): string => {
  if (!res.result) throw new Error(JSON.stringify(res));
  return res.result.content[0].text;
};

describe('tip gate through the real stdio server, both eras', () => {
  let stderrSpy: jest.SpyInstance;
  let active: StdioServerHandle | undefined;
  beforeEach(() => {
    stderrSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  });
  afterEach(async () => {
    await active?.close();
    active = undefined;
    stderrSpy.mockRestore();
  });

  it('legacy initialize as claude-ai: tip shown', async () => {
    const s = startServer();
    active = s.handle;
    armSetupNote();
    await s.request(1, 'initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'claude-ai', version: '1' } });
    s.notify('notifications/initialized');
    const res = await s.request(2, 'tools/call', { name: 'totalreclaw_status', arguments: {} });
    expect(textOf(res)).toBe(`{"tier":"free"}\n\n${SETUP_NOTE_TEXT}`);
  });

  it('modern era without initialize, clientInfo claude-ai in _meta: tip shown (the regression)', async () => {
    const s = startServer();
    active = s.handle;
    armSetupNote();
    await s.request(1, 'server/discover', { _meta: modernMeta({ name: 'claude-ai', version: '1' }) });
    const res = await s.request(2, 'tools/call', {
      name: 'totalreclaw_status',
      arguments: {},
      _meta: modernMeta({ name: 'claude-ai', version: '1' }),
    });
    expect(textOf(res)).toBe(`{"tier":"free"}\n\n${SETUP_NOTE_TEXT}`);
  });

  it('modern era, identity seen on tools/list only: still known at tools/call', async () => {
    const s = startServer();
    active = s.handle;
    armSetupNote();
    await s.request(1, 'tools/list', { _meta: modernMeta({ name: 'claude-ai', version: '1' }) });
    const res = await s.request(2, 'tools/call', { name: 'totalreclaw_status', arguments: {}, _meta: modernMeta() });
    expect(textOf(res)).toContain(SETUP_NOTE_TEXT);
  });

  it('modern era, another client: no tip', async () => {
    const s = startServer();
    active = s.handle;
    armSetupNote();
    const res = await s.request(1, 'tools/call', { name: 'totalreclaw_status', arguments: {}, _meta: modernMeta({ name: 'claude-code', version: '1' }) });
    expect(textOf(res)).toBe('{"tier":"free"}');
  });

  it('modern era, no clientInfo at all: no tip', async () => {
    const s = startServer();
    active = s.handle;
    armSetupNote();
    const res = await s.request(1, 'tools/call', { name: 'totalreclaw_status', arguments: {}, _meta: modernMeta() });
    expect(textOf(res)).toBe('{"tier":"free"}');
  });
});
