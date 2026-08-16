/**
 * Bundle-mode pairing tests for `pair-remote-client.ts` (Option E Phase 2 /
 * #581, P2-13).
 *
 * As documented in the module's header comment, the relay does not yet
 * forward a `payload_type` field on the pair envelope (P2-11, a private-repo
 * change) — so this branch is exercised here against a FIXTURE forward
 * frame (a synthetic WebSocket that emits a hand-crafted, correctly
 * encrypted `{type:"forward", payload_type:"derived-bundle-v1", ...}`
 * message), never live traffic. Mirrors
 * `python/tests/test_pair_completion_bundle.py`'s approach on the Hermes
 * side.
 *
 * The first three tests below build a real bundle via
 * `deriveBundleFromMnemonic`, so they're gated on `hasBundleBindings()`
 * (same pattern as `tests/bundle.test.ts`) — the published
 * `@totalreclaw/core` doesn't carry the bundle WASM bindings yet. The last
 * two tests (unrecognised `payload_type`, legacy-mnemonic) never touch a
 * bundle and run unconditionally.
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
import { deriveBundleFromMnemonic, hasBundleBindings, type DerivedBundleV1 } from '../src/subgraph/bundle.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250';
const TEST_TOKEN = 'test-token-1234';

const bindingsAvailable = hasBundleBindings();
const maybeIt = bindingsAvailable ? it : it.skip;
const skipSuffix = bindingsAvailable
  ? ''
  : ' — SKIPPED: installed @totalreclaw/core lacks bundle bindings (see subgraph/bundle.ts)';

/**
 * A minimal fake WebSocket sufficient for `awaitPhraseUpload`'s needs:
 * `.on`/`.off` (EventEmitter), `.send`, `.close`, `.readyState`. Captures
 * every `.send()` call so tests can assert ack/nack behaviour.
 */
class FakeWebSocket extends EventEmitter {
  static readonly OPEN = 1;
  static readonly CONNECTING = 0;
  readyState = FakeWebSocket.OPEN;
  sent: unknown[] = [];
  closed = false;

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(): void {
    this.closed = true;
    this.readyState = 3; // CLOSED
  }

  /** Simulate the relay pushing a message frame. */
  pushMessage(obj: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(obj), 'utf-8'));
  }
}

function makeSession(ws: FakeWebSocket, keypair: GatewayKeypair): RemotePairSession {
  return {
    url: 'https://api-staging.totalreclaw.xyz/pair/p/test-token-1234#pk=xyz',
    pin: '123456',
    token: TEST_TOKEN,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    keypair,
    mode: 'generate',
    _ws: ws as unknown as RemotePairSession['_ws'],
  };
}

/** Build a valid encrypted forward frame carrying a bundle JSON payload,
 * exactly the way a browser device would (device keypair + ECDH vs the
 * gateway's public key + AES-256-GCM). */
function buildBundleForwardFrame(gatewayKeypair: GatewayKeypair, bundleJson: string) {
  const deviceKeypair = generateGatewayKeypair();
  const { nonceB64, ciphertextB64 } = encryptPairingPayload({
    skLocalB64: deviceKeypair.skB64,
    pkRemoteB64: gatewayKeypair.pkB64,
    sid: TEST_TOKEN,
    plaintext: Buffer.from(bundleJson, 'utf-8'),
  });
  return {
    type: 'forward',
    payload_type: 'derived-bundle-v1',
    client_pubkey: deviceKeypair.pkB64,
    nonce: nonceB64,
    ciphertext: ciphertextB64,
    mode: 'generate',
  };
}

