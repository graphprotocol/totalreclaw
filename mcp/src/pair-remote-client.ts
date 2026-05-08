/**
 * pair-remote-client — gateway-side WebSocket client for the relay-brokered
 * pair flow.
 *
 * Ported from the plugin's `skill/plugin/pair-remote-client.ts` (TotalReclaw
 * plugin v3.3.1-rc.11+) so the MCP server's `totalreclaw_pair` tool emits the
 * same URL/PIN format the browser pair-page already understands.
 *
 * Flow (gateway side):
 *   1. Generate ephemeral x25519 keypair.
 *   2. Open WS to `wss://<relay>/pair/session/open`.
 *   3. Send `{type:"open", gateway_pubkey, pin, client_id, mode}`.
 *   4. Receive `{type:"opened", token, expires_at}` — build the user URL.
 *   5. Block on the WS until the relay pushes `{type:"forward", client_pubkey, nonce, ciphertext}`.
 *   6. Decrypt locally via x25519 ECDH + AES-256-GCM.
 *   7. Hand the decrypted phrase to the caller's completePairing handler
 *      (writes credentials.json) and ack/nack the relay.
 *
 * Phrase-safety:
 *   - Relay sees only ciphertext.
 *   - Phrase NEVER touches logs / stdout / agent context.
 *   - PIN is on the tool return payload (required for the user to type into
 *     the browser) but is never logged.
 *
 * Scope:
 *   - No `fs.*` calls — caller passes a completePairing handler.
 *   - No env-var reads — caller passes the relay base URL.
 *
 * See pair-crypto.ts for the cipher-suite header comment.
 */

import { randomBytes, randomInt } from 'node:crypto';

import WebSocket from 'ws';

import {
  decryptPairingPayload,
  generateGatewayKeypair,
  type GatewayKeypair,
} from './pair-crypto.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const DEFAULT_RELAY_URL = 'wss://api.totalreclaw.xyz';

const OPEN_TIMEOUT_MS = 10_000;

const DEFAULT_AWAIT_TIMEOUT_MS = 5 * 60_000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type PairRelayMode = 'generate' | 'import' | 'either';

export interface RemotePairSession {
  url: string;
  pin: string;
  token: string;
  expiresAt: string;
  keypair: GatewayKeypair;
  mode: PairRelayMode;
  _ws: WebSocket;
}

export interface RelayCompletionResult {
  state: 'active' | 'error';
  accountId?: string;
  error?: string;
}

export type RelayCompletePairingHandler = (inputs: {
  mnemonic: string;
  session: RemotePairSession;
}) => Promise<RelayCompletionResult>;

export type PhraseValidator = (phrase: string) => boolean;

function defaultBip39CountValidator(phrase: string): boolean {
  const words = phrase.split(' ');
  if (words.length !== 12 && words.length !== 24) return false;
  return words.every((w) => /^[a-z]+$/.test(w));
}

function defaultPin(): string {
  const n = randomInt(0, 1_000_000);
  return n.toString(10).padStart(6, '0');
}

function defaultClientId(): string {
  return 'mcp-' + randomBytes(8).toString('hex');
}

function buildUserUrl(relayBase: string, token: string, pkB64: string): string {
  let httpBase = relayBase;
  if (httpBase.startsWith('wss://')) {
    httpBase = 'https://' + httpBase.slice('wss://'.length);
  } else if (httpBase.startsWith('ws://')) {
    httpBase = 'http://' + httpBase.slice('ws://'.length);
  }
  return `${httpBase}/pair/p/${token}#pk=${pkB64}`;
}

