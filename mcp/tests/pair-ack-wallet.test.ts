/**
 * totalreclaw-relay#79 — the pair ack carries the paired Smart Account
 * (`wallet_address`) so the relay can show the account's plan on the pair
 * page's "You're all set" screen. Mirrors python/tests/test_pair_remote_client.py.
 *
 * Invariants:
 *   - address only on a successful (`active`) completion;
 *   - lookup runs in parallel with completion and is bounded;
 *   - unknown / malformed / failing lookup → the plain `{ type: 'ack' }`;
 *   - the bundle path uses `bundle.account.smart_account` (no RPC);
 *   - the default resolver never falls back (e.g. to the EOA).
 */
import { EventEmitter } from 'node:events';

import {
  awaitPhraseUpload,
  type RemotePairSession,
  type RelayCompletionResult,
} from '../src/pair-remote-client.js';
import {
  generateGatewayKeypair,
  encryptPairingPayload,
  type GatewayKeypair,
} from '../src/pair-crypto.js';
import { deriveBundleFromMnemonic, hasBundleBindings } from '../src/subgraph/bundle.js';
import * as smartAccount from '../src/smart-account-address.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250';
const SA = '0xAbCdEf0123456789aBcDeF0123456789AbCdEf01';
const TEST_TOKEN = 'test-token-wallet';

class FakeWebSocket extends EventEmitter {
  static readonly OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  sent: any[] = [];
  closed = false;
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
  }
  pushMessage(obj: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(obj), 'utf-8'));
  }
}

function makeSession(ws: FakeWebSocket, keypair: GatewayKeypair): RemotePairSession {
  return {
    url: 'https://relay.test/pair/p/test-token-wallet#pk=xyz',
    pin: '123456',
    token: TEST_TOKEN,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    keypair,
    mode: 'import',
    _ws: ws as unknown as RemotePairSession['_ws'],
  };
}

function forwardFrame(gw: GatewayKeypair, plaintext: string, payloadType?: string) {
  const device = generateGatewayKeypair();
  const { nonceB64, ciphertextB64 } = encryptPairingPayload({
    skLocalB64: device.skB64,
    pkRemoteB64: gw.pkB64,
    sid: TEST_TOKEN,
    plaintext: Buffer.from(plaintext, 'utf-8'),
  });
  return {
    type: 'forward',
    client_pubkey: device.pkB64,
    nonce: nonceB64,
    ciphertext: ciphertextB64,
    mode: 'import',
    ...(payloadType ? { payload_type: payloadType } : {}),
  };
}

type Opts = Partial<Parameters<typeof awaitPhraseUpload>[1]>;

async function run(opts: Opts, plaintext = TEST_MNEMONIC, payloadType?: string) {
  const gw = generateGatewayKeypair();
  const ws = new FakeWebSocket();
  const session = makeSession(ws, gw);
  const p = awaitPhraseUpload(session, {
    completePairing: async () => ({ state: 'active' }) as RelayCompletionResult,
    ...opts,
  });
  ws.pushMessage(forwardFrame(gw, plaintext, payloadType));
  const result = await p;
  return { ws, result };
}

