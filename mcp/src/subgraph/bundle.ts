/**
 * TotalReclaw MCP — `derived-bundle-v1` credential bundle (Option E Phase 2 /
 * #581, P2-13 — the MCP adapter).
 *
 * A bundle is what an agent host holds *instead of* the BIP-39 recovery
 * phrase: the four HKDF-derived vault-global working keys plus a signing
 * key, but never the phrase or the 64-byte seed that reproduces it. See
 * `docs/specs/totalreclaw/client-consistency.md` ("Credential Material") for
 * the full cross-client contract this file implements.
 *
 * Shared-core-first (CLAUDE.md): every byte of derivation, parsing, and
 * validation lives in `rust/totalreclaw-core/src/bundle.rs` and crosses into
 * TS only via the `@totalreclaw/core` WASM bindings (`parseBundleV1`,
 * `validateBundleV1`, `deriveBundleFromMnemonic`). This module is a THIN
 * adapter: it types the wire shape, calls the WASM binding, and converts hex
 * fields to `Buffer` where the rest of the MCP codebase expects buffers. It
 * does not reimplement HKDF, hex validation, or any §4.7 invariant — the
 * mnemonic-mode `deriveKeys`/`deriveLshSeed` in `subgraph/crypto.ts` are the
 * pattern followed here: WASM-backed wrappers, no local crypto.
 *
 * Mirrors `python/src/totalreclaw/bundle.py`'s shape (frozen dataclasses,
 * zero logic) as closely as TS allows.
 *
 * **Dependency note / feature detection (release gate).** As of this
 * writing, no published `@totalreclaw/core` npm release exposes
 * `parseBundleV1` / `validateBundleV1` / `deriveBundleFromMnemonic` (checked:
 * latest dist-tag 2.6.0-rc.1 does not carry them). Because this package's
 * `package.json` dependency range (`^2.5.3`) is satisfied by versions that
 * lack the bindings, this module CANNOT assume `require('@totalreclaw/core')`
 * exposes them — doing so (e.g. `as typeof import('@totalreclaw/core')`)
 * would type-check locally against a hand-built WASM copy but fail `tsc`
 * against the real, published dependency in CI, which is exactly what
 * happened on the first revision of this file (see PR #618 review history).
 *
 * So every bundle-touching WASM call routes through `ensureBundleBindings()`,
 * which feature-detects the three functions at runtime and throws a single,
 * actionable error if they're absent — never a TS compile-time assumption
 * about what the installed package exports. **Mnemonic-mode code paths never
 * call anything in this file that touches `wasm.*`** (`bundleVaultToBuffers`
 * and `redactedBundleSummary` are pure functions over an already-parsed
 * bundle and need no core binding), so a host running the published
 * dependency with no bundle bindings is completely unaffected until it
 * actually encounters a `version: 2` credentials.json — at which point it
 * gets the actionable upgrade error instead of a silent misconfiguration.
 */

// eslint-disable-next-line @typescript-eslint/no-var-requires
const coreModule: unknown = require('@totalreclaw/core');

/**
 * The subset of `@totalreclaw/core`'s WASM surface this module needs.
 * Deliberately NOT `typeof import('@totalreclaw/core')` — that would assert
 * (at compile time, against whatever `.d.ts` happens to be installed) that
 * the three bundle functions exist, which is false for every published
 * release as of this writing and would make `tsc` pass locally (against a
 * hand-built WASM copy with matching typings) while failing in CI (against
 * the real, published package). This interface is our OWN contract, checked
 * against the require()'d module at runtime, not trusted from the module's
 * ambient types.
 */
interface BundleCoreBindings {
  parseBundleV1(json: string): unknown;
  validateBundleV1(json: string): void;
  deriveBundleFromMnemonic(
    mnemonic: string,
    chainId: bigint,
    provisionedBy: string,
    smartAccount: string,
  ): string;
}

/** `require('@totalreclaw/core')`, typed as a partial — every field is
 * checked for presence before use. Never cast to the full interface without
 * going through `ensureBundleBindings()`. */
const wasm = coreModule as Partial<BundleCoreBindings>;

/**
 * Best-effort installed-version lookup, for the error message only. Never
 * throws — a version string we can't determine still needs an actionable
 * error, just without the "installed version X" detail.
 *
 * Deliberately does NOT `require('@totalreclaw/core/package.json')` — the
 * published package's `exports` map only defines `.` and `./web`, so that
 * subpath throws `ERR_PACKAGE_PATH_NOT_EXPORTED` under Node's exports
 * enforcement (confirmed against the actual installed layout). Instead,
 * resolve the package's entry file and walk up the filesystem to find its
 * own `package.json` via plain `fs` access, which `exports` restrictions
 * don't gate.
 */