function wsConnectBase(relayBase: string): string {
  let base = relayBase.replace(/\/+$/, '');
  if (base.startsWith('https://')) {
    base = 'wss://' + base.slice('https://'.length);
  } else if (base.startsWith('http://')) {
    base = 'ws://' + base.slice('http://'.length);
  }
  return base;
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface OpenRemotePairOptions {
  relayBaseUrl?: string;
  pin?: string;
  clientId?: string;
  mode?: PairRelayMode;
  keypair?: GatewayKeypair;
  webSocketImpl?: typeof WebSocket;
  now?: () => number;
}

export interface AwaitPhraseUploadOptions {
  completePairing: RelayCompletePairingHandler;
  phraseValidator?: PhraseValidator;
  timeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Open
// ---------------------------------------------------------------------------

export async function openRemotePairSession(
  opts: OpenRemotePairOptions = {},
): Promise<RemotePairSession> {
  const relayBase = (opts.relayBaseUrl ?? DEFAULT_RELAY_URL).replace(/\/+$/, '');
  const wsBase = wsConnectBase(relayBase);
  const wsUrl = `${wsBase}/pair/session/open`;
  const WebSocketImpl = opts.webSocketImpl ?? WebSocket;
  const keypair = opts.keypair ?? generateGatewayKeypair();
  const pin = opts.pin ?? defaultPin();
  const clientId = opts.clientId ?? defaultClientId();
  const mode: PairRelayMode = opts.mode ?? 'either';

  const ws: WebSocket = new WebSocketImpl(wsUrl, {
    handshakeTimeout: OPEN_TIMEOUT_MS,
  });

  try {
    await waitOpen(ws, OPEN_TIMEOUT_MS);
  } catch (err) {
    safeClose(ws);
    throw err;
  }

  try {
    ws.send(
      JSON.stringify({
        type: 'open',
        gateway_pubkey: keypair.pkB64,
        pin,
        client_id: clientId,
        mode,
      }),
    );
  } catch (err) {
    safeClose(ws);
    throw err instanceof Error ? err : new Error(String(err));
  }

  let raw: Buffer | ArrayBuffer | string;
  try {
    raw = await waitNextMessage(ws, OPEN_TIMEOUT_MS);
  } catch (err) {
    safeClose(ws);
    throw err;
  }

  let msg: { type?: string; [k: string]: unknown };
  try {
    const text = typeof raw === 'string' ? raw : Buffer.from(raw as ArrayBuffer).toString('utf-8');
    msg = JSON.parse(text);
  } catch {
    safeClose(ws);
    throw new Error('pair-remote-client: opened frame not valid JSON');
  }

  if (msg.type === 'error') {
    const errStr = typeof msg.error === 'string' ? msg.error : 'relay_error';
    safeClose(ws);
    throw new Error(`pair-remote-client: session/open failed: ${errStr}`);
  }

  if (msg.type !== 'opened') {
    safeClose(ws);
    throw new Error(`pair-remote-client: unexpected response type '${String(msg.type)}'`);
  }

  const token = typeof msg.token === 'string' ? msg.token : '';
  const expiresAt = typeof msg.expires_at === 'string' ? msg.expires_at : '';
  if (!token || !expiresAt) {
    safeClose(ws);
    throw new Error('pair-remote-client: opened frame missing token or expires_at');
  }

  const url = buildUserUrl(relayBase, token, keypair.pkB64);

  return {
    url,
    pin,
    token,
    expiresAt,
    keypair,
    mode,
    _ws: ws,
  };
}

// ---------------------------------------------------------------------------
// Await + decrypt + ack
// ---------------------------------------------------------------------------

export async function awaitPhraseUpload(
  session: RemotePairSession,
  opts: AwaitPhraseUploadOptions,
): Promise<RelayCompletionResult> {
  const validate = opts.phraseValidator ?? defaultBip39CountValidator;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_AWAIT_TIMEOUT_MS;
  const ws = session._ws;

  let raw: Buffer | ArrayBuffer | string;
  try {
    raw = await waitNextMessage(ws, timeoutMs);
  } catch (err) {
    safeClose(ws);
    throw err;
  }

  let msg: { type?: string; [k: string]: unknown };
  try {
    const text = typeof raw === 'string' ? raw : Buffer.from(raw as ArrayBuffer).toString('utf-8');
    msg = JSON.parse(text);
  } catch {
    safeSend(ws, { type: 'nack', error: 'bad_json' });
    safeClose(ws);
    throw new Error('pair-remote-client: forward frame not valid JSON');
  }

  if (msg.type !== 'forward') {
    safeSend(ws, { type: 'nack', error: 'expected_forward' });
    safeClose(ws);
    throw new Error(`pair-remote-client: unexpected frame '${String(msg.type)}'`);
  }

  const clientPubkey = typeof msg.client_pubkey === 'string' ? msg.client_pubkey : '';
  const nonce = typeof msg.nonce === 'string' ? msg.nonce : '';
  const ciphertext = typeof msg.ciphertext === 'string' ? msg.ciphertext : '';
  if (!clientPubkey || !nonce || !ciphertext) {
    safeSend(ws, { type: 'nack', error: 'bad_forward_body' });
    safeClose(ws);
    throw new Error('pair-remote-client: forward frame missing required fields');
  }

  let plaintext: Buffer;
  try {
    plaintext = decryptPairingPayload({
      skGatewayB64: session.keypair.skB64,
      pkDeviceB64: clientPubkey,
      sid: session.token,
      nonceB64: nonce,
      ciphertextB64: ciphertext,
    });
  } catch (err) {
    safeSend(ws, { type: 'nack', error: 'decrypt_failed' });
    safeClose(ws);
    throw err instanceof Error ? err : new Error(String(err));
  }

  let mnemonic: string;
  try {
    mnemonic = plaintext
      .toString('utf-8')
      .normalize('NFKC')
      .toLowerCase()
      .trim()
      .split(/\s+/)
      .join(' ');
  } catch (err) {
    safeSend(ws, { type: 'nack', error: 'bad_utf8' });
    safeClose(ws);
    throw err instanceof Error ? err : new Error(String(err));
  } finally {
    plaintext.fill(0);
  }

  if (!validate(mnemonic)) {
    safeSend(ws, { type: 'nack', error: 'invalid_mnemonic' });
    safeClose(ws);
    throw new Error('pair-remote-client: phrase failed BIP-39 validation');
  }

  let result: RelayCompletionResult;
  try {
    result = await opts.completePairing({ mnemonic, session });
  } catch (err) {
    safeSend(ws, { type: 'nack', error: 'completion_failed' });
    safeClose(ws);
    throw err instanceof Error ? err : new Error(String(err));
  } finally {
    mnemonic = '';
  }

  if (result.state !== 'active') {
    safeSend(ws, { type: 'nack', error: result.error ?? 'completion_failed' });
    safeClose(ws);
    return result;
  }

  safeSend(ws, { type: 'ack' });
  safeClose(ws);
  return result;
}

export async function pairViaRelay(opts: {
  completePairing: RelayCompletePairingHandler;
  relayBaseUrl?: string;
  pin?: string;
  mode?: PairRelayMode;
  phraseValidator?: PhraseValidator;
  timeoutMs?: number;
}): Promise<RelayCompletionResult> {
  const session = await openRemotePairSession({
    relayBaseUrl: opts.relayBaseUrl,
    pin: opts.pin,
    mode: opts.mode,
  });
  return awaitPhraseUpload(session, {
    completePairing: opts.completePairing,
    phraseValidator: opts.phraseValidator,
    timeoutMs: opts.timeoutMs,
  });
}

// ---------------------------------------------------------------------------
// WS helpers
// ---------------------------------------------------------------------------

function waitOpen(ws: WebSocket, timeoutMs: number): Promise<void> {
  return new Promise((resolve, reject) => {
    if (ws.readyState === WebSocket.OPEN) {
      resolve();
      return;
    }
    const onOpen = (): void => {
      cleanup();
      resolve();
    };
    const onError = (err: Error): void => {
      cleanup();
      reject(err instanceof Error ? err : new Error(String(err)));
    };
    const onClose = (code: number): void => {
      cleanup();
      reject(new Error(`pair-remote-client: ws closed before open (${code})`));
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error('pair-remote-client: ws open timeout'));
    }, timeoutMs);
    const cleanup = (): void => {
      clearTimeout(timer);
      ws.off('open', onOpen);
      ws.off('error', onError);
      ws.off('close', onClose);
    };
    ws.on('open', onOpen);
    ws.on('error', onError);
    ws.on('close', onClose);
  });
}

