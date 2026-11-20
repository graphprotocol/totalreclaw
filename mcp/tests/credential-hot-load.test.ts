/**
 * @jest-environment node
 *
 * 3.6.1: credentials load after `totalreclaw_pair` finishes, with no host
 * restart (`src/credential-activation.ts`, wired in `index.ts`).
 *
 * Trigger A: the pair task wrote credentials.json in this process.
 * Trigger B: another process wrote it while this server was unconfigured.
 *
 * `index.ts` boots a stdio server on import, so the activator is exercised
 * here with injected fakes for the heavy state init, the REAL
 * `resolveManagedCredential` reading a temp file, the REAL dispatcher, and the
 * REAL pair background task over a fake WebSocket. Fixtures: the public BIP-39
 * test vectors already used by the other pair tests, in temp dirs. No test
 * prints a phrase, and the fake state never carries one.
 */

import { EventEmitter } from 'node:events';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';

import { createCredentialActivator, ACTIVATION_RETRY_COOLDOWN_MS } from '../src/credential-activation';
import { resolveManagedCredential, UnsupportedCredentialsError } from '../src/subgraph/credentials';
import {
  createCallToolHandler,
  type DispatchDeps,
  type ServerMode,
  type UnusableCredentials,
} from '../src/dispatch';
import { handlePair, runPairBackgroundTask } from '../src/tools/pair';
import type { RemotePairSession } from '../src/pair-remote-client';
import { generateGatewayKeypair, encryptPairingPayload, type GatewayKeypair } from '../src/pair-crypto';
import { getLastBillingResponse, resetLastBillingResponse } from '../src/tools/status';
import { getRecallTopK, setServerRecallTopK, resetServerRecallTopK, DEFAULT_RECALL_TOP_K } from '../src/recall-top-k';
import type { ToolResponse } from '../src/tools/types';

const MNEMONIC_A =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const MNEMONIC_B =
  'legal winner thank year wave sausage worth useful legal winner thank yellow';
const TOKEN = 'test-token-hotload-0001';

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

interface FakeState {
  which: 'A' | 'B' | 'bundle';
  id: number;
}

let tmpDir: string;
let credsPath: string;

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-hotload-'));
  credsPath = path.join(tmpDir, 'credentials.json');
});
afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

function writeLegacyCreds(mnemonic: string): void {
  // Same shape `writePairedCredentials` writes; atomic like the pair flow.
  const tmp = `${credsPath}.tmp-${Math.random().toString(36).slice(2)}`;
  fs.writeFileSync(tmp, JSON.stringify({ userId: 'u', salt: 'aa', serverUrl: 'https://relay.test', mnemonic }), { mode: 0o600 });
  fs.renameSync(tmp, credsPath);
}

interface HarnessOptions {
  selfHosted?: boolean;
  resolve?: () => ReturnType<typeof resolveManagedCredential>;
  initDelay?: Promise<void>;
  initFailsOnce?: boolean;
  /** Init fails every time while true. */
  initFailsWhile?: () => boolean;
  clock?: { t: number };
}