function getInstalledCoreVersion(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require('node:fs') as typeof import('node:fs');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const path = require('node:path') as typeof import('node:path');
    let dir = path.dirname(require.resolve('@totalreclaw/core'));
    for (let i = 0; i < 6; i++) {
      const candidate = path.join(dir, 'package.json');
      if (fs.existsSync(candidate)) {
        const pkg = JSON.parse(fs.readFileSync(candidate, 'utf-8')) as {
          name?: string;
          version?: string;
        };
        if (pkg.name === '@totalreclaw/core' && pkg.version) {
          return pkg.version;
        }
      }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  } catch {
    // best-effort
  }
  return 'unknown';
}

/**
 * Feature-detect the three bundle WASM bindings and return them narrowed to
 * `BundleCoreBindings`, or throw a single, actionable error. This is the
 * ONE guard every bundle entry point in this file (and only this file —
 * `credentials.ts` / `pair-remote-client.ts` / `index.ts` all call INTO this
 * module rather than `require('@totalreclaw/core')` directly, so this is
 * genuinely the single choke point) routes through before touching
 * `wasm.*`.
 */
function ensureBundleBindings(): BundleCoreBindings {
  if (
    typeof wasm.parseBundleV1 === 'function' &&
    typeof wasm.validateBundleV1 === 'function' &&
    typeof wasm.deriveBundleFromMnemonic === 'function'
  ) {
    return wasm as BundleCoreBindings;
  }
  const installed = getInstalledCoreVersion();
  throw new Error(
    'derived-bundle-v1: bundle-mode credentials require @totalreclaw/core >= 2.6.0 with bundle ' +
      `bindings (parseBundleV1/validateBundleV1/deriveBundleFromMnemonic); installed version ` +
      `${installed} lacks them — upgrade the @totalreclaw/core dependency. Mnemonic-mode ` +
      '(plaintext recovery phrase) credentials are unaffected by this — this error only fires ' +
      'when a credentials.json version: 2 (derived-bundle-v1) file is actually being read.',
  );
}

// ---------------------------------------------------------------------------
// Wire types — field names match the canonical JSON in
// client-consistency.md's "Credential Material" section exactly (snake_case;
// the WASM binding's `serde_wasm_bindgen` output preserves Rust struct field
// names verbatim — no camelCase rename layer here, deliberately, so the
// on-wire shape and the in-process shape never drift).
// ---------------------------------------------------------------------------

/** The four HKDF-derived vault-global working keys, hex-encoded (64 chars, no `0x`). */
export interface BundleVaultHex {
  encryption_key: string;
  dedup_key: string;
  auth_key: string;
  lsh_seed: string;
}

export interface BundleSigningOwnerEoa {
  kind: 'owner-eoa';
  private_key: string; // 64 hex chars
  address: string; // 0x-prefixed, EIP-55 checksummed
}

export interface BundleSigningSessionKey {
  kind: 'session-key';
  private_key: string;
  address: string;
  grant: unknown; // SessionKeyPermissionGrant v1 — opaque to this crate/adapter
}

export type BundleSigning = BundleSigningOwnerEoa | BundleSigningSessionKey;

export interface BundleAccountRef {
  smart_account: string; // 0x-prefixed, EIP-55 checksummed
  chain_id: number;
}

/** A fully parsed + `parse_bundle_v1`-validated `derived-bundle-v1` bundle. */
export interface DerivedBundleV1 {
  vault: BundleVaultHex;
  signing: BundleSigning;
  account: BundleAccountRef;
  provisioned_at: string;
  provisioned_by: string;
}

// ---------------------------------------------------------------------------
// Feature-detection surface — exported so callers (and tests) can check
// availability without triggering the throw, e.g. to skip a test with a
// visible reason rather than failing.
// ---------------------------------------------------------------------------

/** True iff the installed `@totalreclaw/core` exposes all three bundle
 * bindings. Never throws. */
export function hasBundleBindings(): boolean {
  return (
    typeof wasm.parseBundleV1 === 'function' &&
    typeof wasm.validateBundleV1 === 'function' &&
    typeof wasm.deriveBundleFromMnemonic === 'function'
  );
}

// ---------------------------------------------------------------------------
// Parsing / validation — delegates to @totalreclaw/core. Never re-implements
// any derived-bundle-v1.md §4.7 invariant locally.
// ---------------------------------------------------------------------------

