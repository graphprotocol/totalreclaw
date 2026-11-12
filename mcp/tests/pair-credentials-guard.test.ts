/**
 * totalreclaw_pair must never replace an existing ~/.totalreclaw/credentials.json.
 *
 * The file is shared with every TotalReclaw client on the machine (Hermes
 * reads the same path). A Hermes desktop install keeps its phrase in the OS
 * keychain and writes only a marker, which this server can't read, so the
 * server reports "not configured" while a real account is on disk. Before
 * this guard, a generate-mode pairing then silently replaced that account.
 *
 * All files here live in per-test temp dirs and hold fixture data only (the
 * public BIP-39 test vector, a synthetic keychain marker). Nothing reads the
 * real home directory.
 */

import { EventEmitter } from 'node:events';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import {
  handlePair,
  runPairBackgroundTask,
  ALREADY_CONFIGURED_MESSAGE,
  CREDENTIALS_FILE_EXISTS_MESSAGE,
} from '../src/tools/pair';
import type { RemotePairSession } from '../src/pair-remote-client';
import { generateGatewayKeypair, encryptPairingPayload, type GatewayKeypair } from '../src/pair-crypto';

// Public BIP-39 test vector — fixture only, never a real account.
const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_TOKEN = 'guard-token-1234';

/** Shape of Hermes' v1 OS-keychain file (credentials_wrap.py), synthetic values. */
const HERMES_KEYCHAIN_MARKER_FILE = JSON.stringify({
  mnemonic: '__keychain__:v1:0x0000000000000000000000000000000000000001',
  keychain_wrapped: true,
  scope_address: '0x0000000000000000000000000000000000000002',
});

function tmpCredentialsPath(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-pair-guard-')), 'credentials.json');
}

class FakeWebSocket extends EventEmitter {
  readyState = 1;
  sent: Array<Record<string, unknown>> = [];
  closed = false;
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
  }
  terminate(): void {
    this.close();
  }
  pushMessage(obj: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(obj), 'utf-8'));
  }
}

function makeSession(ws: FakeWebSocket, keypair: GatewayKeypair): RemotePairSession {
  return {
    url: 'https://relay.example/pair/p/guard-token-1234#pk=xyz',
    pin: '123456',
    token: TEST_TOKEN,
    expiresAt: new Date(Date.now() + 10 * 60_000).toISOString(),
    keypair,
    mode: 'generate',
    _ws: ws as unknown as RemotePairSession['_ws'],
  };
}

/** Encrypted legacy-mnemonic forward frame, built the way the browser does. */
function phraseForwardFrame(gatewayKeypair: GatewayKeypair) {
  const device = generateGatewayKeypair();
  const { nonceB64, ciphertextB64 } = encryptPairingPayload({
    skLocalB64: device.skB64,
    pkRemoteB64: gatewayKeypair.pkB64,
    sid: TEST_TOKEN,
    plaintext: Buffer.from(TEST_MNEMONIC, 'utf-8'),
  });
  return { type: 'forward', client_pubkey: device.pkB64, nonce: nonceB64, ciphertext: ciphertextB64 };
}

function parse(res: { content: Array<{ text: string }> }): Record<string, unknown> {
  return JSON.parse(res.content[0].text);
}

const silentLog = () => undefined;

