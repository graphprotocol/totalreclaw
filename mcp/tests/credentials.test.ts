/**
 * Tests for `subgraph/credentials.ts` — the MCP managed-mode credential
 * precedence (Option E Phase 2 / #581, P2-13).
 *
 * Two tests below (marked inline) construct a bundle via
 * `deriveBundleFromMnemonic`, which requires `@totalreclaw/core` to expose
 * the bundle WASM bindings — gated on `hasBundleBindings()`, same pattern
 * as `tests/bundle.test.ts`. Everything else in this file (precedence,
 * unknown-version, keychain_wrapped) never touches a binding — the
 * `keychain_wrapped: true` throw and the unknown-`version` throw both fire
 * BEFORE `resolveManagedCredential` would call into `bundle.ts` at all — so
 * those run unconditionally, including against the published core with no
 * bundle bindings.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { resolveManagedCredential, isV2BundleCredentialsFile } from '../src/subgraph/credentials.js';
import { deriveBundleFromMnemonic, hasBundleBindings } from '../src/subgraph/bundle.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const TEST_SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250';

function tmpCredentialsPath(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-creds-')), 'credentials.json');
}

function writeJson(p: string, obj: unknown): void {
  fs.writeFileSync(p, JSON.stringify(obj), 'utf-8');
}

const bindingsAvailable = hasBundleBindings();
const maybeIt = bindingsAvailable ? it : it.skip;
const maybeDescribe = bindingsAvailable ? describe : describe.skip;
const skipSuffix = bindingsAvailable
  ? ''
  : ' — SKIPPED: installed @totalreclaw/core lacks bundle bindings (see subgraph/bundle.ts)';

describe('resolveManagedCredential — precedence (client-consistency.md "Credential states a client MUST read")', () => {
  it('returns undefined when nothing is configured', () => {
    const credentialsPath = tmpCredentialsPath(); // does not exist
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result).toBeUndefined();
  });

  it('1. TOTALRECLAW_RECOVERY_PHRASE env wins over everything else', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { mnemonic: 'not the env phrase '.repeat(1) + 'x'.repeat(1) });
    const result = resolveManagedCredential({
      credentialsPath,
      env: { TOTALRECLAW_RECOVERY_PHRASE: TEST_MNEMONIC },
    });
    expect(result).toEqual({ kind: 'mnemonic', mnemonic: TEST_MNEMONIC });
  });

  maybeIt('3. credentials.json version:2 resolves to a bundle' + skipSuffix, () => {
    const credentialsPath = tmpCredentialsPath();
    const bundleJson = deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT);
    fs.writeFileSync(credentialsPath, bundleJson, 'utf-8');
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result?.kind).toBe('bundle');
    if (result?.kind === 'bundle') {
      expect(result.bundle.account.smart_account.toLowerCase()).toBe(TEST_SMART_ACCOUNT.toLowerCase());
      expect(result.bundle.signing.kind).toBe('owner-eoa');
    }
  });

  it('4. legacy plaintext {"mnemonic": …} resolves to a mnemonic', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { mnemonic: TEST_MNEMONIC, userId: 'u1', salt: 'aa', serverUrl: 'https://x' });
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result).toEqual({ kind: 'mnemonic', mnemonic: TEST_MNEMONIC });
  });

  it('an unparsable credentials.json is treated as unconfigured, not an error', () => {
    const credentialsPath = tmpCredentialsPath();
    fs.writeFileSync(credentialsPath, '{not valid json', 'utf-8');
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result).toBeUndefined();
  });

  it('a legacy file with an invalid mnemonic string resolves to undefined (not a crash)', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { mnemonic: 'not twelve words' });
    const result = resolveManagedCredential({ credentialsPath, env: {} });
    expect(result).toBeUndefined();
  });
});

describe('resolveManagedCredential — unknown version is a LOUD error, never a silent downgrade', () => {
  it.each([1, 3, 99, '2', 'two'])('throws on version=%p', (badVersion) => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { version: badVersion, mnemonic: TEST_MNEMONIC });
    expect(() => resolveManagedCredential({ credentialsPath, env: {} })).toThrow(/version/i);
  });

  it('the thrown error never contains the mnemonic value from a legacy-shaped but bad-version file', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { version: 3, mnemonic: TEST_MNEMONIC });
    try {
      resolveManagedCredential({ credentialsPath, env: {} });
      fail('expected to throw');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      expect(msg).not.toContain(TEST_MNEMONIC);
    }
  });
});

describe('resolveManagedCredential — keychain_wrapped is a known MCP gap, loud error not silent skip', () => {
  it('throws an actionable error when version=2 keychain_wrapped=true', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, {
      version: 2,
      schema: 'derived-bundle-v1',
      keychain_wrapped: true,
      account: { smart_account: TEST_SMART_ACCOUNT, chain_id: 100 },
      signing: { kind: 'owner-eoa', address: '0x9858EfFD232B4033E47d90003D41EC34EcaEda94' },
      provisioned_at: '2026-08-02T09:41:00Z',
      provisioned_by: 'local-migration',
    });
    expect(() => resolveManagedCredential({ credentialsPath, env: {} })).toThrow(/keychain/i);
  });
});

maybeDescribe('resolveManagedCredential — a malformed v2 bundle rejects loudly via parseBundleV1' + skipSuffix, () => {
  it('propagates parseBundleV1 validation errors (e.g. bad hex)', () => {
    const credentialsPath = tmpCredentialsPath();
    const bundleJson = JSON.parse(
      deriveBundleFromMnemonic(TEST_MNEMONIC, 100, 'local-migration', TEST_SMART_ACCOUNT),
    );
    bundleJson.vault.auth_key = 'not-hex';
    fs.writeFileSync(credentialsPath, JSON.stringify(bundleJson), 'utf-8');
    expect(() => resolveManagedCredential({ credentialsPath, env: {} })).toThrow();
  });
});

describe('isV2BundleCredentialsFile — self-hosted diagnostic (#618 item 2)', () => {
  it('returns true for a version: 2 file', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { version: 2, schema: 'derived-bundle-v1' });
    expect(isV2BundleCredentialsFile(credentialsPath)).toBe(true);
  });

  it('returns false for a legacy plaintext mnemonic file', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { mnemonic: TEST_MNEMONIC });
    expect(isV2BundleCredentialsFile(credentialsPath)).toBe(false);
  });

  it('returns false for a missing file', () => {
    const credentialsPath = tmpCredentialsPath(); // never written
    expect(isV2BundleCredentialsFile(credentialsPath)).toBe(false);
  });

  it('returns false for unparsable JSON (never throws)', () => {
    const credentialsPath = tmpCredentialsPath();
    fs.writeFileSync(credentialsPath, '{not json', 'utf-8');
    expect(() => isV2BundleCredentialsFile(credentialsPath)).not.toThrow();
    expect(isV2BundleCredentialsFile(credentialsPath)).toBe(false);
  });

  it('returns false for an unrelated version number', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { version: 1 });
    expect(isV2BundleCredentialsFile(credentialsPath)).toBe(false);
  });
});

describe('resolveManagedCredential — env-phrase-shadows-bundle logging (#618 item 4)', () => {
  it('logs a console.error when TOTALRECLAW_RECOVERY_PHRASE wins over a v2 bundle also present on disk', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, {
      version: 2,
      schema: 'derived-bundle-v1',
      account: { smart_account: TEST_SMART_ACCOUNT, chain_id: 100 },
    });
    const errSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    try {
      const result = resolveManagedCredential({
        credentialsPath,
        env: { TOTALRECLAW_RECOVERY_PHRASE: TEST_MNEMONIC },
      });
      expect(result).toEqual({ kind: 'mnemonic', mnemonic: TEST_MNEMONIC });
      expect(errSpy).toHaveBeenCalledTimes(1);
      const [msg] = errSpy.mock.calls[0];
      expect(String(msg)).toContain('shadow');
      expect(String(msg)).toContain(credentialsPath);
    } finally {
      errSpy.mockRestore();
    }
  });

  it('does NOT log when TOTALRECLAW_RECOVERY_PHRASE is used and no bundle file exists', () => {
    const credentialsPath = tmpCredentialsPath(); // never written
    const errSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    try {
      resolveManagedCredential({
        credentialsPath,
        env: { TOTALRECLAW_RECOVERY_PHRASE: TEST_MNEMONIC },
      });
      expect(errSpy).not.toHaveBeenCalled();
    } finally {
      errSpy.mockRestore();
    }
  });

  it('does NOT log when the on-disk file is legacy (not a v2 bundle)', () => {
    const credentialsPath = tmpCredentialsPath();
    writeJson(credentialsPath, { mnemonic: 'some other stored phrase' });
    const errSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
    try {
      resolveManagedCredential({
        credentialsPath,
        env: { TOTALRECLAW_RECOVERY_PHRASE: TEST_MNEMONIC },
      });
      expect(errSpy).not.toHaveBeenCalled();
    } finally {
      errSpy.mockRestore();
    }
  });
});
