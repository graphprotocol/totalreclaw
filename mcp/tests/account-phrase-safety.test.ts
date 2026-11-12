/**
 * @jest-environment node
 *
 * account-phrase-safety.test.ts
 *
 * Regression guard for p-diogo/totalreclaw-internal#670: the
 * `totalreclaw_account` tool used to return a `recovery_phrase_hint` field
 * holding the FIRST and LAST recovery-phrase words. The host model read the
 * tool result and printed both words in chat. Two of twelve words in an LLM
 * context shrink the brute-force space and break the phrase-safety rule
 * ("the recovery phrase MUST NEVER cross the LLM context").
 *
 * Invariants checked here, with synthetic test mnemonics only:
 *   1. No word of the mnemonic appears anywhere in the serialized account
 *      tool output (managed mnemonic mode, managed bundle mode, and the
 *      self-hosted / unconfigured response).
 *   2. The account output is byte-identical whatever the mnemonic is, so no
 *      prefix, length, checksum, hash or index can be derived from it.
 *   3. Building the account context never even READS `state.mnemonic`.
 *   4. The status tool output carries no mnemonic word either.
 *   5. A static scan of mcp/src finds no phrase-hint key and no
 *      split-then-index slicing of a phrase/mnemonic value.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import {
  handleAccount,
  accountContextFromState,
  accountUnavailableResponse,
  type AccountStateView,
} from '../src/tools/account';
import { handleStatus } from '../src/tools/status';

// Public BIP-39 test vectors (Trezor reference set).
const FIXED_MNEMONICS: string[] = [
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about',
  'legal winner thank year wave sausage worth useful legal winner thank yellow',
  'letter advice cage absurd amount doctor acoustic avoid letter advice cage above',
];
// Plus one generated per run. Many BIP-39 words are ordinary English, so a
// random word can occur in fixed output copy by chance; the word check
// subtracts a phrase-free baseline output to stay deterministic.
const TEST_MNEMONICS: string[] = [...FIXED_MNEMONICS, generateMnemonic(wordlist, 128)];

const WALLET = '0x1111111111111111111111111111111111111111';
const AUTH_KEY = new Uint8Array(32).fill(7);

const BILLING = {
  tier: 'free',
  free_writes_used: 3,
  free_writes_limit: 500,
  expires_at: null,
  features: { max_candidate_pool: 400 },
};

let originalFetch: typeof fetch;

beforeEach(() => {
  originalFetch = global.fetch;
  global.fetch = jest.fn(async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => BILLING,
    text: async () => JSON.stringify(BILLING),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
});

/** State whose `mnemonic` getter records every read. */
function makeState(
  kind: 'mnemonic' | 'bundle',
  mnemonic: string | undefined,
): { state: AccountStateView; reads: () => number } {
  let reads = 0;
  const state = {
    credentialKind: kind,
    smartAccountAddress: WALLET,
    authKey: AUTH_KEY,
  } as AccountStateView;
  Object.defineProperty(state, 'mnemonic', {
    enumerable: true,
    get() {
      reads += 1;
      return mnemonic;
    },
  });
  return { state, reads: () => reads };
}

async function accountOutput(state: AccountStateView): Promise<string> {
  const ctx = accountContextFromState(state, {
    serverUrl: 'http://relay.invalid',
    getFactCount: async () => 42,
  });
  const res = await handleAccount(ctx, {});
  return JSON.stringify(res);
}

function hasWord(text: string, w: string): boolean {
  return new RegExp(`(^|[^a-z])${w}([^a-z]|$)`).test(text.toLowerCase());
}

/**
 * Every mnemonic word that occurs as a whole word in `output` and NOT in
 * `baseline` (the same tool's output when no phrase exists at all).
 */
function leakedWords(output: string, mnemonic: string, baseline = ''): string[] {
  return [...new Set(mnemonic.split(/\s+/))].filter(
    (w) => hasWord(output, w) && !hasWord(baseline, w),
  );
}

/** Account output for a mnemonic-kind state that holds no phrase. */
async function phraseFreeBaseline(): Promise<string> {
  return accountOutput(makeState('mnemonic', undefined).state);
}

