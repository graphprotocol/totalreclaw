/**
 * Generator for `derived-bundle-v1.json` — the cross-language parity fixture
 * for the Option E Phase 2 `derived-bundle-v1` credential bundle (#581).
 *
 * Locks byte-equality of `derive_bundle_from_mnemonic`'s deterministic
 * fields (vault keys, signing key + address, account) across:
 *
 *   - Rust (canonical) — `rust/totalreclaw-core/src/bundle.rs`
 *   - TypeScript / WASM — `deriveBundleFromMnemonic` (this generator; also
 *     asserted in `derived-bundle-parity.test.ts`)
 *   - Python / PyO3 — `python/tests/test_bundle_parity.py`
 *
 * `provisioned_at` is deliberately excluded from the byte-equality
 * assertions everywhere: `derive_bundle_from_mnemonic` stamps it with
 * `Utc::now()` at derivation time (matching the existing `prepare_fact` /
 * `encode_tombstone_protobuf` precedent of wall-clock timestamps generated
 * inside core), so it is never reproducible across runs. It is
 * "informational only" per derived-bundle-v1.md §4.1 — no code path depends
 * on its value, only that it parses as RFC 3339.
 *
 * Run (regenerates the fixture; build the flat WASM pkg first):
 *   cd rust/totalreclaw-core && wasm-pack build --target nodejs --out-dir pkg --features wasm
 *   cd tests/parity && npx tsx fixtures/generate-derived-bundle-v1.ts
 *   -- or --
 *   cd tests/parity && node --experimental-strip-types fixtures/generate-derived-bundle-v1.ts
 */

import { writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// Load the WASM bindings (CJS module produced by `wasm-pack build --target
// nodejs --out-dir pkg`), mirroring kg-phase1-parity.test.ts's loading
// pattern — the flat `pkg/` build, not the composed nodejs+web split
// `build-wasm.sh` produces for publishing.
const wasmPath = resolve(
  __dirname,
  '..',
  '..',
  '..',
  'rust',
  'totalreclaw-core',
  'pkg',
  'totalreclaw_core.js',
);
const wasm = require(wasmPath) as {
  deriveBundleFromMnemonic: (
    mnemonic: string,
    chainId: bigint,
    provisionedBy: string,
    smartAccount: string,
  ) => string;
};

// ---------------------------------------------------------------------------
// Deterministic inputs — the canonical BIP-39 test vector used throughout
// the parity suite (tests/parity/generate-fixtures.ts,
// python/tests/fixtures/crypto_vectors.json).
// ---------------------------------------------------------------------------

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
const CHAIN_ID = 100n; // Gnosis mainnet, single-chain policy
const PROVISIONED_BY = 'local-migration';
// Reuses the same fixture Smart Account as
// tests/parity/fixtures/generate-session-key-grant-v1.ts — not derived from
// TEST_MNEMONIC (this crate has no CREATE2 helper; smart_account is a
// required caller-supplied argument — see bundle.rs's module doc).
const SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250';

function main(): void {
  const json = wasm.deriveBundleFromMnemonic(TEST_MNEMONIC, CHAIN_ID, PROVISIONED_BY, SMART_ACCOUNT);
  const bundle = JSON.parse(json);

  const fixture = {
    _comment:
      'Cross-language parity fixture for derived-bundle-v1 (Option E Phase 2, #581). ' +
      'Every implementation MUST derive byte-identical vault/signing/account fields ' +
      'from the same inputs below. `provisioned_at` is excluded from equality checks ' +
      '(wall-clock-stamped, informational only — see this generator\'s header comment). ' +
      'Do not edit manually — regenerate via fixtures/generate-derived-bundle-v1.ts.',
    _generated_at: new Date().toISOString(),
    inputs: {
      mnemonic: TEST_MNEMONIC,
      chain_id: Number(CHAIN_ID),
      provisioned_by: PROVISIONED_BY,
      smart_account: SMART_ACCOUNT,
    },
    // The deterministic subset of the bundle every implementation must match
    // byte-for-byte. Mirrors bundle.rs's DerivedBundleV1 shape minus
    // provisioned_at.
    expected: {
      vault: bundle.vault,
      signing: bundle.signing,
      account: bundle.account,
      provisioned_by: bundle.provisioned_by,
    },
    // A full example bundle (including a real provisioned_at) for
    // documentation / round-trip sanity — NOT asserted verbatim by any test.
    example_full_bundle: bundle,
  };

  const outPath = join(__dirname, 'derived-bundle-v1.json');
  writeFileSync(outPath, `${JSON.stringify(fixture, null, 2)}\n`, 'utf8');
  console.log(`wrote ${outPath}`);
  console.log(`  vault.encryption_key: ${bundle.vault.encryption_key.slice(0, 16)}...`);
  console.log(`  signing.kind:         ${bundle.signing.kind}`);
  console.log(`  signing.address:      ${bundle.signing.address}`);
  console.log(`  account.smart_account:${bundle.account.smart_account}`);
}

main();
