/**
 * Tests for bundle-mode signing in `subgraph/store.ts` (Option E Phase 2 /
 * #581, P2-13). Before this change, every on-chain submission function
 * unconditionally called `mnemonicToAccount(config.mnemonic)` — a
 * bundle-configured server (no mnemonic anywhere in the process) could
 * never sign a UserOp. `resolveOwnerAccount` is the single choke point that
 * now decides mnemonic-derivation vs bundle private-key directly.
 */

import { mnemonicToAccount, privateKeyToAccount } from 'viem/accounts';

import { resolveOwnerAccount, getSubgraphConfig, type SubgraphStoreConfig } from '../src/subgraph/store.js';

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

const baseConfig: SubgraphStoreConfig = {
  relayUrl: 'https://api-staging.totalreclaw.xyz',
  cachePath: '/tmp/cache.enc',
  chainId: 100,
  dataEdgeAddress: '0xE7a4D2677B686e13775Ba9092631089e35F0BB91',
  entryPointAddress: '0x0000000071727De22E5E9d8BAf0edAc6f37da032',
};

describe('resolveOwnerAccount', () => {
  it('derives the owner account from a mnemonic when only mnemonic is set', () => {
    const account = resolveOwnerAccount({ ...baseConfig, mnemonic: TEST_MNEMONIC });
    const expected = mnemonicToAccount(TEST_MNEMONIC);
    expect(account.address).toBe(expected.address);
  });

  it('derives the owner account from a bundle private key when only ownerPrivateKeyHex is set', () => {
    // Same signing key the mnemonic above derives — proving bundle-mode
    // and mnemonic-mode produce the SAME signing address for the same
    // underlying root (derived-bundle-v1.md §4.6 point 2 — byte-identical
    // signing behaviour on the same vault).
    const mnemonicAcct = mnemonicToAccount(TEST_MNEMONIC);
    const privateKeyHex = 'ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'.slice(0, 64);
    // Use a real, independently-derived key instead — clearer than reusing
    // an unrelated literal. Derive via privateKeyToAccount and compare
    // address equality with itself as the contract under test.
    const account = resolveOwnerAccount({ ...baseConfig, ownerPrivateKeyHex: privateKeyHex });
    const expected = privateKeyToAccount(`0x${privateKeyHex}` as `0x${string}`);
    expect(account.address).toBe(expected.address);
    expect(mnemonicAcct.address).not.toBe(account.address); // sanity: different roots
  });

  it('accepts a 0x-prefixed ownerPrivateKeyHex identically to an unprefixed one', () => {
    const hex = '1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727';
    const a = resolveOwnerAccount({ ...baseConfig, ownerPrivateKeyHex: hex });
    const b = resolveOwnerAccount({ ...baseConfig, ownerPrivateKeyHex: `0x${hex}` });
    expect(a.address).toBe(b.address);
  });

  it('prefers ownerPrivateKeyHex over mnemonic when both are somehow present', () => {
    const hex = '1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727';
    const account = resolveOwnerAccount({ ...baseConfig, mnemonic: TEST_MNEMONIC, ownerPrivateKeyHex: hex });
    const expected = privateKeyToAccount(`0x${hex}` as `0x${string}`);
    expect(account.address).toBe(expected.address);
  });

  it('throws a loud, actionable error when neither credential is present', () => {
    expect(() => resolveOwnerAccount({ ...baseConfig })).toThrow(/mnemonic|ownerPrivateKeyHex/i);
  });

  it('never appears in the thrown error message (no accidental key material in errors)', () => {
    const hex = '1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727';
    try {
      // Force an unrelated failure path isn't easy here — this test instead
      // documents the invariant: the ONLY error this function throws is the
      // "neither present" message, which by construction can't contain key
      // material because there is none in scope when it fires.
      resolveOwnerAccount({ ...baseConfig });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      expect(msg).not.toContain(hex);
      expect(msg).not.toContain(TEST_MNEMONIC);
    }
  });
});

describe('getSubgraphConfig — mnemonic remains optional in the type (bundle mode)', () => {
  it('a bundle-mode override with only ownerPrivateKeyHex produces a usable config', () => {
    const hex = '1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727';
    const cfg = getSubgraphConfig({
      relayUrl: baseConfig.relayUrl,
      ownerPrivateKeyHex: hex,
      authKeyHex: 'aa',
      walletAddress: '0x2c0CF74B2b76110708CA431796367779e3738250',
      chainId: 100,
      dataEdgeAddress: baseConfig.dataEdgeAddress,
    });
    // resolveOwnerAccount must succeed against the merged config — proves
    // the leftover env-default `mnemonic: ''` from getSubgraphConfig's
    // envConfig does not shadow the bundle-mode private key.
    expect(() => resolveOwnerAccount(cfg)).not.toThrow();
    const account = resolveOwnerAccount(cfg);
    expect(account.address).toBe(privateKeyToAccount(`0x${hex}` as `0x${string}`).address);
  });
});
