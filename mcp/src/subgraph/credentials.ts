/**
 * TotalReclaw MCP — managed-mode credential resolution (Option E Phase 2 /
 * #581, P2-13).
 *
 * Reads `~/.totalreclaw/credentials.json` (or `TOTALRECLAW_RECOVERY_PHRASE`)
 * and resolves EITHER a BIP-39 mnemonic OR a `derived-bundle-v1` bundle,
 * mirroring the precedence `python/src/totalreclaw/agent/state.py::_try_auto_configure`
 * implements for Hermes:
 *
 *   1. `TOTALRECLAW_RECOVERY_PHRASE` env — retained, deprecated (phrase).
 *   2. `credentials.json` `version: 2` — a `derived-bundle-v1` bundle.
 *   3. `credentials.json` legacy — plaintext `{"mnemonic": "…"}`.
 *
 * Deliberately narrower than the Python precedence chain: MCP has no
 * `TOTALRECLAW_CREDENTIALS_PROVIDER` / external-provider transport
 * (`credential_provider.py`) and no OS-keychain unwrap
 * (`credentials_wrap.py`) — both are tracked as explicit known gaps (see
 * `mcp/README.md`, CLAUDE.md's Known Gaps table) rather than silently
 * unsupported. A `keychain_wrapped: true` v2 file is a LOUD, actionable
 * error here, not a silent skip — the same "never silently downgrade"
 * posture the unknown-`version` branch below takes.
 *
 * This module never reads self-hosted credentials (`ClientState`/
 * `MASTER_PASSWORD` in `index.ts`) — self-hosted mode is a completely
 * separate credential system with no BIP-39 root at all (derived-bundle-v1.md
 * §4.5.1) and callers MUST route around this module entirely when
 * `TOTALRECLAW_SELF_HOSTED=true`. See `index.ts`'s `main()` for the gate.
 */

import * as fs from 'node:fs';

import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';

import { parseBundleV1, type DerivedBundleV1 } from './bundle.js';
import { CREDENTIALS_PATH } from '../cli/setup.js';

/**
 * A credentials.json this server recognises as a real credential written by
 * another TotalReclaw client, in a form the MCP server cannot read: an
 * OS-keychain-wrapped file (Hermes on a desktop host, either the v1
 * mnemonic marker or a `version: 2` bundle with `keychain_wrapped: true`),
 * or a `version` newer than this build knows.
 *
 * Distinct from the plain `Error`s below (a `version: 2` bundle that fails
 * `parseBundleV1`'s §4.7 validation): those are evidence of a corrupt or
 * tampered credential store and stay fatal at startup. This one is not —
 * the file is presumably fine, this server just can't use it — so
 * `index.ts` starts in `unconfigured` mode and reports `message` on every
 * tool call instead of crashing (a crash makes the host drop all tools and
 * leaves the reason in a log file the user never sees). Still never a silent
 * downgrade: nothing in the file is used, and `totalreclaw_pair` refuses to
 * overwrite it.
 *
 * `message` never contains any value from the file.
 */
export class UnsupportedCredentialsError extends Error {
  readonly reason: 'keychain_wrapped' | 'unknown_version';
  constructor(reason: 'keychain_wrapped' | 'unknown_version', message: string) {
    super(message);
    this.name = 'UnsupportedCredentialsError';
    this.reason = reason;
  }
}

/** Hermes' v1 keychain marker (`credentials_wrap.py`): `"__keychain__:v1:0x<eoa>"`. */
const KEYCHAIN_MARKER_PREFIX = '__keychain__:';

const KEYCHAIN_MESSAGE =
  'The credentials file at ~/.totalreclaw/credentials.json keeps its secret in the OS ' +
  'keychain (another TotalReclaw client, such as Hermes, set it up that way). The MCP ' +
  'server cannot read the OS keychain yet (a known gap; see mcp/README.md), so TotalReclaw ' +
  'is not available in this app for now. The file and the account are untouched, and ' +
  'pairing here is blocked so it cannot replace that account.';

