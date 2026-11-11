/**
 * totalreclaw_pair: how long the server waits for the browser, and what the
 * tool tells the agent to say.
 *
 * The background WS-await used to stop after 60 s. The relay then reported
 * `gateway_disconnected` to a user who was still writing down their 12 words,
 * so the default generate-mode flow could not be finished. The wait now
 * follows the relay's own session expiry (10 minutes on the current relay).
 */

import { EventEmitter } from 'node:events';

import {
  pairWaitMs,
  pairInstructions,
  runPairBackgroundTask,
  RELAY_PAIR_SESSION_TTL_MS,
} from '../src/tools/pair';
import type { RemotePairSession } from '../src/pair-remote-client';

const NOW = Date.parse('2026-11-03T12:00:00.000Z');

describe('pairWaitMs', () => {
  it('waits until the relay session expires, plus a short grace', () => {
    const expiresAt = new Date(NOW + 10 * 60_000).toISOString();
    expect(pairWaitMs(expiresAt, NOW)).toBe(10 * 60_000 + 15_000);
  });

  it('is far longer than the old 60 s window for the current relay TTL', () => {
    const expiresAt = new Date(NOW + RELAY_PAIR_SESSION_TTL_MS).toISOString();
    expect(pairWaitMs(expiresAt, NOW)).toBeGreaterThanOrEqual(10 * 60_000);
  });

  it('falls back to the relay TTL when expires_at is unparsable', () => {
    expect(pairWaitMs('not-a-date', NOW)).toBe(RELAY_PAIR_SESSION_TTL_MS + 15_000);
  });

  it('falls back to the relay TTL when expires_at is already past by the local clock', () => {
    const expiresAt = new Date(NOW - 60_000).toISOString();
    expect(pairWaitMs(expiresAt, NOW)).toBe(RELAY_PAIR_SESSION_TTL_MS + 15_000);
  });

  it('is clamped to 15 minutes', () => {
    const expiresAt = new Date(NOW + 6 * 60 * 60_000).toISOString();
    expect(pairWaitMs(expiresAt, NOW)).toBe(15 * 60_000);
  });

  it('is never shorter than 1 minute', () => {
    const expiresAt = new Date(NOW + 1_000).toISOString();
    expect(pairWaitMs(expiresAt, NOW)).toBe(60_000);
  });
});

/** Minimal stand-in for a `ws` socket: emits nothing until told to. */
class FakeSocket extends EventEmitter {
  closed = false;
  sent: string[] = [];
  readyState = 1;
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
  }
  terminate(): void {
    this.close();
  }
}

function fakeSession(expiresAt: string, ws: FakeSocket): RemotePairSession {
  return {
    url: 'https://relay.example/pair/p/tok#pk=x',
    pin: '123456',
    token: 'tok-0123456789',
    expiresAt,
    keypair: { skB64: 'unused', pkB64: 'unused' },
    mode: 'generate',
    _ws: ws as unknown as RemotePairSession['_ws'],
  };
}

describe('runPairBackgroundTask keeps the relay socket open for the session lifetime', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(NOW);
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it('is still waiting after 60 s and after 9 minutes, and gives up once the session expires', async () => {
    const ws = new FakeSocket();
    const logs: string[] = [];
    const task = runPairBackgroundTask({
      session: fakeSession(new Date(NOW + 10 * 60_000).toISOString(), ws),
      relayBaseUrl: 'wss://relay.example',
      serverUrl: 'https://relay.example',
      log: (level, msg) => logs.push(`${level}:${msg}`),
    });
    let settled = false;
    void task.then(() => {
      settled = true;
    });

    await jest.advanceTimersByTimeAsync(61_000);
    expect(ws.closed).toBe(false);
    expect(settled).toBe(false);

    await jest.advanceTimersByTimeAsync(8 * 60_000);
    expect(ws.closed).toBe(false);
    expect(settled).toBe(false);

    await jest.advanceTimersByTimeAsync(2 * 60_000);
    expect(settled).toBe(true);
    expect(ws.closed).toBe(true);
  });
});

describe('pairInstructions', () => {
  it('generate: tells the user to write the phrase down, with the real link lifetime', () => {
    const text = pairInstructions('generate', 10).join('\n');
    expect(text).toMatch(/12-word/);
    expect(text).toMatch(/writes it down/);
    expect(text).toMatch(/about 10 minutes/);
    expect(text).not.toMatch(/5 minutes/);
  });

  it('matches the pairing page ("You\'re all set"), not the old "Pairing complete"', () => {
    const text = pairInstructions('generate', 10).join('\n');
    expect(text).toMatch(/You're all set/);
    expect(text).not.toMatch(/Pairing complete/);
  });

  it('says the host app must be fully restarted before the memory tools work', () => {
    const text = pairInstructions('import', 10).join('\n');
    expect(text).toMatch(/fully quit and reopen/);
    expect(text).toMatch(/not_configured/);
  });

  it('import: the phrase goes on the page, never in chat', () => {
    const text = pairInstructions('import', 10).join('\n');
    expect(text).toMatch(/EXISTING/);
    expect(text).toMatch(/never in this chat/);
  });
});