describe('handlePair: refuses when credentials already exist', () => {
  it.each(['generate', 'import'])(
    '%s mode: an existing (Hermes keychain) file is refused, no session opened, file untouched',
    async (mode) => {
      const credentialsPath = tmpCredentialsPath();
      fs.writeFileSync(credentialsPath, HERMES_KEYCHAIN_MARKER_FILE, 'utf-8');
      const openSession = jest.fn();

      const res = await handlePair({} as never, { mode }, {
        credentialsPath,
        isConfigured: () => false,
        openSession: openSession as never,
        log: silentLog,
      });

      const body = parse(res);
      expect(body.already_configured).toBe(true);
      expect(body.message).toBe(CREDENTIALS_FILE_EXISTS_MESSAGE);
      expect(body.url).toBeUndefined();
      expect(body.pin).toBeUndefined();
      expect(openSession).not.toHaveBeenCalled();
      expect(fs.readFileSync(credentialsPath, 'utf-8')).toBe(HERMES_KEYCHAIN_MARKER_FILE);
    },
  );

  it('a configured server is refused even with no file on disk (e.g. env phrase)', async () => {
    const credentialsPath = tmpCredentialsPath();
    const openSession = jest.fn();

    const res = await handlePair({} as never, {}, {
      credentialsPath,
      isConfigured: () => true,
      openSession: openSession as never,
      log: silentLog,
    });

    expect(parse(res).already_configured).toBe(true);
    expect(parse(res).message).toBe(ALREADY_CONFIGURED_MESSAGE);
    expect(openSession).not.toHaveBeenCalled();
    expect(fs.existsSync(credentialsPath)).toBe(false);
  });

  it('when startup flagged the file as unusable, the refusal says why', async () => {
    const credentialsPath = tmpCredentialsPath();
    fs.writeFileSync(credentialsPath, HERMES_KEYCHAIN_MARKER_FILE, 'utf-8');
    const openSession = jest.fn();

    const res = await handlePair({} as never, { mode: 'generate' }, {
      credentialsPath,
      isConfigured: () => false,
      getUnusableCredentials: () => ({ reason: 'keychain_wrapped', message: 'Keychain file, cannot read.' }),
      openSession: openSession as never,
      log: silentLog,
    });

    const body = parse(res);
    expect(body.already_configured).toBe(true);
    expect(body.reason).toBe('keychain_wrapped');
    expect(body.message).toMatch(/^Keychain file, cannot read\./);
    expect(body.message).toMatch(/Do not move, rename or delete/);
    expect(openSession).not.toHaveBeenCalled();
  });

  it('a replace_confirmed argument alone never authorises a replacement', async () => {
    const credentialsPath = tmpCredentialsPath();
    fs.writeFileSync(credentialsPath, '{}', 'utf-8');
    const res = await handlePair({} as never, { replace_confirmed: true } as never, {
      credentialsPath,
      isConfigured: () => false,
      openSession: jest.fn() as never,
      log: silentLog,
    });
    const text = res.content[0].text;
    expect(parse(res).already_configured).toBe(true);
    expect(text).not.toMatch(/token/i);
    expect(fs.readFileSync(credentialsPath, 'utf-8')).toBe('{}');
  });

  it('with no file and an unconfigured server, it opens a session as before', async () => {
    const credentialsPath = tmpCredentialsPath();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, generateGatewayKeypair());
    const openSession = jest.fn(async () => session);

    const res = await handlePair({} as never, { mode: 'generate' }, {
      credentialsPath,
      isConfigured: () => false,
      openSession: openSession as never,
      relayBaseUrl: 'wss://relay.example',
      serverUrl: 'https://relay.example',
      log: silentLog,
    });

    expect(openSession).toHaveBeenCalledTimes(1);
    expect(parse(res).url).toBe(session.url);
    expect(parse(res).already_configured).toBeUndefined();
    ws.emit('close', 1000); // end the background wait
  });
});

describe('runPairBackgroundTask: credentials are created exclusively', () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    // /v1/register is best-effort; make it fail fast without network.
    global.fetch = jest.fn(async () => {
      throw new Error('offline in tests');
    }) as unknown as typeof fetch;
  });
  afterEach(() => {
    global.fetch = realFetch;
  });

  it('a file that appears during the browser step is not overwritten; the relay is nacked', async () => {
    const credentialsPath = tmpCredentialsPath();
    const keypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();

    const task = runPairBackgroundTask({
      session: makeSession(ws, keypair),
      relayBaseUrl: 'wss://relay.example',
      serverUrl: 'https://relay.example',
      credentialsPath,
      log: silentLog,
    });

    // Another client writes credentials while the user is on the page.
    fs.writeFileSync(credentialsPath, HERMES_KEYCHAIN_MARKER_FILE, 'utf-8');
    ws.pushMessage(phraseForwardFrame(keypair));
    await task;

    expect(ws.sent).toContainEqual({ type: 'nack', error: 'already_configured' });
    expect(ws.sent).not.toContainEqual({ type: 'ack' });
    expect(fs.readFileSync(credentialsPath, 'utf-8')).toBe(HERMES_KEYCHAIN_MARKER_FILE);
  });

  it('a first pairing still writes credentials.json at mode 0600 and acks', async () => {
    const credentialsPath = tmpCredentialsPath();
    const keypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();

    const task = runPairBackgroundTask({
      session: makeSession(ws, keypair),
      relayBaseUrl: 'wss://relay.example',
      serverUrl: 'https://relay.example',
      credentialsPath,
      log: silentLog,
    });
    ws.pushMessage(phraseForwardFrame(keypair));
    await task;

    expect(ws.sent).toContainEqual({ type: 'ack' });
    const written = JSON.parse(fs.readFileSync(credentialsPath, 'utf-8'));
    expect(written.mnemonic).toBe(TEST_MNEMONIC);
    expect(fs.statSync(credentialsPath).mode & 0o777).toBe(0o600);
  });
});
