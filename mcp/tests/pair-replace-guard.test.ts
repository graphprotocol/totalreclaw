/**
 * totalreclaw_pair existing-credentials guard + user-confirmed replace mode.
 *
 * Mirrors the Hermes behaviour (`python/src/totalreclaw/hermes/pair_tool.py`,
 * public PR #684) and the relay wire contract (relay PR #72):
 *
 *   - A configured install (server configured, or credentials.json on disk)
 *     refuses to pair with `already_configured`. Nothing is opened.
 *   - `replace=true` only opens a replace-mode relay session. The relay must
 *     echo `replace: true` in `opened`, else the tool changes nothing.
 *   - A replace-mode upload is refused (`replace_not_confirmed`) unless the
 *     relay forwards the user's page confirmation. Checked before decrypting.
 *   - A confirmed replacement keeps the old credentials.json as
 *     `credentials.json.replaced-<UTC>` (0600) before writing the new one.
 *   - A normal pairing never overwrites a credentials.json that appeared
 *     after the guard ran.
 *
 * The test mnemonic is the public BIP-39 "abandon … about" vector, not a
 * real phrase. No test prints it.
 */

import { EventEmitter } from 'node:events';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import {
  handlePair,
  runPairBackgroundTask,
  preserveCredentialsBeforeReplace,
  pairToolDefinition,
  ALREADY_CONFIGURED_MESSAGE,
} from '../src/tools/pair.js';
import {
  openRemotePairSession,
  awaitPhraseUpload,
  ReplaceNotSupportedError,
  type RemotePairSession,
  type OpenRemotePairOptions,
} from '../src/pair-remote-client.js';
import {
  generateGatewayKeypair,
  encryptPairingPayload,
  type GatewayKeypair,
} from '../src/pair-crypto.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_TOKEN = 'test-token-replace-0001';
const OLD_CREDS = JSON.stringify({ userId: 'old-user', salt: 'aa', mnemonic: 'old placeholder' }) + '\n';

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

/** Minimal WebSocket stand-in: EventEmitter + send/close/readyState. */
class FakeWebSocket extends EventEmitter {
  readyState = 1; // OPEN
  sent: Array<Record<string, unknown>> = [];
  closed = false;
  /** Reply the fake relay sends to the `open` frame. */
  openedReply: ((openFrame: Record<string, unknown>) => Record<string, unknown>) | undefined;

  constructor(_url?: string, _opts?: unknown) {
    super();
  }

  send(data: string): void {
    const frame = JSON.parse(data) as Record<string, unknown>;
    this.sent.push(frame);
    if (frame.type === 'open' && this.openedReply) {
      const reply = this.openedReply(frame);
      setImmediate(() => this.pushMessage(reply));
    }
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  pushMessage(obj: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(obj), 'utf-8'));
  }
}

function makeSession(ws: FakeWebSocket, keypair: GatewayKeypair, replace: boolean): RemotePairSession {
  return {
    url: `https://api-staging.totalreclaw.xyz/pair/p/${TEST_TOKEN}#pk=xyz`,
    pin: '123456',
    token: TEST_TOKEN,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    keypair,
    mode: 'import',
    _ws: ws as unknown as RemotePairSession['_ws'],
    ...(replace ? { replace: true } : {}),
  };
}

/** Encrypted forward frame, built the way the browser builds it. */
function forwardFrame(gatewayKeypair: GatewayKeypair, extra: Record<string, unknown> = {}) {
  const device = generateGatewayKeypair();
  const { nonceB64, ciphertextB64 } = encryptPairingPayload({
    skLocalB64: device.skB64,
    pkRemoteB64: gatewayKeypair.pkB64,
    sid: TEST_TOKEN,
    plaintext: Buffer.from(TEST_MNEMONIC, 'utf-8'),
  });
  return {
    type: 'forward',
    client_pubkey: device.pkB64,
    nonce: nonceB64,
    ciphertext: ciphertextB64,
    ...extra,
  };
}

/** A session opener that records its options and returns a fake session. */
function recordingOpener(result: RemotePairSession | Error) {
  const calls: OpenRemotePairOptions[] = [];
  const opener = (async (o: OpenRemotePairOptions = {}) => {
    calls.push(o);
    if (result instanceof Error) throw result;
    return { ...result, ...(o.replace ? { replace: true } : {}) };
  }) as typeof openRemotePairSession;
  return { opener, calls };
}