describe('pair ack wallet_address (totalreclaw-relay#79)', () => {
  it('carries the resolved address, lowercased', async () => {
    const resolver = jest.fn(async () => SA);
    const { ws, result } = await run({ resolveWalletAddress: resolver });
    expect(resolver).toHaveBeenCalledWith(TEST_MNEMONIC);
    expect(result.state).toBe('active');
    expect(ws.sent).toEqual([{ type: 'ack', wallet_address: SA.toLowerCase() }]);
  });

  it('without a resolver the ack is unchanged', async () => {
    const { ws } = await run({});
    expect(ws.sent).toEqual([{ type: 'ack' }]);
  });

  it.each([undefined, '', '0x1234', 'not-an-address', SA + '00', SA + '\n', 42 as any])(
    'unknown / malformed address %p → plain ack',
    async (answer) => {
      const { ws } = await run({ resolveWalletAddress: async () => answer });
      expect(ws.sent).toEqual([{ type: 'ack' }]);
    },
  );

  it('resolver rejection → plain ack, completion still active', async () => {
    const { ws, result } = await run({
      resolveWalletAddress: async () => {
        throw new Error('rpc down');
      },
    });
    expect(result.state).toBe('active');
    expect(ws.sent).toEqual([{ type: 'ack' }]);
  });

  it('resolver throwing synchronously → plain ack', async () => {
    const { ws } = await run({
      resolveWalletAddress: (() => {
        throw new Error('sync boom');
      }) as any,
    });
    expect(ws.sent).toEqual([{ type: 'ack' }]);
  });

  it('a slow resolver is bounded by walletTimeoutMs', async () => {
    const t0 = Date.now();
    const { ws } = await run({
      resolveWalletAddress: () => new Promise<string>(() => {}),
      walletTimeoutMs: 150,
    });
    expect(Date.now() - t0).toBeLessThan(1500);
    expect(ws.sent).toEqual([{ type: 'ack' }]);
  });

  it('runs in parallel with completion, not after it', async () => {
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
    const t0 = Date.now();
    const { ws } = await run({
      resolveWalletAddress: async () => {
        await sleep(400);
        return SA;
      },
      completePairing: async () => {
        await sleep(400);
        return { state: 'active' };
      },
    });
    // Sequential would be >= 800ms.
    expect(Date.now() - t0).toBeLessThan(700);
    expect(ws.sent).toEqual([{ type: 'ack', wallet_address: SA.toLowerCase() }]);
  });

  it('an inactive completion nacks and never reports the address', async () => {
    const { ws, result } = await run({
      resolveWalletAddress: async () => SA,
      completePairing: async () => ({ state: 'error', error: 'credentials_write_failed' }),
    });
    expect(result.state).toBe('error');
    expect(ws.sent).toEqual([{ type: 'nack', error: 'credentials_write_failed' }]);
  });

  it.each([
    undefined,
    '',
    "EACCES: permission denied, open '/home/alice/.totalreclaw/credentials.json'",
    'A'.repeat(10),
    'x'.repeat(65),
  ])('a failed completion never forwards free-text error %p', async (error) => {
    const { ws } = await run({
      completePairing: async () => ({ state: 'error', error }) as RelayCompletionResult,
    });
    expect(ws.sent).toEqual([{ type: 'nack', error: 'completion_failed' }]);
  });

  it('a completion that throws nacks without the address', async () => {
    const gw = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const p = awaitPhraseUpload(makeSession(ws, gw), {
      completePairing: async () => {
        throw new Error('disk full');
      },
      resolveWalletAddress: async () => SA,
    });
    ws.pushMessage(forwardFrame(gw, TEST_MNEMONIC));
    await expect(p).rejects.toThrow('disk full');
    expect(ws.sent).toEqual([{ type: 'nack', error: 'completion_failed' }]);
  });

  it('an invalid phrase never reaches the resolver', async () => {
    const resolver = jest.fn(async () => SA);
    const gw = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const p = awaitPhraseUpload(makeSession(ws, gw), {
      completePairing: async () => ({ state: 'active' }),
      resolveWalletAddress: resolver,
    });
    ws.pushMessage(forwardFrame(gw, 'foo bar baz'));
    await expect(p).rejects.toThrow();
    expect(resolver).not.toHaveBeenCalled();
    expect(ws.sent).toEqual([{ type: 'nack', error: 'invalid_mnemonic' }]);
  });

  const maybeIt = hasBundleBindings() ? it : it.skip;
  maybeIt('bundle path reports bundle.account.smart_account with no resolver', async () => {
    const bundleJson = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    const resolver = jest.fn(async () => SA);
    const { ws } = await run(
      {
        resolveWalletAddress: resolver,
        completePairingBundle: async () => ({ state: 'active' }),
      },
      bundleJson,
      'derived-bundle-v1',
    );
    expect(resolver).not.toHaveBeenCalled();
    expect(ws.sent).toEqual([{ type: 'ack', wallet_address: TEST_SMART_ACCOUNT.toLowerCase() }]);
  });
});

describe('resolvePairedSmartAccount', () => {
  /** Load the real module with `toSimpleSmartAccount` replaced — no RPC. */
  function loadWith(impl: () => Promise<{ address: string }>): typeof smartAccount {
    let mod: typeof smartAccount | undefined;
    jest.isolateModules(() => {
      jest.doMock('permissionless/accounts', () => ({ toSimpleSmartAccount: jest.fn(impl) }));
      mod = jest.requireActual('../src/smart-account-address');
    });
    return mod!;
  }

  it('is stubbed for every test by the global setup (no RPC)', async () => {
    expect(await smartAccount.resolvePairedSmartAccount(TEST_MNEMONIC)).toBeUndefined();
  });

  it('returns the derived Smart Account, lowercased', async () => {
    const mod = loadWith(async () => ({ address: TEST_SMART_ACCOUNT }));
    expect(await mod.resolvePairedSmartAccount(TEST_MNEMONIC)).toBe(TEST_SMART_ACCOUNT.toLowerCase());
  });

  it('never falls back: a derivation failure resolves undefined', async () => {
    const mod = loadWith(async () => {
      throw new Error('rpc unreachable');
    });
    expect(await mod.resolvePairedSmartAccount(TEST_MNEMONIC)).toBeUndefined();
    await expect(mod.deriveSmartAccountAddress(TEST_MNEMONIC)).rejects.toThrow('rpc unreachable');
  });
});