function makeHarness(opts: HarnessOptions = {}) {
  const h = {
    state: null as FakeState | null,
    unusable: undefined as UnusableCredentials | undefined,
    initCalls: 0,
    resolveCalls: 0,
    resets: 0,
    commits: [] as Array<{ phase: string; hadState: boolean; next: FakeState | null }>,
    logs: [] as string[],
  };
  let failOnce = opts.initFailsOnce === true;
  const activator = createCredentialActivator<FakeState>({
    credentialsPath: credsPath,
    isSelfHosted: () => opts.selfHosted === true,
    resolveCredential: () => {
      h.resolveCalls++;
      return opts.resolve ? opts.resolve() : resolveManagedCredential({ credentialsPath: credsPath, env: {} });
    },
    initFromBundle: async () => {
      h.initCalls++;
      await opts.initDelay;
      return { which: 'bundle', id: h.initCalls };
    },
    initFromMnemonic: async (m) => {
      h.initCalls++;
      await opts.initDelay;
      if (opts.initFailsWhile?.()) throw new Error('billing unreachable');
      if (failOnce) {
        failOnce = false;
        throw new Error('billing unreachable');
      }
      return { which: m === MNEMONIC_A ? 'A' : 'B', id: h.initCalls };
    },
    isUsableMnemonic: (m) => validateMnemonic(m, wordlist),
    ownerAddress: (s) => `0xSA${s.id}`,
    getState: () => h.state,
    commit: (next, info) => {
      if (info.hadState) h.resets++; // index.ts: resetPerAccountState()
      h.commits.push({ phase: info.phase, hadState: info.hadState, next });
      h.state = next;
    },
    getUnusable: () => h.unusable,
    setUnusable: (u) => {
      h.unusable = u;
    },
    log: (m) => h.logs.push(m),
    ...(opts.clock ? { now: () => opts.clock!.t } : {}),
  });
  const mode = (): ServerMode => (h.state ? 'subgraph' : 'unconfigured');
  return { h, activator, mode };
}

function ok(payload: Record<string, unknown>): ToolResponse {
  return { content: [{ type: 'text', text: JSON.stringify(payload) }] };
}
function parse(res: ToolResponse): Record<string, unknown> {
  return JSON.parse(res.content[0].text) as Record<string, unknown>;
}

/** The real dispatcher over the harness, with a stub status tool. */
function makeDispatcher(
  hx: ReturnType<typeof makeHarness>,
  extra: Partial<DispatchDeps> = {},
) {
  const deps: DispatchDeps = {
    getMode: hx.mode,
    handleSupport: async () => ok({ support: true }),
    handlePair: async () => ok({ pair: true }),
    common: { totalreclaw_status: async () => ok({ onboarded: true, which: hx.h.state?.which }) },
    resolveBundle: async () => ({}),
    isQuotaExceededError: () => false,
    quotaExceededResponse: () => ok({ error: 'quota' }),
    isAuthError: () => false,
    authHintResponse: () => ok({ error: 'auth' }),
    onMutate: () => undefined,
    getUnusableCredentials: () => hx.h.unusable,
    tryReload: () => hx.activator.tryReload(),
    ...extra,
  };
  return createCallToolHandler(deps);
}

// ---------------------------------------------------------------------------
// Fake WebSocket (same shape as pair-replace-guard.test.ts)
// ---------------------------------------------------------------------------

class FakeWebSocket extends EventEmitter {
  readyState = 1;
  sent: Array<Record<string, unknown>> = [];
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(): void {
    this.readyState = 3;
  }
  pushMessage(obj: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(obj), 'utf-8'));
  }
}

function makeSession(ws: FakeWebSocket, keypair: GatewayKeypair, replace: boolean): RemotePairSession {
  return {
    url: `https://api-staging.totalreclaw.xyz/pair/p/${TOKEN}#pk=xyz`,
    pin: '123456',
    token: TOKEN,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    keypair,
    mode: 'import',
    _ws: ws as unknown as RemotePairSession['_ws'],
    ...(replace ? { replace: true } : {}),
  };
}

function forwardFrame(gatewayKeypair: GatewayKeypair, mnemonic: string, extra: Record<string, unknown> = {}) {
  const device = generateGatewayKeypair();
  const { nonceB64, ciphertextB64 } = encryptPairingPayload({
    skLocalB64: device.skB64,
    pkRemoteB64: gatewayKeypair.pkB64,
    sid: TOKEN,
    plaintext: Buffer.from(mnemonic, 'utf-8'),
  });
  return { type: 'forward', client_pubkey: device.pkB64, nonce: nonceB64, ciphertext: ciphertextB64, ...extra };
}

const silentLog = () => undefined;
const realFetch = global.fetch;
beforeEach(() => {
  // registerUser is best-effort; keep it off the network.
  global.fetch = jest.fn(async () => {
    throw new Error('offline in tests');
  }) as unknown as typeof fetch;
});
afterEach(() => {
  global.fetch = realFetch;
});

