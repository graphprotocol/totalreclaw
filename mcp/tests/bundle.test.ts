/**
 * Tests for `subgraph/bundle.ts` — the MCP adapter over `@totalreclaw/core`'s
 * `derived-bundle-v1` WASM bindings (Option E Phase 2 / #581, P2-13).
 *
 * Uses the canonical parity mnemonic + fixture from
 * `tests/parity/fixtures/derived-bundle-v1.json` so this suite doubles as
 * this client's parity-fixture check (client-consistency.md Patch 3, step 7).
 *
 * **Gated on `hasBundleBindings()`.** As of this writing no PUBLISHED
 * `@totalreclaw/core` release exposes the bundle WASM bindings (see
 * `subgraph/bundle.ts`'s header comment) — every test in this file needs a
 * real bundle to parse/derive, so the whole suite is skipped (with a
 * visible reason in the describe title) when the installed core lacks
 * them, and runs fully against a core build that has them (e.g. the
 * locally-built WASM used for development — see mcp/README.md). The
 * feature-detection contract itself (the actionable error + mnemonic-mode
 * being unaffected when bindings are absent) is covered unconditionally by
 * `tests/bundle-feature-detection.test.ts`.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

import {
  parseBundleV1,
  validateBundleV1,
  deriveBundleFromMnemonic,
  bundleVaultToBuffers,
  redactedBundleSummary,
  hasBundleBindings,
} from '../src/subgraph/bundle.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250';

const FIXTURE_PATH = path.join(
  __dirname,
  '..',
  '..',
  'tests',
  'parity',
  'fixtures',
  'derived-bundle-v1.json',
);

function loadFixture(): { expected: Record<string, unknown>; example_full_bundle: Record<string, unknown> } {
  return JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf-8'));
}

// Every describe block below needs a real parse/derive, so gate the whole
// suite on one flag, computed once. `maybeDescribe` skips (with the reason
// baked into the visible title) rather than silently omitting the block.
const bindingsAvailable = hasBundleBindings();
const maybeDescribe = bindingsAvailable ? describe : describe.skip;
const skipSuffix = bindingsAvailable
  ? ''
  : ' — SKIPPED: installed @totalreclaw/core lacks bundle bindings (see subgraph/bundle.ts)';

maybeDescribe('deriveBundleFromMnemonic — parity fixture (client-consistency.md Patch 3 step 7)' + skipSuffix, () => {
  it('is byte-equal (minus provisioned_at) to tests/parity/fixtures/derived-bundle-v1.json', () => {
    const fixture = loadFixture();
    const json = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    const parsed = JSON.parse(json);

    expect(parsed.vault).toEqual(fixture.expected.vault);
    expect(parsed.signing).toEqual(fixture.expected.signing);
    expect(parsed.account).toEqual(fixture.expected.account);
    expect(parsed.provisioned_by).toEqual(fixture.expected.provisioned_by);
    // provisioned_at is wall-clock-stamped — excluded from equality per the
    // fixture's own _comment.
    expect(typeof parsed.provisioned_at).toBe('string');
  });
});

maybeDescribe('parseBundleV1 — round-trip + §4.7 validation' + skipSuffix, () => {
  it('parses the checked-in example_full_bundle fixture', () => {
    const fixture = loadFixture();
    const json = JSON.stringify(fixture.example_full_bundle);
    const bundle = parseBundleV1(json);
    expect(bundle.vault).toEqual(fixture.expected.vault);
    expect(bundle.account).toEqual(fixture.expected.account);
    expect(bundle.signing.kind).toBe('owner-eoa');
  });

  it('round-trips a freshly derived bundle', () => {
    const json = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    const bundle = parseBundleV1(json);
    expect(bundle.account.smart_account.toLowerCase()).toBe(TEST_SMART_ACCOUNT.toLowerCase());
    expect(bundle.account.chain_id).toBe(100);
  });

  it('rejects an unknown version — loud error, never a silent downgrade', () => {
    const fixture = loadFixture();
    const bad = { ...fixture.example_full_bundle, version: 3 };
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow(/version|schema/i);
  });

  it('rejects an unknown schema', () => {
    const fixture = loadFixture();
    const bad = { ...fixture.example_full_bundle, schema: 'something-else' };
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects a malformed (short) vault key', () => {
    const fixture = loadFixture();
    const bad = JSON.parse(JSON.stringify(fixture.example_full_bundle));
    bad.vault.auth_key = 'ab';
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects 0x-prefixed / uppercase key hex', () => {
    const fixture = loadFixture();
    const bad = JSON.parse(JSON.stringify(fixture.example_full_bundle));
    bad.vault.auth_key = '0x' + bad.vault.auth_key.slice(2);
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects an unknown signing.kind — never coerced to owner-eoa', () => {
    const fixture = loadFixture();
    const bad = JSON.parse(JSON.stringify(fixture.example_full_bundle));
    bad.signing.kind = 'multisig';
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects an owner-eoa bundle carrying a grant field', () => {
    const fixture = loadFixture();
    const bad = JSON.parse(JSON.stringify(fixture.example_full_bundle));
    bad.signing.grant = { foo: 'bar' };
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects a signing.address that does not match address(private_key)', () => {
    const fixture = loadFixture();
    const bad = JSON.parse(JSON.stringify(fixture.example_full_bundle));
    bad.signing.address = '0x0000000000000000000000000000000000000001';
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('rejects an injected mnemonic/seed field (deny_unknown_fields)', () => {
    const fixture = loadFixture();
    const bad: Record<string, unknown> = { ...fixture.example_full_bundle, mnemonic: TEST_MNEMONIC };
    expect(() => parseBundleV1(JSON.stringify(bad))).toThrow();
  });

  it('the serialised bundle never contains the mnemonic or seed substrings', () => {
    const json = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    expect(json).not.toContain('abandon');
    expect(json).not.toContain('about');
  });
});

maybeDescribe('validateBundleV1' + skipSuffix, () => {
  it('is a no-op on a valid bundle', () => {
    const fixture = loadFixture();
    expect(() => validateBundleV1(JSON.stringify(fixture.example_full_bundle))).not.toThrow();
  });

  it('throws on an invalid bundle', () => {
    expect(() => validateBundleV1('{}')).toThrow();
  });
});

maybeDescribe('bundleVaultToBuffers' + skipSuffix, () => {
  it('converts hex vault fields to 32-byte Buffers', () => {
    const fixture = loadFixture();
    const bundle = parseBundleV1(JSON.stringify(fixture.example_full_bundle));
    const buffers = bundleVaultToBuffers(bundle);
    expect(buffers.encryptionKey).toBeInstanceOf(Buffer);
    expect(buffers.encryptionKey.length).toBe(32);
    expect(buffers.dedupKey.length).toBe(32);
    expect(buffers.authKey.length).toBe(32);
    expect(buffers.lshSeed.length).toBe(32);
    expect(buffers.authKey.toString('hex')).toBe((fixture.expected.vault as Record<string, string>).auth_key);
  });
});

maybeDescribe('redactedBundleSummary — phrase-safety (derived-bundle-v1.md §4.6 point 5)' + skipSuffix, () => {
  it('never includes vault key material or the signing private key', () => {
    const fixture = loadFixture();
    const bundle = parseBundleV1(JSON.stringify(fixture.example_full_bundle));
    const summary = JSON.stringify(redactedBundleSummary(bundle));
    const vault = fixture.expected.vault as Record<string, string>;
    const signing = fixture.expected.signing as Record<string, string>;
    expect(summary).not.toContain(vault.encryption_key);
    expect(summary).not.toContain(vault.dedup_key);
    expect(summary).not.toContain(vault.auth_key);
    expect(summary).not.toContain(vault.lsh_seed);
    expect(summary).not.toContain(signing.private_key);
    // Non-secret fields ARE present — this is a redaction test, not a
    // "log nothing" test.
    expect(summary).toContain(bundle.account.smart_account);
  });
});