describe('awaitPhraseUpload — payload_type: derived-bundle-v1 (Option E Phase 2 / #581, P2-13)', () => {
  maybeIt('decrypts + validates a bundle payload and hands it to completePairingBundle' + skipSuffix, async () => {
    const gatewayKeypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, gatewayKeypair);

    const bundleJson = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    const frame = buildBundleForwardFrame(gatewayKeypair, bundleJson);

    let receivedBundle: DerivedBundleV1 | undefined;
    const completePairingBundle = jest.fn(async ({ bundle }: { bundle: DerivedBundleV1 }) => {
      receivedBundle = bundle;
      return { state: 'active' } as RelayCompletionResult;
    });
    const completePairing = jest.fn();

    const resultPromise = awaitPhraseUpload(session, {
      completePairing,
      completePairingBundle,
    });

    ws.pushMessage(frame);
    const result = await resultPromise;

    expect(result.state).toBe('active');
    expect(completePairingBundle).toHaveBeenCalledTimes(1);
    expect(completePairing).not.toHaveBeenCalled();
    expect(receivedBundle?.account.smart_account.toLowerCase()).toBe(TEST_SMART_ACCOUNT.toLowerCase());
    expect(receivedBundle?.signing.kind).toBe('owner-eoa');

    // Acks the relay on success.
    expect(ws.sent).toContainEqual({ type: 'ack' });
    expect(ws.closed).toBe(true);
  });

  // No `maybeIt`/bindings gate here (#618 adversarial-review NIT): the
  // handler-existence check in pair-remote-client.ts now runs BEFORE
  // parseBundleV1, so this path never touches a bundle binding at all — an
  // arbitrary plaintext payload proves the point (if parseBundleV1 ran
  // first, non-bundle plaintext would fail validation and this test would
  // need a real bundle + bindings, defeating the purpose of the reorder).
  it('nacks unsupported_payload_type — checked BEFORE parseBundleV1, never invalid_bundle — when the call site has no completePairingBundle handler', async () => {
    const gatewayKeypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, gatewayKeypair);

    // Deliberately NOT a valid (or even parseable-as-JSON) bundle — proves
    // the handler-existence check fires without ever calling parseBundleV1.
    const frame = buildBundleForwardFrame(gatewayKeypair, 'not even json, let alone a bundle');

    const resultPromise = awaitPhraseUpload(session, {
      completePairing: jest.fn(),
      // no completePairingBundle — this call site (e.g. an older totalreclaw_pair
      // wired to the legacy-only handler) only understands phrases.
    });

    ws.pushMessage(frame);
    await expect(resultPromise).rejects.toThrow(/completePairingBundle/);
    expect(ws.sent).toContainEqual({ type: 'nack', error: 'unsupported_payload_type' });
    // The specific negative this NIT fixed: never invalid_bundle here.
    expect(ws.sent).not.toContainEqual({ type: 'nack', error: 'invalid_bundle' });
  });

  maybeIt('nacks invalid_bundle and rejects when the decrypted payload fails parseBundleV1 validation' + skipSuffix, async () => {
    const gatewayKeypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, gatewayKeypair);

    const bundleJson = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    const corrupted = JSON.parse(bundleJson);
    corrupted.vault.auth_key = 'not-hex';
    const frame = buildBundleForwardFrame(gatewayKeypair, JSON.stringify(corrupted));

    const completePairingBundle = jest.fn();
    const resultPromise = awaitPhraseUpload(session, {
      completePairing: jest.fn(),
      completePairingBundle,
    });

    ws.pushMessage(frame);
    await expect(resultPromise).rejects.toThrow();
    expect(completePairingBundle).not.toHaveBeenCalled();
    expect(ws.sent).toContainEqual({ type: 'nack', error: 'invalid_bundle' });
  });

  it('rejects an unrecognised payload_type loudly (never silently treated as legacy-mnemonic)', async () => {
    const gatewayKeypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, gatewayKeypair);

    const deviceKeypair = generateGatewayKeypair();
    const { nonceB64, ciphertextB64 } = encryptPairingPayload({
      skLocalB64: deviceKeypair.skB64,
      pkRemoteB64: gatewayKeypair.pkB64,
      sid: TEST_TOKEN,
      plaintext: Buffer.from('whatever', 'utf-8'),
    });

    const resultPromise = awaitPhraseUpload(session, {
      completePairing: jest.fn(),
      completePairingBundle: jest.fn(),
    });

    ws.pushMessage({
      type: 'forward',
      payload_type: 'some-future-payload-type-v7',
      client_pubkey: deviceKeypair.pkB64,
      nonce: nonceB64,
      ciphertext: ciphertextB64,
    });

    await expect(resultPromise).rejects.toThrow(/payload_type/);
    expect(ws.sent).toContainEqual({ type: 'nack', error: 'unknown_payload_type' });
  });

  it('legacy-mnemonic (payload_type absent) is unaffected — existing phrase flow still works', async () => {
    const gatewayKeypair = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const session = makeSession(ws, gatewayKeypair);

    const deviceKeypair = generateGatewayKeypair();
    const { nonceB64, ciphertextB64 } = encryptPairingPayload({
      skLocalB64: deviceKeypair.skB64,
      pkRemoteB64: gatewayKeypair.pkB64,
      sid: TEST_TOKEN,
      plaintext: Buffer.from(TEST_MNEMONIC, 'utf-8'),
    });

    const completePairing = jest.fn(async () => ({ state: 'active' } as RelayCompletionResult));
    const completePairingBundle = jest.fn();

    const resultPromise = awaitPhraseUpload(session, {
      completePairing,
      completePairingBundle,
      phraseValidator: (p) => p.split(' ').length === 12,
    });

    ws.pushMessage({
      type: 'forward',
      // No payload_type field — the current, only shipped shape.
      client_pubkey: deviceKeypair.pkB64,
      nonce: nonceB64,
      ciphertext: ciphertextB64,
    });

    const result = await resultPromise;
    expect(result.state).toBe('active');
    expect(completePairing).toHaveBeenCalledWith(
      expect.objectContaining({ mnemonic: TEST_MNEMONIC }),
    );
    expect(completePairingBundle).not.toHaveBeenCalled();
  });
});