export type ResolvedCredential =
  | { kind: 'mnemonic'; mnemonic: string }
  | { kind: 'bundle'; bundle: DerivedBundleV1 };

export interface ResolveManagedCredentialOptions {
  /** Override the credentials.json path — for tests. */
  credentialsPath?: string;
  /** Override the env — for tests. */
  env?: NodeJS.ProcessEnv;
}

function isValidBip39Mnemonic(candidate: string): boolean {
  const words = candidate.split(/\s+/);
  if (words.length !== 12 && words.length !== 24) return false;
  const allWordsValid = words.every((w: string) => wordlist.includes(w));
  return validateMnemonic(candidate, wordlist) || allWordsValid;
}

/**
 * Resolve the managed-mode credential per the precedence above.
 *
 * Returns `undefined` when nothing is configured (equivalent to today's
 * `resolveMnemonic()` returning `undefined` — falls through to
 * `unconfigured` mode).
 *
 * Throws — never returns — on a credentials.json that is present but
 * must not be silently downgraded:
 *   - `UnsupportedCredentialsError`: `version` present and not `2`
 *     (derived-bundle-v1.md §4.7 / the client-consistency contract: unknown
 *     version is a loud error), or an OS-keychain-wrapped file (v1 marker or
 *     `version === 2` with `keychain_wrapped: true`; MCP has no keychain
 *     unwrap — see the module doc). `index.ts` starts unconfigured on these.
 *   - plain `Error`: `version === 2` and the bundle fails `parseBundleV1`'s
 *     §4.7 validation (malformed hex, unknown `signing.kind`, address/key
 *     mismatch, …). Fatal at startup.
 *
 * A missing file, an unreadable file, or a file that fails to parse as JSON
 * at all are treated as "nothing configured" (`undefined`) — matching the
 * pre-existing `resolveMnemonic()` behaviour exactly, since a bare ENOENT or
 * a syntax error is not evidence of tampering the way a well-formed-but-
 * wrong `version` is.
 */