/**
 * Parse and fully validate a `derived-bundle-v1` JSON string.
 *
 * Runs every §4.7 invariant inside `totalreclaw-core::bundle::parse_bundle_v1`
 * (wrong version/schema, malformed hex, unknown `signing.kind`, an
 * `owner-eoa` bundle carrying a `grant`, a `session-key` bundle with an
 * inconsistent `grant`, `signing.address` not matching `address(private_key)`,
 * …). Throws a plain `Error` — never a silent downgrade — on any violation.
 *
 * The thrown error's message is core's own message, which never contains
 * hex key material (core's validation errors describe *shape* violations,
 * not values).
 *
 * Throws `ensureBundleBindings()`'s actionable upgrade error if the
 * installed `@totalreclaw/core` lacks the bundle bindings — checked BEFORE
 * any attempt to call into the WASM module.
 */
export function parseBundleV1(json: string): DerivedBundleV1 {
  const bindings = ensureBundleBindings();
  // bindings.parseBundleV1 throws a JsError on any validation failure —
  // surface it as a normal Error with a stable, actionable prefix so
  // callers don't need to know this crossed a WASM boundary.
  let parsed: unknown;
  try {
    parsed = bindings.parseBundleV1(json);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`derived-bundle-v1: invalid bundle — ${msg}`);
  }
  return parsed as DerivedBundleV1;
}

/**
 * Validate a `derived-bundle-v1` JSON string without returning the parsed
 * object. Throws on any violation; no-op on success.
 */
export function validateBundleV1(json: string): void {
  const bindings = ensureBundleBindings();
  try {
    bindings.validateBundleV1(json);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`derived-bundle-v1: invalid bundle — ${msg}`);
  }
}

/**
 * Derive a `derived-bundle-v1` bundle from a BIP-39 mnemonic.
 *
 * NOT part of MCP's read-side credential-loading scope (P2-13) — MCP never
 * derives a bundle from a mnemonic on its own initiative; that is Hermes's
 * `local-migration` job (P2-9) or the SPA's `provisioned_by: "spa"` job.
 * Exported anyway because the test fixtures (parity + pair-bundle tests)
 * need a way to mint a bundle for `parseBundleV1` round-trip coverage
 * without hand-rolling JSON, and because a future MCP-side migration tool
 * (tracked as a known gap — see mcp/README.md) will need exactly this call.
 */
export function deriveBundleFromMnemonic(
  mnemonic: string,
  chainId: number,
  provisionedBy: string,
  smartAccount: string,
): string {
  const bindings = ensureBundleBindings();
  return bindings.deriveBundleFromMnemonic(mnemonic.trim(), BigInt(chainId), provisionedBy, smartAccount);
}

// ---------------------------------------------------------------------------
// Buffer conversion — the rest of the MCP codebase (subgraph/crypto.ts,
// subgraph/lsh.ts, encrypt/decrypt) works in `Buffer`, not hex strings.
// Pure function over an already-parsed bundle — touches no WASM binding, so
// it needs no feature-detection guard and works identically regardless of
// whether the installed core has bundle bindings (by the time a caller has
// a `DerivedBundleV1` to pass in, `parseBundleV1` already succeeded).
// ---------------------------------------------------------------------------

export interface BundleVaultBuffers {
  encryptionKey: Buffer;
  dedupKey: Buffer;
  authKey: Buffer;
  lshSeed: Buffer;
}

/** Convert a parsed bundle's hex vault keys to `Buffer`s. */
export function bundleVaultToBuffers(bundle: DerivedBundleV1): BundleVaultBuffers {
  return {
    encryptionKey: Buffer.from(bundle.vault.encryption_key, 'hex'),
    dedupKey: Buffer.from(bundle.vault.dedup_key, 'hex'),
    authKey: Buffer.from(bundle.vault.auth_key, 'hex'),
    lshSeed: Buffer.from(bundle.vault.lsh_seed, 'hex'),
  };
}

// ---------------------------------------------------------------------------
// Redaction — derived-bundle-v1.md §4.6 point 5: no value under `vault` or
// `signing.private_key` may ever be logged, printed, or included in an
// exception message. Python's `bundle.py` enforces this with `__repr__`
// overrides on frozen dataclasses; TS has no dataclass-repr equivalent, so
// every call site that might log a bundle MUST route through this helper
// instead of `JSON.stringify(bundle)` / `console.error(bundle)` directly.
// Also touches no WASM binding — pure function, no feature-detection needed.
// ---------------------------------------------------------------------------

/**
 * A safe-to-log summary of a bundle: everything EXCEPT `vault.*` and
 * `signing.private_key`. Use this (never the raw bundle object) in any
 * `console.error` / log line that touches a bundle.
 */
export function redactedBundleSummary(bundle: DerivedBundleV1): Record<string, unknown> {
  return {
    schema: 'derived-bundle-v1',
    signing_kind: bundle.signing.kind,
    signing_address: bundle.signing.address,
    smart_account: bundle.account.smart_account,
    chain_id: bundle.account.chain_id,
    provisioned_at: bundle.provisioned_at,
    provisioned_by: bundle.provisioned_by,
  };
}
