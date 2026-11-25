/**
 * PRD-04 DEP-4 — MCP adapter for the core PII / credential filter.
 *
 * Covers `src/pii.ts` against the live `@totalreclaw/core` binding, the
 * self-hosted `handleRemember` call site (block-and-tell, confirmation
 * token, batch, log redaction) and the trajectory poller's allow-only gate.
 * The managed-mode handler in `src/index.ts` uses the same `screenExplicit`.
 *
 * Sensitive values are public test vectors (PSP test card) or fake-shaped.
 *
 * Run with: npx jest tests/pii.test.ts --no-cache
 */

export {};

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

// eslint-disable-next-line @typescript-eslint/no-require-imports
const pii = require('../src/pii') as typeof import('../src/pii');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { handleRemember } = require('../src/tools/remember') as typeof import('../src/tools/remember');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { startTrajectoryPoller } = require('../src/trajectory-poller') as typeof import('../src/trajectory-poller');

const CARD = '4111 1111 1111 1111';
const CARD_TEXT = `My card number is ${CARD}`;
const CLEAN_TEXT = 'My doctor is Dr. Ana Sousa';

const mockRecall = jest.fn();
const mockRemember = jest.fn();
const mockForget = jest.fn();
const client = { recall: mockRecall, remember: mockRemember, forget: mockForget, isReady: () => true };
const ctx = { client } as never;

function parse(res: { content: Array<{ text: string }> }): Record<string, unknown> {
  return JSON.parse(res.content[0].text) as Record<string, unknown>;
}

let errorSpy: jest.SpyInstance;
let nowSpy: jest.SpyInstance | null = null;

beforeEach(() => {
  jest.clearAllMocks();
  pii._setPiiFilterForTests(null);
  mockRecall.mockResolvedValue([]);
  mockRemember.mockResolvedValue('new-fact-id');
  mockForget.mockResolvedValue(undefined);
  errorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  errorSpy.mockRestore();
  nowSpy?.mockRestore();
  nowSpy = null;
});

function loggedText(): string {
  return errorSpy.mock.calls.map((c) => c.join(' ')).join('\n');
}

function advanceClock(ms: number): void {
  const base = Date.now();
  nowSpy?.mockRestore();
  nowSpy = jest.spyOn(Date, 'now').mockReturnValue(base + ms);
}

describe('pii.ts decisions from the live core binding', () => {
  test('allow', () => {
    expect(pii.piiDecision(CLEAN_TEXT)).toEqual({ decision: 'allow', category: undefined, message: undefined });
  });

  test('block carries category and the core message', () => {
    const d = pii.piiDecision(CARD_TEXT);
    expect(d.decision).toBe('block');
    expect(d.category).toBe('payment_card');
    expect(d.message).toMatch(/^Not saved: /);
  });

  test('fails closed to flag/filter_error when the binding throws', () => {
    pii._setPiiFilterForTests(() => {
      throw new Error('boom');
    });
    expect(pii.piiDecision(CLEAN_TEXT)).toEqual({ decision: 'flag', category: 'filter_error' });
    expect(pii.filterForAutoExtraction([{ text: CLEAN_TEXT }]).kept).toEqual([]);
  });

  test.each([
    [true, true],
    ['true', true],
    [' TRUE ', true],
    [false, false],
    ['false', false],
    [1, false],
    ['yes', false],
    [undefined, false],
  ])('parseAllowSensitive(%p) === %p', (value, expected) => {
    expect(pii.parseAllowSensitive(value)).toBe(expected);
  });

  test('block notice keys and order match Hermes', () => {
    const d = pii.piiDecision(CARD_TEXT);
    expect(Object.keys(pii.sensitiveBlockNotice(d))).toEqual(['stored', 'blocked', 'category', 'message', 'override']);
    const withToken = pii.sensitiveBlockNotice(d, 'abc');
    expect(Object.keys(withToken)).toEqual([
      'stored', 'blocked', 'category', 'message', 'override', 'confirm_token', 'next_step',
    ]);
    expect(withToken).toMatchObject({ stored: false, blocked: 'sensitive', category: 'payment_card', override: 'allow_sensitive' });
    expect(withToken.next_step).toBe(
      'Relay message to the user and wait for their answer. Only if they confirm in a later message, ' +
        'call totalreclaw_remember again with allow_sensitive=true and this confirm_token.',
    );
  });

  test('filterForAutoExtraction keeps only allow', () => {
    const flagText = 'number 123-45-6789 on the form';
    const out = pii.filterForAutoExtraction([{ text: CLEAN_TEXT }, { text: CARD_TEXT }, { text: flagText }]);
    expect(out.kept.map((f) => f.text)).toEqual([CLEAN_TEXT]);
    expect(out.droppedCategories).toEqual(['payment_card', 'government_id']);
    expect(loggedText()).not.toContain(CARD);
  });
});