async function waitFor(cond: () => boolean, ms = 3000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) throw new Error('waitFor timed out');
    await new Promise((r) => setTimeout(r, 5));
  }
}

// ---------------------------------------------------------------------------
// 1. Trigger A: pair completes in this process
// ---------------------------------------------------------------------------

describe('Trigger A: pairing finishes in this process', () => {
  it('leaves unconfigured and the next memory tool call works, with no restart', async () => {
    const hx = makeHarness();
    const dispatch = makeDispatcher(hx);

    const before = await dispatch('totalreclaw_status', {});
    expect(before.isError).toBe(true);
    expect(parse(before).error).toBe('not_configured');

    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const ackedWhenCallbackRan: boolean[] = [];
    const task = runPairBackgroundTask({
      session: makeSession(ws, kp, false),
      relayBaseUrl: 'wss://relay.test',
      serverUrl: 'https://relay.test',
      hardTimeoutMs: 10_000,
      log: silentLog,
      credentialsPath: credsPath,
      onCredentialsWritten: () => {
        ackedWhenCallbackRan.push(ws.sent.some((f) => f.type === 'ack'));
        return hx.activator.reloadAfterPair();
      },
    });
    ws.pushMessage(forwardFrame(kp, MNEMONIC_A));
    await task;

    // The relay was acked first, then the callback ran.
    expect(ackedWhenCallbackRan).toEqual([true]);
    expect(hx.mode()).toBe('subgraph');
    expect(hx.h.state?.which).toBe('A');
    expect(hx.h.initCalls).toBe(1);

    const after = await dispatch('totalreclaw_status', {});
    expect(after.isError).toBeUndefined();
    expect(parse(after)).toEqual({ onboarded: true, which: 'A' });
  });

  it('is wired through handlePair (option plumbing)', async () => {
    const hx = makeHarness();
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const callback = jest.fn(() => hx.activator.reloadAfterPair());
    await handlePair({}, { mode: 'import' }, {
      credentialsPath: credsPath,
      openSession: (async () => makeSession(ws, kp, false)) as never,
      log: silentLog,
      onCredentialsWritten: callback,
    });
    ws.pushMessage(forwardFrame(kp, MNEMONIC_A));
    await waitFor(() => hx.h.state !== null);
    expect(callback).toHaveBeenCalledTimes(1);
    expect(callback.mock.calls[0]).toHaveLength(0); // no arguments: nothing secret passed
  });

  it('a throwing callback does not change the ack the relay got', async () => {
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const logs: string[] = [];
    const task = runPairBackgroundTask({
      session: makeSession(ws, kp, false),
      relayBaseUrl: 'wss://relay.test',
      serverUrl: 'https://relay.test',
      hardTimeoutMs: 10_000,
      log: (_l, m) => logs.push(m),
      credentialsPath: credsPath,
      onCredentialsWritten: () => {
        throw new Error('boom');
      },
    });
    ws.pushMessage(forwardFrame(kp, MNEMONIC_A));
    await expect(task).resolves.toBeUndefined();
    expect(ws.sent).toContainEqual({ type: 'ack' });
    expect(ws.sent.filter((f) => f.type === 'nack')).toHaveLength(0);
    expect(fs.existsSync(credsPath)).toBe(true);
    expect(logs.join('\n')).toMatch(/without a restart failed: boom/);
  });

  it('is not called when nothing was written (existing file appeared mid-pairing, nack)', async () => {
    fs.writeFileSync(credsPath, '{"userId":"x"}\n', { mode: 0o600 });
    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const callback = jest.fn();
    const task = runPairBackgroundTask({
      session: makeSession(ws, kp, false),
      relayBaseUrl: 'wss://relay.test',
      serverUrl: 'https://relay.test',
      hardTimeoutMs: 10_000,
      log: silentLog,
      credentialsPath: credsPath,
      onCredentialsWritten: callback,
    });
    ws.pushMessage(forwardFrame(kp, MNEMONIC_A));
    await task;
    expect(ws.sent).toContainEqual({ type: 'nack', error: 'already_configured' });
    expect(callback).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 2. Trigger B: file written by another process
// ---------------------------------------------------------------------------

describe('Trigger B: credentials.json written by another process', () => {
  it('the next tool call activates it', async () => {
    const hx = makeHarness();
    await hx.activator.activate('startup'); // no file: unconfigured
    expect(hx.mode()).toBe('unconfigured');
    const dispatch = makeDispatcher(hx);

    const first = await dispatch('totalreclaw_status', {});
    expect(parse(first).error).toBe('not_configured');
    expect(hx.h.initCalls).toBe(0); // no file, nothing to activate

    writeLegacyCreds(MNEMONIC_A); // e.g. Claude Code paired
    const second = await dispatch('totalreclaw_status', {});
    expect(second.isError).toBeUndefined();
    expect(parse(second)).toEqual({ onboarded: true, which: 'A' });
    expect(hx.h.initCalls).toBe(1);
    expect(hx.h.commits.at(-1)).toMatchObject({ phase: 'reload', hadState: false });
  });

  it('does not re-read an unchanged unusable file on every call; retries when it changes', async () => {
    fs.writeFileSync(credsPath, JSON.stringify({ version: 2, schema: 'derived-bundle-v1', keychain_wrapped: true }));
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.h.unusable?.reason).toBe('keychain_wrapped');
    const dispatch = makeDispatcher(hx);

    const resolvesAfterStartup = hx.h.resolveCalls;
    const res = await dispatch('totalreclaw_status', {});
    expect(parse(res).reason).toBe('keychain_wrapped');
    await dispatch('totalreclaw_status', {});
    expect(hx.h.resolveCalls).toBe(resolvesAfterStartup); // one stat each, no re-read

    writeLegacyCreds(MNEMONIC_A);
    const after = await dispatch('totalreclaw_status', {});
    expect(after.isError).toBeUndefined();
    expect(hx.h.unusable).toBeUndefined();
    expect(hx.h.state?.which).toBe('A');
  });

  it('never swaps a configured server (restart still needed for that)', async () => {
    writeLegacyCreds(MNEMONIC_A);
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.h.state?.which).toBe('A');
    const dispatch = makeDispatcher(hx);

    writeLegacyCreds(MNEMONIC_B); // another client replaced the account
    await new Promise((r) => setTimeout(r, 15));
    await dispatch('totalreclaw_status', {});
    await hx.activator.tryReload();
    expect(hx.h.state?.which).toBe('A');
    expect(hx.h.initCalls).toBe(1);
    expect(hx.h.resets).toBe(0);
  });

  it('dispatcher only tries a reload while unconfigured, and swallows a failing reload', async () => {
    const tryReload = jest.fn(async () => {
      throw new Error('x');
    });
    const unconfigured = makeDispatcher(makeHarness(), { tryReload });
    expect(parse(await unconfigured('totalreclaw_status', {})).error).toBe('not_configured');
    expect(tryReload).toHaveBeenCalledTimes(1);

    tryReload.mockClear();
    const hx = makeHarness();
    writeLegacyCreds(MNEMONIC_A);
    await hx.activator.activate('startup');
    const configured = makeDispatcher(hx, { tryReload });
    await configured('totalreclaw_status', {});
    expect(tryReload).not.toHaveBeenCalled();
  });

  it('pair and support never trigger a reload (they run before the unconfigured check)', async () => {
    const tryReload = jest.fn(async () => undefined);
    const dispatch = makeDispatcher(makeHarness(), { tryReload });
    await dispatch('totalreclaw_pair', {});
    await dispatch('totalreclaw_support', {});
    expect(tryReload).not.toHaveBeenCalled();
  });

  it('self-hosted mode: reload is a no-op in both triggers', async () => {
    const hx = makeHarness({ selfHosted: true });
    writeLegacyCreds(MNEMONIC_A);
    await hx.activator.tryReload();
    await hx.activator.reloadAfterPair();
    expect(hx.h.resolveCalls).toBe(0);
    expect(hx.h.initCalls).toBe(0);
    expect(hx.mode()).toBe('unconfigured');
  });
});

// ---------------------------------------------------------------------------
// 3. Replace in this process
// ---------------------------------------------------------------------------

describe('confirmed replace in this process', () => {
  it('swaps the running server to the new account and resets the per-account state', async () => {
    writeLegacyCreds(MNEMONIC_A);
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.h.state?.which).toBe('A');
    const dispatch = makeDispatcher(hx);
    expect(parse(await dispatch('totalreclaw_status', {})).which).toBe('A');

    const kp = generateGatewayKeypair();
    const ws = new FakeWebSocket();
    const task = runPairBackgroundTask({
      session: makeSession(ws, kp, true),
      relayBaseUrl: 'wss://relay.test',
      serverUrl: 'https://relay.test',
      hardTimeoutMs: 10_000,
      log: silentLog,
      credentialsPath: credsPath,
      onCredentialsWritten: () => hx.activator.reloadAfterPair(),
    });
    ws.pushMessage(forwardFrame(kp, MNEMONIC_B, { replace_confirmed: true }));
    await task;

    expect(hx.h.state?.which).toBe('B');
    expect(hx.h.commits.at(-1)).toMatchObject({ phase: 'reload', hadState: true });
    expect(hx.h.resets).toBe(1);
    expect(parse(await dispatch('totalreclaw_status', {})).which).toBe('B');
  });

  it('a replace that cannot be activated drops to unconfigured with a reason, never keeps the old account', async () => {
    writeLegacyCreds(MNEMONIC_A);
    let broken = false;
    const hx = makeHarness({
      resolve: () => {
        if (broken) throw new Error('bundle failed validation');
        return resolveManagedCredential({ credentialsPath: credsPath, env: {} });
      },
    });
    await hx.activator.activate('startup');
    expect(hx.h.state?.which).toBe('A');

    broken = true;
    await hx.activator.reloadAfterPair();
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.resets).toBe(1);
    expect(hx.h.unusable?.reason).toBe('invalid_credentials');
  });

  it('the per-account module state it resets is really cleared', () => {
    setServerRecallTopK(42);
    expect(getRecallTopK({} as NodeJS.ProcessEnv)).toBe(42);
    resetServerRecallTopK();
    expect(getRecallTopK({} as NodeJS.ProcessEnv)).toBe(DEFAULT_RECALL_TOP_K);

    resetLastBillingResponse();
    expect(getLastBillingResponse()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. Invalid file at reload
// ---------------------------------------------------------------------------

describe('invalid credentials at reload', () => {
  it('stays unconfigured, does not throw, uses nothing from the file, and says why on every tool call', async () => {
    const secretish = 'SECRET-LOOKING-DETAIL-MUST-NOT-LEAK';
    // Start from a clean unconfigured server, then the file appears.
    const clean = makeHarness({
      resolve: () => {
        if (!fs.existsSync(credsPath)) return undefined;
        throw new Error(`bad bundle: ${secretish}`);
      },
    });
    await clean.activator.activate('startup');
    const dispatch = makeDispatcher(clean);
    fs.writeFileSync(credsPath, '{"version":2}\n', { mode: 0o600 });

    const res = await dispatch('totalreclaw_status', {});
    const again = await dispatch('totalreclaw_recall', { query: 'x' });
    for (const r of [res, again]) {
      expect(r.isError).toBe(true);
      const body = parse(r);
      expect(body.error).toBe('not_configured');
      expect(body.reason).toBe('invalid_credentials');
      expect(r.content[0].text).not.toContain(secretish);
    }
    expect(clean.h.initCalls).toBe(0);
    expect(clean.mode()).toBe('unconfigured');
    // The cause goes to the log (as at startup), not to the tool response.
    expect(clean.h.logs.join('\n')).toContain(secretish);
    // One read for the changed file, not one per call.
    expect(clean.h.resolveCalls).toBe(2); // startup + one reload
  });

  it('a real keychain-wrapped file on reload gives the existing unsupported reason', async () => {
    const hx = makeHarness();
    await hx.activator.activate('startup');
    fs.writeFileSync(credsPath, JSON.stringify({ version: 2, schema: 'derived-bundle-v1', keychain_wrapped: true }));
    await hx.activator.tryReload();
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.unusable?.reason).toBe('keychain_wrapped');
    expect(hx.h.initCalls).toBe(0);
  });

  it('a transient init failure stays unconfigured with a reason; Trigger B retries after the cooldown', async () => {
    const clock = { t: 1_000_000 };
    const hx = makeHarness({ initFailsOnce: true, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);

    const first = await dispatch('totalreclaw_status', {});
    expect(parse(first).reason).toBe('activation_failed');
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.initCalls).toBe(1);

    clock.t += ACTIVATION_RETRY_COOLDOWN_MS;
    const second = await dispatch('totalreclaw_status', {});
    expect(second.isError).toBeUndefined();
    expect(hx.h.unusable).toBeUndefined();
    expect(hx.h.state?.which).toBe('A');
  });
});

describe('activation_failed cooldown', () => {
  const COOLDOWN = ACTIVATION_RETRY_COOLDOWN_MS;

  it('two calls 1 s apart run activation once, with the stored reason in between', async () => {
    const clock = { t: 5_000_000 };
    const hx = makeHarness({ initFailsWhile: () => true, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);

    const first = await dispatch('totalreclaw_status', {});
    expect(parse(first).reason).toBe('activation_failed');
    clock.t += 1_000;
    const second = await dispatch('totalreclaw_status', {});
    expect(parse(second).reason).toBe('activation_failed');
    expect(hx.h.initCalls).toBe(1);
  });

  it('a call after the cooldown runs activation again', async () => {
    const clock = { t: 5_000_000 };
    const hx = makeHarness({ initFailsWhile: () => true, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);

    await dispatch('totalreclaw_status', {});
    clock.t += COOLDOWN - 1;
    await dispatch('totalreclaw_status', {});
    expect(hx.h.initCalls).toBe(1);
    clock.t += 1;
    await dispatch('totalreclaw_status', {});
    expect(hx.h.initCalls).toBe(2);
  });

  it('a changed file runs at once, inside the cooldown', async () => {
    const clock = { t: 5_000_000 };
    let failing = true;
    const hx = makeHarness({ initFailsWhile: () => failing, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);
    await dispatch('totalreclaw_status', {});
    expect(hx.h.initCalls).toBe(1);

    failing = false;
    writeLegacyCreds(MNEMONIC_B);
    fs.utimesSync(credsPath, new Date(Date.now() + 5_000), new Date(Date.now() + 5_000)); // distinct mtime
    clock.t += 1_000;
    const res = await dispatch('totalreclaw_status', {});
    expect(res.isError).toBeUndefined();
    expect(hx.h.state?.which).toBe('B');
  });

  it('a pair completion inside the cooldown runs at once', async () => {
    const clock = { t: 5_000_000 };
    let failing = true;
    const hx = makeHarness({ initFailsWhile: () => failing, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);
    await dispatch('totalreclaw_status', {});
    expect(hx.h.initCalls).toBe(1);

    failing = false;
    clock.t += 1_000;
    await hx.activator.reloadAfterPair();
    expect(hx.h.initCalls).toBe(2);
    expect(hx.h.state?.which).toBe('A');
    expect(hx.h.unusable).toBeUndefined();
  });

  it('the file disappearing clears the stored reason: plain not_configured', async () => {
    const clock = { t: 5_000_000 };
    const hx = makeHarness({ initFailsWhile: () => true, clock });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);
    expect(parse(await dispatch('totalreclaw_status', {})).reason).toBe('activation_failed');

    fs.rmSync(credsPath);
    const res = await dispatch('totalreclaw_status', {});
    const body = parse(res);
    expect(res.isError).toBe(true);
    expect(body.error).toBe('not_configured');
    expect(body.reason).toBeUndefined();
    expect(hx.h.unusable).toBeUndefined();

    // And a later file starts fresh, with no cooldown carried over.
    clock.t += 1_000;
    writeLegacyCreds(MNEMONIC_A);
    expect(hx.h.initCalls).toBe(1);
    await dispatch('totalreclaw_status', {});
    expect(hx.h.initCalls).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// 5. Single flight
// ---------------------------------------------------------------------------

describe('single flight', () => {
  it('two concurrent tool calls during a reload run ONE activation', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const hx = makeHarness({ initDelay: gate });
    await hx.activator.activate('startup');
    const dispatch = makeDispatcher(hx);
    writeLegacyCreds(MNEMONIC_A);

    const p1 = dispatch('totalreclaw_status', {});
    const p2 = dispatch('totalreclaw_status', {});
    await new Promise((r) => setTimeout(r, 20));
    release();
    const [r1, r2] = await Promise.all([p1, p2]);

    expect(hx.h.initCalls).toBe(1);
    expect(r1.isError).toBeUndefined();
    expect(r2.isError).toBeUndefined();
  });

  it('a pair completing while a reload is running is not missed (one re-run)', async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const hx = makeHarness({ initDelay: gate });
    await hx.activator.activate('startup');
    writeLegacyCreds(MNEMONIC_A);
    const b = hx.activator.tryReload(); // starts a run that reads file A
    await new Promise((r) => setTimeout(r, 10));
    const a = hx.activator.reloadAfterPair(); // joins, asks for a re-run
    release();
    await Promise.all([a, b]);
    expect(hx.h.resolveCalls).toBe(3); // startup + run + re-run
    expect(hx.h.state).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 6. Startup behaviour unchanged
// ---------------------------------------------------------------------------

describe('startup activation', () => {
  it('nothing configured: unconfigured, no init, same log line as before', async () => {
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.unusable).toBeUndefined();
    expect(hx.h.logs).toEqual([
      'TotalReclaw MCP server started (unconfigured — ask the assistant to run totalreclaw_pair to set up an account)',
    ]);
  });

  it('a usable mnemonic file starts configured', async () => {
    writeLegacyCreds(MNEMONIC_A);
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.h.state?.which).toBe('A');
    expect(hx.h.logs[0]).toBe('TotalReclaw MCP server started (managed service, owner: 0xSA1)');
  });

  it('an unsupported file starts unconfigured with the reason (not fatal)', async () => {
    fs.writeFileSync(credsPath, JSON.stringify({ version: 9 }));
    const hx = makeHarness();
    await hx.activator.activate('startup');
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.unusable?.reason).toBe('unknown_version');
    expect(hx.h.logs[0]).toMatch(/^TotalReclaw MCP server started \(unconfigured\): /);
  });

  it('a credential that fails validation is still FATAL at startup', async () => {
    const hx = makeHarness({
      resolve: () => {
        throw new Error('bundle failed validation');
      },
    });
    await expect(hx.activator.activate('startup')).rejects.toThrow('bundle failed validation');
    expect(hx.h.logs[0]).toBe('TotalReclaw MCP: fatal credential error: bundle failed validation');
    expect(hx.mode()).toBe('unconfigured');
    expect(hx.h.unusable).toBeUndefined();
  });

  it('a failing init at startup propagates (as before)', async () => {
    writeLegacyCreds(MNEMONIC_A);
    const hx = makeHarness({ initFailsOnce: true });
    await expect(hx.activator.activate('startup')).rejects.toThrow('billing unreachable');
  });

  it('UnsupportedCredentialsError class is the one the activator recognises', () => {
    expect(new UnsupportedCredentialsError('unknown_version', 'm')).toBeInstanceOf(Error);
  });
});
