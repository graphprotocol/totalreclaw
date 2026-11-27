/**
 * Existing-account guard for OpenClaw pairing (#610 + the gateway pair
 * routes). Everything lives in a temp dir; the relay is a loopback stub.
 * Dummy phrase only (BIP-39 test vector).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createServer } from 'node:http';
import { AddressInfo } from 'node:net';
import { Writable } from 'node:stream';
import { WebSocketServer } from 'ws';

import {
  discardReplaceCopy,
  existingAccountGuard,
  hasExistingAccount,
  preserveCredentialsBeforeReplace,
  stripAccountFields,
  writeCredentialsJson,
} from '../fs-helpers.js';
import { runRelayPairCli } from './pair-cli-relay.js';
import { registerPairCli } from './pair-cli.js';
import { encryptPairingPayload, generateGatewayKeypair } from './pair-crypto.js';

let n = 0;
let failed = 0;
function ok(name: string, cond: boolean): void {
  n += 1;
  console.log(`${cond ? 'ok' : 'not ok'} ${n} - ${name}`);
  if (!cond) failed += 1;
}

const PHRASE =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-guard-'));
let seq = 0;
function freshPath(): string {
  const d = path.join(tmp, `case${seq++}`);
  fs.mkdirSync(d);
  return path.join(d, 'credentials.json');
}
const OLD = { mnemonic: 'dummy old phrase', userId: 'old-user', salt: 'old-salt' };

// --- hasExistingAccount ---------------------------------------------------
{
  const p = freshPath();
  ok('missing file is no account', !hasExistingAccount(p));
  fs.writeFileSync(p, '  ');
  ok('empty file is no account', !hasExistingAccount(p));
  fs.writeFileSync(p, JSON.stringify({ mnemonic: 'x y z' }));
  ok('mnemonic is an account', hasExistingAccount(p));
  fs.writeFileSync(p, JSON.stringify({ version: 2, account: { smart_account: '0x1' } }));
  ok('bundle shape is an account', hasExistingAccount(p));
  fs.writeFileSync(p, JSON.stringify({ scope_address: '0x0000000000000000000000000000000000000001' }));
  ok('scope_address only is an account', hasExistingAccount(p));
  ok('guard refuses a scope_address-only file', existingAccountGuard(p, { replace: false }) === 'existing_account');
  fs.writeFileSync(p, JSON.stringify({ salt: 'c2FsdA==' }));
  ok('salt only is an account', hasExistingAccount(p));
  fs.writeFileSync(p, JSON.stringify({ salt: '  ', scope_address: '', serverUrl: 'x' }));
  ok('blank salt/scope_address and unrelated settings are no account', !hasExistingAccount(p));
  fs.writeFileSync(p, '{not json');
  ok('unparseable non-empty file counts as an account', hasExistingAccount(p));
  ok('guard refuses without replace', existingAccountGuard(p, { replace: false }) === 'existing_account');
  ok('guard allows explicit replace', existingAccountGuard(p, { replace: true }) === null);
  ok('guard allows when no account', existingAccountGuard(freshPath(), { replace: false }) === null);
}

// --- atomic write ---------------------------------------------------------
{
  const p = freshPath();
  ok('write ok', writeCredentialsJson(p, { mnemonic: 'a b c' }));
  ok('mode 0600', (fs.statSync(p).mode & 0o777) === 0o600);
  writeCredentialsJson(p, OLD);
  const realRename = fs.renameSync;
  fs.renameSync = (() => { throw new Error('simulated crash'); }) as typeof fs.renameSync;
  const res = writeCredentialsJson(p, { mnemonic: 'new' });
  fs.renameSync = realRename;
  ok('crash between write and rename reports failure', res === false);
  ok('old file still valid after crash', JSON.parse(fs.readFileSync(p, 'utf-8')).userId === 'old-user');
  ok('no temp file left behind', fs.readdirSync(path.dirname(p)).length === 1);
}

// --- preserve / discard ---------------------------------------------------
{
  const p = freshPath();
  writeCredentialsJson(p, OLD);
  const copy = preserveCredentialsBeforeReplace(p)!;
  ok('copy exists, 0600, identical', fs.readFileSync(copy).equals(fs.readFileSync(p)) && (fs.statSync(copy).mode & 0o777) === 0o600);
  const copy2 = preserveCredentialsBeforeReplace(p)!;
  ok('two copies never collide', copy !== copy2);
  discardReplaceCopy(p, copy);
  ok('discard removes an identical copy', !fs.existsSync(copy));
  writeCredentialsJson(p, { mnemonic: 'changed' });
  discardReplaceCopy(p, copy2);
  ok('discard keeps the copy once the file changed', fs.existsSync(copy2));
  ok('stripAccountFields drops identity but keeps the rest',
    JSON.stringify(stripAccountFields({ ...OLD, serverUrl: 's' } as never)) === JSON.stringify({ serverUrl: 's' }));
}

// --- relay CLI end to end against a loopback stub -------------------------
async function startRelay(opts: { echoReplace: boolean; confirm: boolean }) {
  const http = createServer((_req, res) => { res.statusCode = 500; res.end('{}'); });
  const wss = new WebSocketServer({ server: http });
  const seen: Record<string, unknown>[] = [];
  wss.on('connection', (ws) => {
    ws.on('message', (raw) => {
      const msg = JSON.parse(raw.toString()) as Record<string, unknown>;
      seen.push(msg);
      if (msg.type !== 'open') return;
      ws.send(JSON.stringify({
        type: 'opened', token: 'tok1234567890abc',
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        ...(opts.echoReplace && msg.replace === true ? { replace: true } : {}),
      }));
      setTimeout(() => {
        const dev = generateGatewayKeypair();
        const { nonceB64, ciphertextB64 } = encryptPairingPayload({
          skLocalB64: dev.skB64, pkRemoteB64: String(msg.gateway_pubkey),
          sid: 'tok1234567890abc', plaintext: Buffer.from(PHRASE, 'utf-8'),
        } as never);
        ws.send(JSON.stringify({
          type: 'forward', client_pubkey: dev.pkB64, nonce: nonceB64, ciphertext: ciphertextB64,
          ...(opts.confirm ? { replace_confirmed: true } : {}),
        }));
      }, 20);
    });
  });
  await new Promise<void>((r) => http.listen(0, '127.0.0.1', r));
  const port = (http.address() as AddressInfo).port;
  return { url: `ws://127.0.0.1:${port}`, seen, close: () => { wss.close(); http.close(); } };
}

function sink(): { stream: Writable; text: () => string } {
  let t = '';
  return { stream: new Writable({ write(c, _e, cb) { t += c.toString(); cb(); } }), text: () => t };
}

async function runCli(p: string, relayUrl: string, replace: boolean) {
  const out = sink(); const err = sink();
  const outcome = await runRelayPairCli('import', {
    relayBaseUrl: relayUrl,
    credentialsPath: p,
    onboardingStatePath: path.join(path.dirname(p), 'onboarding.json'),
    pluginVersion: 'test',
    deriveScopeAddress: async () => undefined,
    logger: { info() {}, warn() {}, error() {} },
    renderQr: (_u, cb) => cb(''),
    io: { stdout: out.stream, stderr: err.stream, onInterrupt: () => () => {} },
    outputMode: 'url-pin',
    replace,
  });
  return { outcome, err: err.text() };
}

(async () => {
  // refuses over an existing account; never even opens a session
  {
    const relay = await startRelay({ echoReplace: true, confirm: true });
    const p = freshPath();
    writeCredentialsJson(p, OLD);
    const before = fs.readFileSync(p, 'utf-8');
    const { outcome, err } = await runCli(p, relay.url, false);
    ok('CLI refuses over an existing account', outcome.status === 'error' && /already has/.test(err));
    ok('CLI refusal leaves the file untouched', fs.readFileSync(p, 'utf-8') === before);
    ok('CLI refusal opened no relay session', relay.seen.length === 0);
    relay.close();
  }
  // explicit replace: opens a replace session, keeps a copy, clean slate
  {
    const relay = await startRelay({ echoReplace: true, confirm: true });
    const p = freshPath();
    writeCredentialsJson(p, { ...OLD, serverUrl: 'keep-me' } as never);
    const { outcome } = await runCli(p, relay.url, true);
    const now = JSON.parse(fs.readFileSync(p, 'utf-8'));
    const copies = fs.readdirSync(path.dirname(p)).filter((f) => f.includes('.replaced-'));
    ok('replace completes', outcome.status === 'completed');
    ok('replace sent replace:true in the open frame', relay.seen[0]?.replace === true);
    ok('new phrase written, old userId/salt gone', now.mnemonic === PHRASE && now.userId === undefined && now.salt !== 'old-salt');
    ok('replace keeps one .replaced copy of the old file', copies.length === 1
      && JSON.parse(fs.readFileSync(path.join(path.dirname(p), copies[0]), 'utf-8')).userId === 'old-user');
    ok('replace leaves unrelated settings', now.serverUrl === 'keep-me');
    relay.close();
  }
  // old relay that does not echo replace: refuse, file untouched
  {
    const relay = await startRelay({ echoReplace: false, confirm: true });
    const p = freshPath();
    writeCredentialsJson(p, OLD);
    const { outcome } = await runCli(p, relay.url, true);
    ok('relay without replace support: refused', outcome.status === 'error');
    ok('and the old account is intact', JSON.parse(fs.readFileSync(p, 'utf-8')).userId === 'old-user');
    relay.close();
  }
  // replace session but user did not confirm on the page: nothing changes
  {
    const relay = await startRelay({ echoReplace: true, confirm: false });
    const p = freshPath();
    writeCredentialsJson(p, OLD);
    const { outcome } = await runCli(p, relay.url, true);
    ok('unconfirmed replace is refused', outcome.status !== 'completed');
    ok('unconfirmed replace leaves the account intact', JSON.parse(fs.readFileSync(p, 'utf-8')).userId === 'old-user');
    relay.close();
  }
  // no existing account: ordinary pairing still works
  {
    const relay = await startRelay({ echoReplace: false, confirm: false });
    const p = freshPath();
    const { outcome } = await runCli(p, relay.url, false);
    ok('fresh pairing still works', outcome.status === 'completed' && JSON.parse(fs.readFileSync(p, 'utf-8')).mnemonic === PHRASE);
    relay.close();
  }
  // leftover file with only scope_address: CLI refuses, no session, file untouched
  {
    const relay = await startRelay({ echoReplace: true, confirm: true });
    const p = freshPath();
    fs.writeFileSync(p, JSON.stringify({ scope_address: '0x0000000000000000000000000000000000000001' }));
    const before = fs.readFileSync(p, 'utf-8');
    const { outcome } = await runCli(p, relay.url, false);
    ok('CLI refuses over a scope_address-only leftover', outcome.status === 'error');
    ok('scope_address leftover: file untouched, no relay session', fs.readFileSync(p, 'utf-8') === before && relay.seen.length === 0);
    relay.close();
  }
  // fresh pairing never inherits account fields from a file
  {
    const relay = await startRelay({ echoReplace: false, confirm: false });
    const p = freshPath();
    fs.writeFileSync(p, JSON.stringify({ serverUrl: 'keep-me' }));
    const { outcome } = await runCli(p, relay.url, false);
    const now = JSON.parse(fs.readFileSync(p, 'utf-8'));
    ok('fresh pairing keeps unrelated settings and writes its own salt', outcome.status === 'completed' && now.serverUrl === 'keep-me' && typeof now.salt === 'string');
  }
  // pair with no relay runner (effective mode = local) over an account: refused at start
  {
    const mk = (name: string): any => {
      const c: any = { _name: name, _action: null, commands: [] as any[],
        name() { return this._name; },
        command(n: string) { const ch = mk(n); this.commands.push(ch); return ch; },
        description() { return this; }, option() { return this; },
        action(fn: any) { this._action = fn; return this; } };
      return c;
    };
    const program = mk('openclaw');
    let started = false;
    registerPairCli(program, {
      sessionsPath: path.join(tmp, 'sessions.json'),
      renderPairingUrl: () => { started = true; return 'x'; },
      logger: { info() {}, warn() {}, error() {} },
      hasExistingAccount: () => true,
    });
    const pairCmd = program.commands[0].commands[0];
    const err = sink();
    const realErr = process.stderr.write.bind(process.stderr);
    const realExit = process.exit;
    let exitCode: number | undefined;
    (process.stderr as any).write = (c: any) => { err.stream.write(c); return true; };
    (process as any).exit = (code?: number) => { exitCode ??= code; throw new Error('__exit__'); };
    try { await pairCmd._action('generate', {}, pairCmd); } catch (e) { if ((e as Error).message !== '__exit__') throw e; }
    finally { (process.stderr as any).write = realErr; (process as any).exit = realExit; }
    ok('pair without relay runner over an account: refused at start (exit 1)', exitCode === 1 && /already has a TotalReclaw account/.test(err.text()));
    ok('and no local session was started', !started);
  }
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log(`# ${n - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
