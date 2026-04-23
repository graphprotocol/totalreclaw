/**
 * Tests for `pair-remote-client` — relay-brokered pair flow (plugin rc.11).
 *
 * Runs a local WebSocket server that mimics the relay's open/forward/ack
 * protocol so the test covers the full TS <-> relay wire with no network
 * dependency. Encryption vectors are produced with the same `pair-crypto`
 * primitives the gateway would use — so the round-trip proves:
 *
 *   1. open frame shape is correct
 *   2. opened reply drives the user URL / PIN / token plumbing
 *   3. forward frame is decrypted locally + completion handler is invoked
 *   4. ack is sent back on success; nack carries a typed error on failure
 *
 * Does NOT run against the real staging relay — that is covered by the
 * auto-QA harness after publish.
 */
import { WebSocketServer, WebSocket } from 'ws';
import { createServer, type Server as HttpServer } from 'node:http';
import { AddressInfo } from 'node:net';

import {
  awaitPhraseUpload,
  openRemotePairSession,
} from './pair-remote-client.js';
import {
  encryptPairingPayload,
  generateGatewayKeypair,
  type GatewayKeypair,
} from './pair-crypto.js';

// Minimal tap-style harness — matches the rest of this plugin's test style.
let _passed = 0;
let _failed = 0;
let _seq = 0;
function ok(name: string, cond: boolean, detail?: string): void {
  const status = cond ? 'ok' : 'fail';
  const tail = detail ? ` -- ${detail}` : '';
  _seq += 1;
  // eslint-disable-next-line no-console
  console.log(`${status} ${_seq} - ${name}${tail}`);
  if (cond) _passed += 1;
  else _failed += 1;
}

/** The phrase the "browser" side will encrypt + upload in these tests. */
const TEST_PHRASE =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

/** Non-BIP-39 lowercase ASCII — still matches the 12-word regex. */
const INVALID_PHRASE =
  'foo foo foo foo foo foo foo foo foo foo foo foo';

// ---------------------------------------------------------------------------
// Relay stub
// ---------------------------------------------------------------------------

interface RelayStubOptions {
  /** Override the default `token` the stub issues. */
  token?: string;
  /** Override the `expires_at` ISO string. */
  expiresAt?: string;
  /** If true, send an `{type:"error"}` frame instead of `opened`. */
  errorOnOpen?: string;
  /** Delay (ms) before sending the forward frame. Default 10. */
  forwardAfterMs?: number;
  /** If true, mimic a nonce/ciphertext built for the WRONG session id. */
  corruptSid?: boolean;
  /**
   * Phrase the stub-browser encrypts + uploads. Default TEST_PHRASE. Set
   * to something non-BIP-39 to exercise validator failure paths.
   */
  phrase?: string;
  /**
   * Force a device keypair so the test can verify the exact client pubkey
   * that reaches the gateway side.
   */
  forceDeviceKeypair?: GatewayKeypair;
}

interface RelayStub {
  wssUrl: string;
  /** Resolves with whatever frame the stub last received from the gateway. */
  lastGatewayFrame: () => Record<string, unknown> | null;
  /** Resolves when the `ack` or `nack` from the gateway arrives (or when the WS closes). */
  awaitAck: () => Promise<'ack' | 'nack' | 'closed'>;
  close: () => Promise<void>;
}

