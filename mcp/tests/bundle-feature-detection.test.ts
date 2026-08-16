/**
 * Feature-detection contract for `subgraph/bundle.ts` (Option E Phase 2 /
 * #581, P2-13 — CI fixup after PR #618 review).
 *
 * The published `@totalreclaw/core` (checked: latest dist-tag 2.6.0-rc.1)
 * does not expose `parseBundleV1` / `validateBundleV1` /
 * `deriveBundleFromMnemonic`. `bundle.ts` feature-detects these at runtime
 * (`ensureBundleBindings()`) rather than assuming the installed package's
 * `.d.ts` matches a locally-built WASM copy — the earlier revision of this
 * file typed the `require()` as `typeof import('@totalreclaw/core')`, which
 * type-checked locally (against a hand-built WASM copy with matching
 * typings) but failed `tsc` in CI against the real, published dependency.
 *
 * This suite runs UNCONDITIONALLY — its whole point is to prove the
 * feature-detection contract holds regardless of which `@totalreclaw/core`
 * is installed:
 *
 *   - When bindings are ABSENT (the CI / published-core condition today):
 *     every bundle entry point throws ONE actionable error, and mnemonic-
 *     mode code paths are completely unaffected.
 *   - When bindings are PRESENT (a locally-built WASM, or a future
 *     published release): the entry points work normally — covered by
 *     `tests/bundle.test.ts` and friends, gated on `hasBundleBindings()`.
 */

import {
  hasBundleBindings,
  parseBundleV1,
  validateBundleV1,
  deriveBundleFromMnemonic,
} from '../src/subgraph/bundle.js';
import { resolveManagedCredential } from '../src/subgraph/credentials.js';
import { deriveKeys, computeAuthKeyHash } from '../src/subgraph/crypto.js';

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

describe('hasBundleBindings — never throws, reflects reality', () => {
  it('returns a boolean and matches the installed @totalreclaw/core', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const wasm = require('@totalreclaw/core');
    const expected =
      typeof wasm.parseBundleV1 === 'function' &&
      typeof wasm.validateBundleV1 === 'function' &&
      typeof wasm.deriveBundleFromMnemonic === 'function';
    expect(hasBundleBindings()).toBe(expected);
  });
});

// The interesting branch — only meaningful when this run's installed core
// actually lacks the bindings (today: always, since none are published).
// Gated the OPPOSITE way from tests/bundle.test.ts: this describes what
// happens WITHOUT bindings, so it is a no-op (skipped, visible reason) on a
// core build that HAS them, and is exactly what runs in CI against the
// published dependency.
const noBindings = !hasBundleBindings();
const maybeDescribeNoBindings = noBindings ? describe : describe.skip;
const skipSuffix = noBindings
  ? ''
  : ' — SKIPPED: installed @totalreclaw/core HAS bundle bindings (this suite covers the absent-bindings case)';

maybeDescribeNoBindings(
  'bundle entry points without bindings — actionable error, never a crash or silent misbehaviour' + skipSuffix,
  () => {
    it('parseBundleV1 throws ONE actionable upgrade error', () => {
      expect(() => parseBundleV1('{}')).toThrow(
        /@totalreclaw\/core >= 2\.6\.0 with bundle bindings.*upgrade the @totalreclaw\/core dependency/s,
      );
    });

    it('validateBundleV1 throws the same actionable error', () => {
      expect(() => validateBundleV1('{}')).toThrow(/bundle-mode credentials require @totalreclaw\/core/);
    });

    it('deriveBundleFromMnemonic throws the same actionable error', () => {
      expect(() =>
        deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', '0x2c0CF74B2b76110708CA431796367779e3738250'),
      ).toThrow(/bundle-mode credentials require @totalreclaw\/core/);
    });

    it('the error names the installed version when determinable, and never crashes trying', () => {
      // Never throws itself, whatever it returns is embedded in the message.
      let message = '';
      try {
        parseBundleV1('{}');
      } catch (err) {
        message = err instanceof Error ? err.message : String(err);
      }
      expect(message).toMatch(/installed version .+ lacks them/);
    });

    it(
      'a credentials.json version: 2 file surfaces the SAME actionable error through ' +
        'resolveManagedCredential — not a generic parse failure',
      () => {
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-nobindings-'));
        const credentialsPath = path.join(dir, 'credentials.json');
        fs.writeFileSync(
          credentialsPath,
          JSON.stringify({
            version: 2,
            schema: 'derived-bundle-v1',
            vault: {
              encryption_key: 'a'.repeat(64),
              dedup_key: 'a'.repeat(64),
              auth_key: 'a'.repeat(64),
              lsh_seed: 'a'.repeat(64),
            },
            signing: { kind: 'owner-eoa', private_key: 'a'.repeat(64), address: '0x' + '1'.repeat(40) },
            account: { smart_account: '0x' + '1'.repeat(40), chain_id: 100 },
            provisioned_at: '2026-08-02T09:41:00Z',
            provisioned_by: 'local-migration',
          }),
          'utf-8',
        );
        expect(() => resolveManagedCredential({ credentialsPath, env: {} })).toThrow(
          /bundle-mode credentials require @totalreclaw\/core/,
        );
      },
    );
  },
);

describe('mnemonic mode is COMPLETELY unaffected by bundle-binding availability', () => {
  // Unconditional — this is the core guarantee the coordinator asked to
  // pin down, and it should hold identically whether or not bundle
  // bindings happen to be installed.
  it('deriveKeys / computeAuthKeyHash (subgraph/crypto.ts) work with no bundle.ts involvement', () => {
    const { authKey, encryptionKey, dedupKey, salt } = deriveKeys(TEST_MNEMONIC);
    expect(authKey.length).toBe(32);
    expect(encryptionKey.length).toBe(32);
    expect(dedupKey.length).toBe(32);
    expect(salt.length).toBe(32);
    expect(computeAuthKeyHash(authKey)).toHaveLength(64);
  });

  it('resolveManagedCredential resolves a legacy plaintext mnemonic credentials.json regardless of binding availability', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-mnemonic-unaffected-'));
    const credentialsPath = path.join(dir, 'credentials.json');
    fs.writeFileSync(credentialsPath, JSON.stringify({ mnemonic: TEST_MNEMONIC }), 'utf-8');
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result).toEqual({ kind: 'mnemonic', mnemonic: TEST_MNEMONIC });
  });

  it('resolveManagedCredential resolves TOTALRECLAW_RECOVERY_PHRASE regardless of binding availability', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-mnemonic-env-'));
    const credentialsPath = path.join(dir, 'credentials.json'); // does not exist
    const result = resolveManagedCredential({
      credentialsPath,
      env: { TOTALRECLAW_RECOVERY_PHRASE: TEST_MNEMONIC },
    });
    expect(result).toEqual({ kind: 'mnemonic', mnemonic: TEST_MNEMONIC });
  });
});