describe('ConfirmationStore', () => {
  test('rejects unknown, same-turn, mismatched, expired; accepts once', () => {
    let t = 1_000_000;
    const store = new pii.ConfirmationStore(() => t);
    expect(store.redeem(undefined, 'payment_card')).toBe('no_token');
    expect(store.redeem('nope', 'payment_card')).toBe('unknown_token');

    const token = store.issue('payment_card');
    expect(token).toMatch(/^[0-9a-f]{16}$/);
    expect(store.redeem(token, 'payment_card')).toBe('too_soon');
    t += pii.CONFIRM_TOKEN_MIN_AGE_MS;
    expect(store.redeem(token, 'api_key')).toBe('category_mismatch');
    expect(store.redeem(token, 'payment_card')).toBeNull();
    expect(store.redeem(token, 'payment_card')).toBe('unknown_token'); // single use

    const old = store.issue('payment_card');
    t += pii.CONFIRM_TOKEN_TTL_MS;
    expect(store.redeem(old, 'payment_card')).toBe('expired');
  });

  test('keeps at most CONFIRM_TOKEN_MAX_LIVE tokens', () => {
    let t = 0;
    const store = new pii.ConfirmationStore(() => t);
    const first = store.issue('x');
    for (let i = 0; i < pii.CONFIRM_TOKEN_MAX_LIVE; i++) store.issue('x');
    t += pii.CONFIRM_TOKEN_MIN_AGE_MS;
    expect(store.redeem(first, 'x')).toBe('unknown_token');
  });
});

describe('ConfirmationStore value binding (#718)', () => {
  const CARD_B = 'my other card is 5500 0000 0000 0004';

  test('same value accepted (whitespace/case-insensitive); different value rejected; reuse rejected', () => {
    let t = 1_000_000;
    const store = new pii.ConfirmationStore(() => t);
    const token = store.issue('payment_card', CARD_TEXT);
    t += pii.CONFIRM_TOKEN_MIN_AGE_MS;
    expect(store.redeem(token, 'payment_card', CARD_B)).toBe('value_mismatch');
    expect(store.redeem(token, 'payment_card', undefined)).toBe('value_mismatch');
    // A rejected attempt does not consume the token.
    expect(store.redeem(token, 'payment_card', `  ${CARD_TEXT.toUpperCase()}  `)).toBeNull();
    expect(store.redeem(token, 'payment_card', CARD_TEXT)).toBe('unknown_token');
  });

  test('expiry and minimum age are unchanged', () => {
    let t = 0;
    const store = new pii.ConfirmationStore(() => t);
    const token = store.issue('payment_card', CARD_TEXT);
    expect(store.redeem(token, 'payment_card', CARD_TEXT)).toBe('too_soon');
    t += pii.CONFIRM_TOKEN_TTL_MS;
    expect(store.redeem(token, 'payment_card', CARD_TEXT)).toBe('expired');
  });

  test('store never holds the plaintext value', () => {
    const store = new pii.ConfirmationStore(() => 0);
    store.issue('payment_card', CARD_TEXT);
    expect(JSON.stringify([...((store as any).tokens as Map<string, unknown>).values()])).not.toContain('4111');
  });

  test('screenExplicit: a retry with a different card gets a fresh block notice', () => {
    let t = 1_000_000;
    const store = new pii.ConfirmationStore(() => t);
    const first = pii.screenExplicit(CARD_TEXT, false, undefined, store);
    t += pii.CONFIRM_TOKEN_MIN_AGE_MS;
    const retry = pii.screenExplicit(CARD_B, true, first.notice!.confirm_token, store);
    expect(retry.notice?.blocked).toBe('sensitive');
    expect(retry.notice?.confirm_token).not.toBe(first.notice!.confirm_token);
    expect(pii.screenExplicit(CARD_TEXT, true, first.notice!.confirm_token, store).notice).toBeNull();
  });
});