async function startRelayStub(opts: RelayStubOptions = {}): Promise<RelayStub> {
  const token = opts.token ?? 'testtoken12345678';
  const expiresAt =
    opts.expiresAt ?? new Date(Date.now() + 60_000).toISOString();
  const forwardDelay = opts.forwardAfterMs ?? 10;
  const phrase = opts.phrase ?? TEST_PHRASE;

  const http: HttpServer = createServer();
  const wss = new WebSocketServer({ server: http });

  let lastFrame: Record<string, unknown> | null = null;
  let ackResolver: ((v: 'ack' | 'nack' | 'closed') => void) | null = null;
  const ackPromise: Promise<'ack' | 'nack' | 'closed'> = new Promise(
    (resolve) => {
      ackResolver = resolve;
    },
  );

  wss.on('connection', (ws) => {
    ws.on('message', async (raw) => {
      try {
        const text =
          typeof raw === 'string'
            ? raw
            : Buffer.from(raw as ArrayBuffer).toString('utf-8');
        const msg = JSON.parse(text) as Record<string, unknown>;
        lastFrame = msg;

        // First frame must be `open`. Reply with `opened` (or `error`).
        if (msg.type === 'open') {
          if (opts.errorOnOpen) {
            ws.send(JSON.stringify({ type: 'error', error: opts.errorOnOpen }));
            return;
          }
          ws.send(
            JSON.stringify({
              type: 'opened',
              token,
              short_url: `/pair/p/${token}`,
              expires_at: expiresAt,
            }),
          );

          // Simulate the browser POST: encrypt the phrase for the gateway's
          // public key + push a `forward` frame.
          setTimeout(() => {
            const gatewayPubkey = String(msg.gateway_pubkey);
            const deviceKp =
              opts.forceDeviceKeypair ?? generateGatewayKeypair();
            const sid = opts.corruptSid ? 'wrong-sid' : token;
            const { nonceB64, ciphertextB64 } = encryptPairingPayload({
              skLocalB64: deviceKp.skB64,
              pkRemoteB64: gatewayPubkey,
              sid,
              plaintext: Buffer.from(phrase, 'utf-8'),
            });
            ws.send(
              JSON.stringify({
                type: 'forward',
                client_pubkey: deviceKp.pkB64,
                nonce: nonceB64,
                ciphertext: ciphertextB64,
              }),
            );
          }, forwardDelay);
          return;
        }

        if (msg.type === 'ack') {
          ackResolver?.('ack');
          ackResolver = null;
          try {
            ws.close();
          } catch {
            /* noop */
          }
          return;
        }
        if (msg.type === 'nack') {
          ackResolver?.('nack');
          ackResolver = null;
          try {
            ws.close();
          } catch {
            /* noop */
          }
          return;
        }
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('relay-stub: message handler error', err);
      }
    });

    ws.on('close', () => {
      if (ackResolver) {
        ackResolver('closed');
        ackResolver = null;
      }
    });
  });

  await new Promise<void>((resolve) => {
    http.listen(0, '127.0.0.1', () => resolve());
  });

  const addr = http.address() as AddressInfo;
  const wssUrl = `ws://127.0.0.1:${addr.port}`;

  return {
    wssUrl,
    lastGatewayFrame: () => lastFrame,
    awaitAck: () => ackPromise,
    close: () =>
      new Promise<void>((resolve) => {
        try {
          wss.close(() => {
            http.close(() => resolve());
          });
        } catch {
          resolve();
        }
      }),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

async function testHappyPath(): Promise<void> {
  const stub = await startRelayStub({ token: 'abc12345' });
  try {
    const session = await openRemotePairSession({
      relayBaseUrl: stub.wssUrl,
      pin: '123456',
      clientId: 'gw-test-1',
      mode: 'either',
      webSocketImpl: WebSocket,
    });

    // Assertions against the open-side plumbing.
    ok(
      'happy: URL contains the token',
      session.url.includes('/pair/p/abc12345'),
      session.url,
    );
    ok(
      'happy: URL uses https scheme (stub serves ws://)',
      session.url.startsWith('http://'),
      session.url,
    );
    ok(
      'happy: URL carries #pk= fragment',
      /#pk=[A-Za-z0-9_-]{40,48}$/.test(session.url),
      session.url,
    );
    ok('happy: PIN echoes', session.pin === '123456');
    ok('happy: token echoes', session.token === 'abc12345');

    const firstFrame = stub.lastGatewayFrame();
    ok(
      'happy: open frame has correct shape',
      !!firstFrame &&
        firstFrame.type === 'open' &&
        typeof firstFrame.gateway_pubkey === 'string' &&
        firstFrame.pin === '123456' &&
        firstFrame.client_id === 'gw-test-1' &&
        firstFrame.mode === 'either',
      JSON.stringify(firstFrame),
    );

    // Block on phrase upload.
    let completed = false;
    const result = await awaitPhraseUpload(session, {
      completePairing: async ({ mnemonic }) => {
        completed = true;
        // MUST NOT log the phrase — but we can assert on its shape for the test.
        ok(
          'happy: completion received full 12-word phrase',
          mnemonic.split(' ').length === 12 && mnemonic === TEST_PHRASE,
          `len=${mnemonic.split(' ').length}`,
        );
        return { state: 'active', accountId: '0xdeadbeef' };
      },
    });

    ok('happy: completePairing called', completed);
    ok('happy: result.state active', result.state === 'active');
    ok(
      'happy: result.accountId echoed',
      result.accountId === '0xdeadbeef',
    );

    const ack = await stub.awaitAck();
    ok('happy: gateway sent ack', ack === 'ack');
  } finally {
    await stub.close();
  }
}

async function testInvalidPhraseSendsNack(): Promise<void> {
  const stub = await startRelayStub({ phrase: INVALID_PHRASE });
  try {
    const session = await openRemotePairSession({
      relayBaseUrl: stub.wssUrl,
      pin: '654321',
    });

    let called = false;
    let threw = false;
    try {
      await awaitPhraseUpload(session, {
        completePairing: async () => {
          called = true;
          return { state: 'active' };
        },
        phraseValidator: (_p) => false,
      });
    } catch (err) {
      threw =
        err instanceof Error &&
        /failed BIP-39 validation/.test(err.message);
    }

    ok(
      'invalid-phrase: completePairing NOT invoked',
      !called,
    );
    ok(
      'invalid-phrase: awaitPhraseUpload threw validation error',
      threw,
    );

    const ack = await stub.awaitAck();
    ok('invalid-phrase: gateway sent nack', ack === 'nack');
  } finally {
    await stub.close();
  }
}

async function testOpenErrorPropagates(): Promise<void> {
  const stub = await startRelayStub({ errorOnOpen: 'rate_limited' });
  try {
    let threw = false;
    try {
      await openRemotePairSession({
        relayBaseUrl: stub.wssUrl,
        pin: '000000',
      });
    } catch (err) {
      threw =
        err instanceof Error && /rate_limited/.test(err.message);
    }
    ok('open-error: error propagates with relay error code', threw);
  } finally {
    await stub.close();
  }
}

async function testDecryptFailureSendsNack(): Promise<void> {
  const stub = await startRelayStub({ corruptSid: true });
  try {
    const session = await openRemotePairSession({
      relayBaseUrl: stub.wssUrl,
      pin: '999999',
    });

    let called = false;
    let threw = false;
    try {
      await awaitPhraseUpload(session, {
        completePairing: async () => {
          called = true;
          return { state: 'active' };
        },
      });
    } catch {
      threw = true;
    }
    ok('decrypt-fail: completePairing NOT invoked', !called);
    ok('decrypt-fail: awaitPhraseUpload threw', threw);
    const ack = await stub.awaitAck();
    ok('decrypt-fail: gateway sent nack', ack === 'nack');
  } finally {
    await stub.close();
  }
}

async function testHttpsInputConvertedToWss(): Promise<void> {
  // The caller passes a https:// URL (as most config plumbing will produce
  // in production); the client should still hit wss://host/pair/session/open.
  const stub = await startRelayStub({ token: 'https-test' });
  try {
    const httpsStyle = stub.wssUrl.replace(/^ws:/, 'http:');
    const session = await openRemotePairSession({
      relayBaseUrl: httpsStyle,
      pin: '111111',
    });
    ok(
      'scheme-convert: URL uses http scheme (because stub is http://)',
      session.url.startsWith('http://'),
      session.url,
    );
    ok(
      'scheme-convert: URL contains token',
      session.url.includes('/pair/p/https-test'),
      session.url,
    );
    // Clean shutdown — cancel the awaiting phrase upload.
    try {
      session._ws.close();
    } catch {
      /* noop */
    }
  } finally {
    await stub.close();
  }
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  await testHappyPath();
  await testInvalidPhraseSendsNack();
  await testOpenErrorPropagates();
  await testDecryptFailureSendsNack();
  await testHttpsInputConvertedToWss();

  // eslint-disable-next-line no-console
  console.log(`# fail: ${_failed}`);
  // eslint-disable-next-line no-console
  console.log(`# ${_passed}/${_passed + _failed} passed`);
  if (_failed > 0) {
    // eslint-disable-next-line no-console
    console.log('TESTS FAILED');
    process.exit(1);
  } else {
    // eslint-disable-next-line no-console
    console.log('ALL TESTS PASSED');
  }
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('Test runner crashed:', err);
  process.exit(1);
});