function parse(res: { content: Array<{ text: string }> }): Record<string, unknown> {
  return JSON.parse(res.content[0].text) as Record<string, unknown>;
}

const silentLog = () => undefined;

let tmpDir: string;
let credsPath: string;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-pair-guard-'));
  credsPath = path.join(tmpDir, 'credentials.json');
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Tool-level guard
// ---------------------------------------------------------------------------

describe('handlePair — existing-credentials guard', () => {
  it('refuses when credentials.json exists, opens nothing, leaves the file alone', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const { opener, calls } = recordingOpener(new Error('must not be called'));

    const res = await handlePair({}, { mode: 'generate' }, { credentialsPath: credsPath, openSession: opener });
    const body = parse(res);

    expect(res.isError).toBeUndefined();
    expect(body.already_configured).toBe(true);
    expect(body.message).toBe(ALREADY_CONFIGURED_MESSAGE);
    expect(body.url).toBeUndefined();
    expect(body.pin).toBeUndefined();
    expect(calls).toHaveLength(0);
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
  });

  it('refuses when the server is configured even without a credentials file (env phrase)', async () => {
    const { opener, calls } = recordingOpener(new Error('must not be called'));
    const res = await handlePair({}, {}, {
      credentialsPath: credsPath,
      openSession: opener,
      isConfigured: () => true,
    });
    expect(parse(res).already_configured).toBe(true);
    expect(calls).toHaveLength(0);
    expect(fs.existsSync(credsPath)).toBe(false);
  });

  it.each([
    ['replace as a string', { replace: 'true' }],
    ['replace as 1', { replace: 1 }],
    ['legacy Hermes confirmation args', { replace_confirmed: true, replace_token: 'abc' }],
  ])('only boolean replace=true gets past the guard (%s)', async (_label, args) => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const { opener, calls } = recordingOpener(new Error('must not be called'));
    const res = await handlePair({}, args as Record<string, unknown>, {
      credentialsPath: credsPath,
      openSession: opener,
    });
    expect(parse(res).already_configured).toBe(true);
    expect(calls).toHaveLength(0);
  });

  it('the refusal carries no token or anything the agent could use to confirm', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const res = await handlePair({}, {}, { credentialsPath: credsPath });
    const body = parse(res);
    expect(Object.keys(body).sort()).toEqual(['already_configured', 'message']);
    expect(res.content[0].text).not.toMatch(/token/i);
  });

  it('replace=true on a configured install opens a replace-mode session and tells the user to confirm', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const ws = new FakeWebSocket();
    const { opener, calls } = recordingOpener(makeSession(ws, generateGatewayKeypair(), false));

    // awaitBackground stays false; the background task waits on the fake WS.
    const res = await handlePair({}, { replace: true, mode: 'import' }, {
      credentialsPath: credsPath,
      openSession: opener,
      log: silentLog,
    });
    const body = parse(res);

    expect(calls).toHaveLength(1);
    expect(calls[0].replace).toBe(true);
    expect(body.replace).toBe(true);
    expect(body.url).toBeDefined();
    expect((body.instructions as string[]).join(' ')).toMatch(/replaces the account currently on this agent/);
    expect(res.content[0].text).not.toContain('abandon');
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
    ws.close();
    ws.emit('close', 1000);
  });

  it('replace=true on an unconfigured install is a normal pairing', async () => {
    const ws = new FakeWebSocket();
    const { opener, calls } = recordingOpener(makeSession(ws, generateGatewayKeypair(), false));
    const res = await handlePair({}, { replace: true }, {
      credentialsPath: credsPath,
      openSession: opener,
      log: silentLog,
    });
    expect(calls[0].replace).toBeUndefined();
    expect(parse(res).replace).toBe(false);
    ws.close();
    ws.emit('close', 1000);
  });

  it('fails closed when the relay has no replace mode', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const { opener } = recordingOpener(new ReplaceNotSupportedError());
    const res = await handlePair({}, { replace: true }, { credentialsPath: credsPath, openSession: opener });
    const body = parse(res);
    expect(res.isError).toBe(true);
    expect(body.error).toBe('replace_not_supported');
    expect(body.already_configured).toBe(true);
    expect(body.url).toBeUndefined();
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
    expect(fs.readdirSync(tmpDir)).toEqual(['credentials.json']);
  });

  it('declares the replace flag in the input schema', () => {
    const props = pairToolDefinition.inputSchema.properties as Record<string, { type?: string }>;
    expect(props.replace.type).toBe('boolean');
  });
});

