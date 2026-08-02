/**
 * Keychain-marker detection for the shared `credentials.json` (#545).
 *
 * #546 (Python/Hermes side — `python/src/totalreclaw/credentials_wrap.py`)
 * wraps the recovery phrase in the OS keychain on desktop and writes a
 * non-secret **marker** into `~/.totalreclaw/credentials.json` in place of
 * the mnemonic:
 *
 *   __keychain__:v1:<eoa-address>
 *
 * ALL clients share that one file. The OpenClaw plugin does not implement
 * the OS-keychain wrap itself (that ships from the Python/Hermes side
 * only), but when a co-installed Hermes client wraps the phrase on the
 * same machine, this plugin's `initialize()` (index.ts) would otherwise
 * read the marker as if it were the phrase and hand it to `deriveKeys()`,
 * which throws an unhandled `invalid mnemonic: expected 12 or 24 words,
 * got 1` — a hard crash. Safety was never at risk (the marker fails
 * BIP-39 validation loudly everywhere, so no wrong-wallet derivation is
 * possible); this module exists purely so `initialize()` can recognize
 * the marker up front and fail SOFT (`needsSetup = true` + a clear,
 * actionable message) instead of crashing.
 *
 * Mirrors the Python side's `MARKER_PREFIX` / `is_marker` exactly — see
 * `python/src/totalreclaw/credentials_wrap.py`.
 *
 * The prefix itself is a non-secret, well-known constant. The payload
 * after it (the wallet address) is also non-secret on its own, but this
 * module never echoes ANY marker value in logs or messages — only the
 * fixed guidance string below.
 */

/** Prefix that identifies a keychain-wrapped marker in place of a phrase. */
export const KEYCHAIN_MARKER_PREFIX = '__keychain__:v1:';

/**
 * True iff `value` is a keychain-wrapped marker string rather than a
 * usable recovery phrase. Never true for `undefined`/`null`/non-strings.
 */
export function isKeychainMarker(value: unknown): value is string {
  return typeof value === 'string' && value.startsWith(KEYCHAIN_MARKER_PREFIX);
}

/**
 * Actionable, non-secret message logged when `initialize()` detects a
 * keychain marker in `credentials.json` instead of a usable phrase. A
 * fixed constant — never interpolate the marker's payload into it.
 */
export const KEYCHAIN_MARKER_SETUP_MSG =
  'TotalReclaw: credentials.json holds a recovery phrase wrapped by the OS keychain ' +
  '(written by a co-installed Hermes client on this machine — see #546). This plugin ' +
  'cannot read OS-keychain-wrapped secrets directly. Fix: set TOTALRECLAW_RECOVERY_PHRASE ' +
  'for this client, or run the Hermes client with TOTALRECLAW_NO_KEYCHAIN=1 so it writes ' +
  'the plaintext phrase to credentials.json instead.';