describe('#670 totalreclaw_account never exposes recovery-phrase material', () => {
  test.each(TEST_MNEMONICS.map((m, i) => [i, m] as const))(
    'managed mnemonic mode, vector %i: no phrase word in output',
    async (_i, mnemonic) => {
      const { state } = makeState('mnemonic', mnemonic);
      const out = await accountOutput(state);
      expect(leakedWords(out, mnemonic, await phraseFreeBaseline())).toEqual([]);
      expect(out).not.toMatch(/phrase_hint|mnemonic_hint/);
    },
  );

  test('managed mnemonic mode: output is identical for every mnemonic', async () => {
    const outputs = await Promise.all(
      TEST_MNEMONICS.map((m) => accountOutput(makeState('mnemonic', m).state)),
    );
    for (const o of outputs) expect(o).toBe(outputs[0]);
  });

  test('managed mnemonic mode: building the context never reads state.mnemonic', async () => {
    const { state, reads } = makeState('mnemonic', TEST_MNEMONICS[1]);
    await accountOutput(state);
    expect(reads()).toBe(0);
  });

  test('managed mnemonic mode: reports a non-derived credential status', async () => {
    const out = JSON.parse(
      JSON.parse(await accountOutput(makeState('mnemonic', TEST_MNEMONICS[1]).state)).content[0].text,
    );
    expect(out.credential).toBe('recovery phrase (stored locally, never shown)');
    expect(out).not.toHaveProperty('recovery_phrase_hint');
    expect(out.wallet_address).toBe(WALLET);
    expect(out.total_facts_stored).toBe(42);
  });

  test('managed bundle mode: no phrase word and bundle credential wording', async () => {
    // A bundle state has no mnemonic; plant one anyway to prove it is ignored.
    const { state, reads } = makeState('bundle', TEST_MNEMONICS[2]);
    const out = await accountOutput(state);
    expect(leakedWords(out, TEST_MNEMONICS[2])).toEqual([]);
    expect(reads()).toBe(0);
    const body = JSON.parse(JSON.parse(out).content[0].text);
    expect(body.credential).toMatch(/derived-bundle-v1/);
  });

  test('self-hosted / unconfigured path: no phrase material', () => {
    const out = JSON.stringify(accountUnavailableResponse());
    for (const m of FIXED_MNEMONICS) expect(leakedWords(out, m)).toEqual([]);
    expect(out).not.toMatch(/hint/i);
  });
});

describe('#670 totalreclaw_status never exposes recovery-phrase material', () => {
  test('status output carries no phrase word', async () => {
    const res = await handleStatus(
      { serverUrl: 'http://relay.invalid', authKeyHex: Buffer.from(AUTH_KEY).toString('hex') },
      { wallet_address: WALLET },
    );
    const out = JSON.stringify(res);
    for (const m of FIXED_MNEMONICS) expect(leakedWords(out, m)).toEqual([]);
  });
});

describe('#670 static scan: no phrase hinting or slicing in mcp/src', () => {
  const SRC = path.resolve(__dirname, '..', 'src');

  function walk(dir: string): string[] {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) return walk(p);
      return e.name.endsWith('.ts') ? [p] : [];
    });
  }

  const FORBIDDEN: Array<{ name: string; re: RegExp }> = [
    // A key / identifier that names a hint of the phrase.
    { name: 'phrase-hint identifier', re: /\b(recovery_phrase|mnemonic|phrase)_hint\b|\b(mnemonic|recoveryPhrase)Hint\b/ },
    // Split a phrase and index / slice the words on the same line.
    { name: 'split-then-index of a phrase', re: /\b(mnemonic|phrase)[!?]*\.split\([^)]*\)\s*(\[|\.slice\(|\.at\()/ },
    // Splitting the in-memory managed-service phrase at all.
    { name: 'split of state.mnemonic', re: /[Ss]tate!?\.mnemonic!?\.split\(/ },
  ];

  test('no forbidden pattern outside comments', () => {
    const hits: string[] = [];
    for (const file of walk(SRC)) {
      fs.readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
        const t = line.trim();
        if (t.startsWith('//') || t.startsWith('*') || t.startsWith('/*')) return;
        for (const { name, re } of FORBIDDEN) {
          if (re.test(line)) hits.push(`${path.relative(SRC, file)}:${i + 1} [${name}] ${t}`);
        }
      });
    }
    expect(hits).toEqual([]);
  });
});