function waitNextMessage(
  ws: WebSocket,
  timeoutMs: number,
): Promise<Buffer | ArrayBuffer | string> {
  return new Promise((resolve, reject) => {
    const onMessage = (data: Buffer | ArrayBuffer | string): void => {
      cleanup();
      resolve(data);
    };
    const onError = (err: Error): void => {
      cleanup();
      reject(err instanceof Error ? err : new Error(String(err)));
    };
    const onClose = (code: number): void => {
      cleanup();
      reject(new Error(`pair-remote-client: ws closed before message (${code})`));
    };
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error('pair-remote-client: ws message timeout'));
    }, timeoutMs);
    const cleanup = (): void => {
      clearTimeout(timer);
      ws.off('message', onMessage);
      ws.off('error', onError);
      ws.off('close', onClose);
    };
    ws.on('message', onMessage);
    ws.on('error', onError);
    ws.on('close', onClose);
  });
}

function safeSend(ws: WebSocket, msg: unknown): void {
  try {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  } catch {
    /* swallow */
  }
}

function safeClose(ws: WebSocket): void {
  try {
    if (
      ws.readyState === WebSocket.OPEN ||
      ws.readyState === WebSocket.CONNECTING
    ) {
      ws.close();
    }
  } catch {
    /* swallow */
  }
}