// ---------------------------------------------------------------------------
// Relay wire contract (relay PR #72)
// ---------------------------------------------------------------------------

describe('openRemotePairSession — replace mode handshake', () => {
  function relayWith(reply: (f: Record<string, unknown>) => Record<string, unknown>) {
    let last: FakeWebSocket | undefined;
    class Impl extends FakeWebSocket {
      constructor(url?: string, opts?: unknown) {
        super(url, opts);
        this.openedReply = reply;
        last = this;
      }
    }
    return { Impl: Impl as unknown as OpenRemotePairOptions['webSocketImpl'], ws: () => last! };
  }
  const opened = (extra: Record<string, unknown> = {}) => () => ({
    type: 'opened',
    token: TEST_TOKEN,
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    ...extra,
  });

  it('a normal session never sends replace', async () => {
    const { Impl, ws } = relayWith(opened());
    const s = await openRemotePairSession({ relayBaseUrl: 'wss://relay.test', webSocketImpl: Impl });
    expect(ws().sent[0].type).toBe('open');
    expect('replace' in ws().sent[0]).toBe(false);
    expect(s.replace).toBeUndefined();
  });

  it('a replace session sends replace:true and requires the relay to echo it', async () => {
    const { Impl, ws } = relayWith(opened({ replace: true }));
    const s = await openRemotePairSession({ relayBaseUrl: 'wss://relay.test', webSocketImpl: Impl, replace: true });
    expect(ws().sent[0].replace).toBe(true);
    expect(s.replace).toBe(true);
  });

  it('throws ReplaceNotSupportedError and closes when an older relay does not echo replace', async () => {
    const { Impl, ws } = relayWith(opened());
    await expect(
      openRemotePairSession({ relayBaseUrl: 'wss://relay.test', webSocketImpl: Impl, replace: true }),
    ).rejects.toBeInstanceOf(ReplaceNotSupportedError);
    expect(ws().closed).toBe(true);
  });
});

describe('awaitPhraseUpload — replace confirmation', () => {
  it.each([
    ['absent', {}],
    ['false', { replace_confirmed: false }],
    ['a string', { replace_confirmed: 'true' }],
  ])('nacks replace_not_confirmed before decrypting when the confirmation is %s', async (_l, extra) => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const completePairing = jest.fn();
    const p = awaitPhraseUpload(makeSession(ws, kp, true), { completePairing });
    ws.pushMessage(forwardFrame(kp, extra));
    await expect(p).rejects.toThrow(/not confirmed/);
    expect(ws.sent).toContainEqual({ type: 'nack', error: 'replace_not_confirmed' });
    expect(completePairing).not.toHaveBeenCalled();
  });

  it('proceeds when the relay forwards replace_confirmed:true', async () => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const completePairing = jest.fn(async () => ({ state: 'active' as const }));
    const p = awaitPhraseUpload(makeSession(ws, kp, true), {
      completePairing,
      phraseValidator: () => true,
    });
    ws.pushMessage(forwardFrame(kp, { replace_confirmed: true }));
    await expect(p).resolves.toEqual({ state: 'active' });
    expect(completePairing).toHaveBeenCalledTimes(1);
  });

  it('a normal session ignores replace_confirmed entirely', async () => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const completePairing = jest.fn(async () => ({ state: 'active' as const }));
    const p = awaitPhraseUpload(makeSession(ws, kp, false), {
      completePairing,
      phraseValidator: () => true,
    });
    ws.pushMessage(forwardFrame(kp));
    await expect(p).resolves.toEqual({ state: 'active' });
  });
});

// ---------------------------------------------------------------------------
// Background completion: what lands on disk
// ---------------------------------------------------------------------------

