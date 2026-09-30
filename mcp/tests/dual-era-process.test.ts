/**
 * @jest-environment node
 *
 * DEP-15 — end-to-end stdio smoke of the BUILT server (`dist/index.js`) as a
 * host launches it: a child process speaking newline-delimited JSON-RPC on
 * stdin/stdout. Covers the `main()` wiring that unit tests cannot import
 * (importing index.ts boots the server).
 *
 * Runs UNCONFIGURED: a throwaway HOME (no ~/.totalreclaw/credentials.json,
 * no ~/.openclaw) and an env with no TOTALRECLAW_* variables, so there is no
 * key material and no network I/O. Requires `npm run build` first (CI runs
 * `npm run build && npm test`).
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

type Json = Record<string, any>;

const DIST_ENTRY = path.join(__dirname, '..', 'dist', 'index.js');
const GOLDEN_TOOLS = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'tools-list.golden.json'), 'utf8'),
) as Json[];

const MODERN_META = {
  'io.modelcontextprotocol/protocolVersion': '2026-07-28',
  'io.modelcontextprotocol/clientInfo': { name: 'dep15-process-test', version: '0.0.0' },
  'io.modelcontextprotocol/clientCapabilities': {},
};

interface Session {
  child: ChildProcessWithoutNullStreams;
  stdoutLines: string[];
  request: (id: string | number, method: string, params?: Json) => Promise<Json>;
  notify: (method: string, params?: Json) => void;
  exited: Promise<number | null>;
}

let session: Session | undefined;
let home: string | undefined;

function launch(): Session {
  home = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-dep15-'));
  const child = spawn(process.execPath, [DIST_ENTRY], {
    env: { PATH: process.env.PATH ?? '', HOME: home },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const stdoutLines: string[] = [];
  const waiters = new Map<string | number, (msg: Json) => void>();
  let buffer = '';
  child.stdout.on('data', (chunk: Buffer) => {
    buffer += chunk.toString('utf8');
    let newline = buffer.indexOf('\n');
    while (newline >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line.trim()) {
        stdoutLines.push(line);
        const msg = JSON.parse(line) as Json;
        const waiter = msg.id !== undefined ? waiters.get(msg.id) : undefined;
        if (waiter) {
          waiters.delete(msg.id);
          waiter(msg);
        }
      }
      newline = buffer.indexOf('\n');
    }
  });
  child.stderr.resume(); // startup banner + poller logs; drained, not asserted
  const exited = new Promise<number | null>((resolve) => child.on('exit', (code) => resolve(code)));
  const write = (msg: Json) => child.stdin.write(JSON.stringify(msg) + '\n');
  session = {
    child,
    stdoutLines,
    exited,
    request: (id, method, params) => {
      const answered = new Promise<Json>((resolve, reject) => {
        const timer = setTimeout(() => {
          waiters.delete(id);
          reject(new Error(`no response to ${method} (id ${String(id)}) within 15000 ms`));
        }, 15000);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });
      write({ jsonrpc: '2.0', id, method, ...(params !== undefined ? { params } : {}) });
      return answered;
    },
    notify: (method, params) =>
      write({ jsonrpc: '2.0', method, ...(params !== undefined ? { params } : {}) }),
  };
  return session;
}

afterEach(async () => {
  if (session && session.child.exitCode === null) {
    session.child.kill('SIGKILL');
    await session.exited;
  }
  session = undefined;
  if (home) fs.rmSync(home, { recursive: true, force: true });
  home = undefined;
});

describe('built server over a real stdio pipe (dist/index.js)', () => {
  it('serves the legacy era: initialize → tools/list (golden); stdout is JSON-RPC only', async () => {
    const s = launch();
    const init = await s.request(1, 'initialize', {
      protocolVersion: '2025-11-25',
      capabilities: {},
      clientInfo: { name: 'dep15-process-test', version: '0.0.0' },
    });
    expect(init.result.protocolVersion).toBe('2025-11-25');
    expect(init.result.serverInfo).toEqual({ name: 'totalreclaw', version: '1.0.0' });
    s.notify('notifications/initialized');
    const tools = await s.request(2, 'tools/list', {});
    expect(tools.result.tools).toEqual(GOLDEN_TOOLS);
    for (const line of s.stdoutLines) {
      expect(JSON.parse(line).jsonrpc).toBe('2.0');
    }
  }, 30000);

  it('serves the modern era: server/discover → tools/call (unconfigured envelope)', async () => {
    const s = launch();
    const discover = await s.request('d', 'server/discover', { _meta: MODERN_META });
    expect(discover.result.supportedVersions).toEqual(['2026-07-28']);
    const call = await s.request(2, 'tools/call', {
      name: 'totalreclaw_status',
      arguments: {},
      _meta: MODERN_META,
    });
    expect(call.result.resultType).toBe('complete');
    const payload = JSON.parse(call.result.content[0].text) as Json;
    expect(payload.error).toBe('not_configured');
    for (const line of s.stdoutLines) {
      expect(JSON.parse(line).jsonrpc).toBe('2.0');
    }
  }, 30000);

  it('exits after the host closes stdin', async () => {
    const s = launch();
    await s.request('d', 'server/discover', { _meta: MODERN_META });
    s.child.stdin.end();
    let timer: NodeJS.Timeout | undefined;
    const code = await Promise.race([
      s.exited,
      new Promise<'timeout'>((resolve) => {
        timer = setTimeout(() => resolve('timeout'), 10000);
      }),
    ]);
    clearTimeout(timer);
    expect(code).not.toBe('timeout');
  }, 30000);
});