describe('handleRemember (self-hosted) block-and-tell', () => {
  test('a credential is blocked with the approved message and a token; nothing stored', async () => {
    const out = parse(await handleRemember(ctx, { fact: CARD_TEXT }));
    expect(out).toMatchObject({ stored: false, blocked: 'sensitive', category: 'payment_card', override: 'allow_sensitive' });
    expect(out.message).toBe(pii.piiDecision(CARD_TEXT).message);
    expect(typeof out.confirm_token).toBe('string');
    expect(mockRemember).not.toHaveBeenCalled();
    expect(loggedText()).not.toContain(CARD);
  });

  test('allow_sensitive without a token is ignored', async () => {
    const out = parse(await handleRemember(ctx, { fact: CARD_TEXT, allow_sensitive: true }));
    expect(out.blocked).toBe('sensitive');
    expect(mockRemember).not.toHaveBeenCalled();
  });

  test('a retry in the same turn is rejected, a later confirmed retry stores it', async () => {
    const first = parse(await handleRemember(ctx, { fact: CARD_TEXT }));
    const token = first.confirm_token as string;

    const same = parse(await handleRemember(ctx, { fact: CARD_TEXT, allow_sensitive: true, confirm_token: token }));
    expect(same.blocked).toBe('sensitive');
    expect(mockRemember).not.toHaveBeenCalled();

    // The rejected retry issued a fresh token; use the one it returned, later.
    advanceClock(pii.CONFIRM_TOKEN_MIN_AGE_MS + 1000);
    const later = parse(
      await handleRemember(ctx, { fact: CARD_TEXT, allow_sensitive: true, confirm_token: first.confirm_token }),
    );
    expect(later).toMatchObject({ success: true, action: 'created' });
    expect(mockRemember).toHaveBeenCalledTimes(1);
    expect(loggedText()).not.toContain(CARD);

    // Single use.
    const reuse = parse(
      await handleRemember(ctx, { fact: CARD_TEXT, allow_sensitive: true, confirm_token: first.confirm_token }),
    );
    expect(reuse.blocked).toBe('sensitive');
    expect(mockRemember).toHaveBeenCalledTimes(1);
  });

  test('clean text is stored normally', async () => {
    const out = parse(await handleRemember(ctx, { fact: CLEAN_TEXT }));
    expect(out).toMatchObject({ success: true, action: 'created' });
    expect(mockRemember).toHaveBeenCalledTimes(1);
  });

  test('batch: the blocked fact is reported, the rest are stored', async () => {
    const out = parse(await handleRemember(ctx, { facts: [{ text: CLEAN_TEXT }, { text: CARD_TEXT }] }));
    const results = out.results as Array<Record<string, unknown>>;
    expect(results[0]).toMatchObject({ success: true, action: 'created' });
    expect(results[1]).toMatchObject({ success: false, stored: false, blocked: 'sensitive', category: 'payment_card' });
    expect(typeof results[1].confirm_token).toBe('string');
    expect(mockRemember).toHaveBeenCalledTimes(1);
    expect(mockRemember.mock.calls[0][0]).toBe(CLEAN_TEXT);
    expect(loggedText()).not.toContain(CARD);
  });
});

describe('trajectory poller stores only allow verdicts', () => {
  test('a block-verdict fact never reaches persistFacts', async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-pii-poller-'));
    const sessions = path.join(home, '.openclaw', 'agents', 'main', 'sessions');
    fs.mkdirSync(sessions, { recursive: true });
    const lines = [JSON.stringify({ type: 'session.started' })];
    for (const [u, a] of [['hello', 'hi'], ['again', 'ok']]) {
      lines.push(JSON.stringify({ type: 'prompt.submitted', data: { prompt: u } }));
      lines.push(JSON.stringify({ type: 'model.completed', data: { assistantTexts: [a] } }));
    }
    fs.writeFileSync(path.join(sessions, 's1.trajectory.jsonl'), lines.join('\n') + '\n');

    const persisted: Array<{ text: string }> = [];
    const origHome = process.env.HOME;
    const origHomedir = os.homedir;
    process.env.HOME = home;
    (os as unknown as { homedir: () => string }).homedir = () => home;
    const quiet = { info: () => {}, warn: () => {}, error: () => {} };
    try {
      const handle = startTrajectoryPoller(
        {
          logger: quiet,
          ensureInitialized: async () => {},
          isPairingPending: () => false,
          isImportActive: () => false,
          getExtractInterval: () => 2,
          getMaxFactsPerExtraction: () => 10,
          isDedupEnabled: () => false,
          getDedupCandidates: async () => [],
          runExtraction: async () => [
            { text: CLEAN_TEXT, importance: 8 },
            { text: CARD_TEXT, importance: 9 },
          ],
          filterByImportance: (facts) => ({ kept: facts, dropped: 0 }),
          persistFacts: async (facts) => {
            persisted.push(...(facts as Array<{ text: string }>));
            return facts.length;
          },
        },
        { pollIntervalMs: 60_000, stateFile: path.join(home, '.totalreclaw', 'extract-state.json') },
      );
      try {
        await handle.pollOnce();
      } finally {
        handle.stop();
      }
    } finally {
      if (origHome === undefined) delete process.env.HOME;
      else process.env.HOME = origHome;
      (os as unknown as { homedir: () => string }).homedir = origHomedir;
    }
    expect(persisted.map((f) => f.text)).toEqual([CLEAN_TEXT]);
  });
});