describe('runPairBackgroundTask — credentials on disk', () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    // /v1/register is best-effort; make it fail fast so no network is used.
    global.fetch = jest.fn(async () => {
      throw new Error('offline in tests');
    }) as unknown as typeof fetch;
  });
  afterEach(() => {
    global.fetch = realFetch;
  });

  async function run(session: RemotePairSession, ws: FakeWebSocket, frame: Record<string, unknown>) {
    const task = runPairBackgroundTask({
      session,
      relayBaseUrl: 'wss://relay.test',
      serverUrl: 'https://relay.test',
      hardTimeoutMs: 10_000,
      log: silentLog,
      credentialsPath: credsPath,
    });
    ws.pushMessage(frame);
    await task;
  }

  it('a confirmed replacement keeps the old file (0600) and writes the new one', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    await run(makeSession(ws, kp, true), ws, forwardFrame(kp, { replace_confirmed: true }));

    expect(ws.sent).toContainEqual({ type: 'ack' });
    const kept = fs.readdirSync(tmpDir).filter((f) => f.startsWith('credentials.json.replaced-'));
    expect(kept).toHaveLength(1);
    const keptPath = path.join(tmpDir, kept[0]);
    expect(fs.readFileSync(keptPath, 'utf-8')).toBe(OLD_CREDS);
    expect(fs.statSync(keptPath).mode & 0o777).toBe(0o600);
    const fresh = JSON.parse(fs.readFileSync(credsPath, 'utf-8')) as { mnemonic?: string };
    expect(fresh.mnemonic).toBe(TEST_MNEMONIC);
  });

  it('moves a Hermes credentials.json.bak aside so the new account cannot overwrite it', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    fs.writeFileSync(`${credsPath}.bak`, 'old backup\n', { mode: 0o600 });
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    await run(makeSession(ws, kp, true), ws, forwardFrame(kp, { replace_confirmed: true }));

    expect(fs.existsSync(`${credsPath}.bak`)).toBe(false);
    const bak = fs.readdirSync(tmpDir).filter((f) => f.startsWith('credentials.json.bak.replaced-'));
    expect(bak).toHaveLength(1);
    expect(fs.readFileSync(path.join(tmpDir, bak[0]), 'utf-8')).toBe('old backup\n');
  });

  it('an unconfirmed replacement changes nothing on disk', async () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    await run(makeSession(ws, kp, true), ws, forwardFrame(kp));

    expect(ws.sent).toContainEqual({ type: 'nack', error: 'replace_not_confirmed' });
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
    expect(fs.readdirSync(tmpDir)).toEqual(['credentials.json']);
  });

  it('a normal pairing never overwrites credentials that appeared after the guard ran', async () => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, kp, false);
    // The guard passed (no file). Another client writes credentials meanwhile.
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    await run(session, ws, forwardFrame(kp));

    expect(ws.sent).toContainEqual({ type: 'nack', error: 'already_configured' });
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
  });

  it('a normal first pairing still writes credentials.json at 0600', async () => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    await run(makeSession(ws, kp, false), ws, forwardFrame(kp));

    expect(ws.sent).toContainEqual({ type: 'ack' });
    expect(fs.statSync(credsPath).mode & 0o777).toBe(0o600);
    expect(fs.readdirSync(tmpDir)).toEqual(['credentials.json']);
  });
});

describe('preserveCredentialsBeforeReplace', () => {
  it('copies byte-for-byte with a UTC stamp and is a no-op with no files', () => {
    expect(preserveCredentialsBeforeReplace(credsPath)).toEqual([]);
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o644 });
    const kept = preserveCredentialsBeforeReplace(credsPath, new Date('2026-11-03T01:02:03.456Z'));
    expect(kept).toEqual([`${credsPath}.replaced-20261103T010203Z`]);
    expect(fs.readFileSync(kept[0], 'utf-8')).toBe(OLD_CREDS);
    expect(fs.statSync(kept[0]).mode & 0o777).toBe(0o600);
    // The original stays until the new credentials are written over it.
    expect(fs.readFileSync(credsPath, 'utf-8')).toBe(OLD_CREDS);
  });

  it('throws rather than clobber an existing preserved copy', () => {
    fs.writeFileSync(credsPath, OLD_CREDS, { mode: 0o600 });
    const when = new Date('2026-11-03T01:02:03Z');
    preserveCredentialsBeforeReplace(credsPath, when);
    expect(() => preserveCredentialsBeforeReplace(credsPath, when)).toThrow();
  });
});