export function resolveManagedCredential(
  opts: ResolveManagedCredentialOptions = {},
): ResolvedCredential | undefined {
  const env = opts.env ?? process.env;

  // 1. TOTALRECLAW_RECOVERY_PHRASE env — retained, deprecated.
  const envPhrase = env.TOTALRECLAW_RECOVERY_PHRASE;
  if (envPhrase && envPhrase.trim().length > 0) {
    // #618 adversarial-review NIT (mirrors
    // python/src/totalreclaw/agent/state.py's equivalent env-branch
    // logging): the env phrase wins the precedence race unconditionally,
    // which silently shadows a v2 bundle if one also happens to exist at
    // the same path — a state an operator mid-migration (env var not yet
    // unset after provisioning a bundle) could easily land in without
    // noticing. Best-effort, plain JSON peek only (no parseBundleV1 — this
    // is a diagnostic, not a validation path); never throws.
    try {
      const credentialsPath = opts.credentialsPath ?? CREDENTIALS_PATH;
      const raw = fs.readFileSync(credentialsPath, 'utf-8');
      const parsed = JSON.parse(raw) as { version?: unknown };
      if (parsed.version === 2) {
        console.error(
          'TotalReclaw: TOTALRECLAW_RECOVERY_PHRASE is set and takes precedence over the ' +
            `derived-bundle-v1 bundle at ${credentialsPath} (version: 2), which is being ` +
            'shadowed. Unset the env var to use the bundle, or remove the bundle file if the ' +
            'env-var phrase is the intended credential.',
        );
      }
    } catch {
      // No file, unreadable, or not JSON — nothing to shadow, stay silent.
    }
    return { kind: 'mnemonic', mnemonic: envPhrase.trim() };
  }

  // 2/3. credentials.json
  const credentialsPath = opts.credentialsPath ?? CREDENTIALS_PATH;
  let raw: string;
  try {
    raw = fs.readFileSync(credentialsPath, 'utf-8');
  } catch {
    return undefined; // no file — unconfigured, not an error.
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return undefined; // unparsable — treated as unconfigured, matches legacy behaviour.
  }

  if (typeof parsed !== 'object' || parsed === null) {
    return undefined;
  }
  const obj = parsed as Record<string, unknown>;

  const version = obj.version;
  // OS-keychain-wrapped file (Hermes desktop storage), v1 marker or v2
  // bundle. MCP has no keychain unwrap (derived-bundle-v1.md §4.3,
  // mcp/README.md Known Gaps). Checked before the version switch so the v1
  // marker (no `version` key) gets the same actionable error instead of
  // silently reading as "nothing configured".
  const mnemonicMarker =
    typeof obj.mnemonic === 'string' && obj.mnemonic.trim().startsWith(KEYCHAIN_MARKER_PREFIX);
  if (obj.keychain_wrapped === true || mnemonicMarker) {
    if (version === undefined || version === 2) {
      throw new UnsupportedCredentialsError('keychain_wrapped', KEYCHAIN_MESSAGE);
    }
  }
  if (version === 2) {
    // parseBundleV1 runs every derived-bundle-v1.md §4.7 invariant and
    // throws a loud, actionable Error on any violation — never a silent
    // downgrade to legacy mnemonic handling.
    const bundle = parseBundleV1(JSON.stringify(obj));
    return { kind: 'bundle', bundle };
  }
  if (version !== undefined) {
    // `version` is a number or short string in every real file; cap it so a
    // hostile file can't stuff arbitrary content into the message.
    const shown = JSON.stringify(version).slice(0, 16);
    throw new UnsupportedCredentialsError(
      'unknown_version',
      `credentials.json has an unrecognised version=${shown}. This MCP server reads ` +
        'version 2 (a derived-bundle-v1 bundle) or no version key at all (legacy plaintext ' +
        'mnemonic). It may have been written by a newer TotalReclaw client; updating ' +
        '@totalreclaw/mcp-server may fix it. Refusing to guess, so nothing in the file is ' +
        'used, and pairing here is blocked so it cannot replace that account.',
    );
  }

  // 4. Legacy: plaintext {"mnemonic": "…"}.
  const mnemonicField = obj.mnemonic;
  if (typeof mnemonicField === 'string' && mnemonicField.trim().length > 0) {
    const trimmed = mnemonicField.trim();
    if (isValidBip39Mnemonic(trimmed)) {
      return { kind: 'mnemonic', mnemonic: trimmed };
    }
  }

  return undefined;
}

// ---------------------------------------------------------------------------
// Self-hosted diagnostic (#618 adversarial-review item 2). Lives here
// (rather than inline in `index.ts`) so it has direct test coverage —
// `index.ts` cannot be unit-tested (importing it executes `main()`).
// ---------------------------------------------------------------------------

/**
 * Diagnostic-only check: does the credentials file at `credentialsPath`
 * look like a `derived-bundle-v1` `version: 2` file? Plain `JSON.parse`,
 * no WASM, no `parseBundleV1` — the only caller
 * (`index.ts`'s `TOTALRECLAW_SELF_HOSTED` branch in `main()`) must never
 * touch bundle parsing/validation at all; self-hosted mode is a wholly
 * separate credential system (derived-bundle-v1.md §4.5.1) and this
 * function exists purely to produce a more accurate diagnostic message —
 * "is the `version` field present and `2`" — when self-hosted mode finds
 * no usable mnemonic and would otherwise tell the operator to "set
 * TOTALRECLAW_RECOVERY_PHRASE", which is actively wrong when a
 * valid-but-wrong-mode bundle credential is already present.
 *
 * Never throws; a missing/corrupt/non-bundle file is simply `false`.
 */
export function isV2BundleCredentialsFile(credentialsPath: string): boolean {
  try {
    const raw = fs.readFileSync(credentialsPath, 'utf-8');
    const parsed = JSON.parse(raw) as { version?: unknown };
    return parsed.version === 2;
  } catch {
    return false;
  }
}
