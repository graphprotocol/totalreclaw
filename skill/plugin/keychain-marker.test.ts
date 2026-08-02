/**
 * Tests for the keychain-marker helper (#545).
 *
 * #546 (Python/Hermes side, python/src/totalreclaw/credentials_wrap.py)
 * wraps the recovery phrase in the OS keychain on desktop and writes a
 * non-secret marker into the shared `~/.totalreclaw/credentials.json` in
 * place of the mnemonic: `__keychain__:v1:<eoa-address>`. All clients read
 * the same file, so a co-installed OpenClaw plugin must recognize the
 * marker and fail soft (not crash inside `deriveKeys()`).
 *
 * This file only covers the marker-detection helper itself
 * (`skill/plugin/keychain-marker.ts`) — the crash-site fix in
 * `initialize()` (skill/plugin/index.ts) is exercised end-to-end by manual
 * QA / the PR description, since `initialize()` pulls in the WASM crypto
 * core + a live API client and isn't practically unit-testable in
 * isolation (no existing test in this package does so either).
 *
 * Run with: npx tsx keychain-marker.test.ts
 *
 * TAP-style output, no jest dependency (mirrors
 * pairing/credentials-bootstrap.test.ts).
 */

import {
  KEYCHAIN_MARKER_PREFIX,
  KEYCHAIN_MARKER_SETUP_MSG,
  isKeychainMarker,
} from './keychain-marker.js';

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// 1. Prefix constant matches the Python side exactly.
// ---------------------------------------------------------------------------
{
  assert(
    KEYCHAIN_MARKER_PREFIX === '__keychain__:v1:',
    'KEYCHAIN_MARKER_PREFIX matches python credentials_wrap.MARKER_PREFIX',
  );
}

// ---------------------------------------------------------------------------
// 2. isKeychainMarker: true for a well-formed marker.
// ---------------------------------------------------------------------------
{
  const marker = `${KEYCHAIN_MARKER_PREFIX}0x${'a'.repeat(40)}`;
  assert(isKeychainMarker(marker) === true, 'isKeychainMarker true for a constructed marker');
}

// ---------------------------------------------------------------------------
// 3. isKeychainMarker: false for a real 12-word plaintext mnemonic.
// ---------------------------------------------------------------------------
{
  const mnemonic =
    'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
  assert(isKeychainMarker(mnemonic) === false, 'isKeychainMarker false for plaintext mnemonic');
}

// ---------------------------------------------------------------------------
// 4. isKeychainMarker: false for empty string, undefined, null, non-string.
// ---------------------------------------------------------------------------
{
  assert(isKeychainMarker('') === false, 'isKeychainMarker false for empty string');
  assert(isKeychainMarker(undefined) === false, 'isKeychainMarker false for undefined');
  assert(isKeychainMarker(null) === false, 'isKeychainMarker false for null');
  assert(isKeychainMarker(42) === false, 'isKeychainMarker false for a number');
  assert(
    isKeychainMarker('__keychain__') === false,
    'isKeychainMarker false for the bare prefix stem without the v1: suffix',
  );
}

// ---------------------------------------------------------------------------
// 5. isKeychainMarker: false for a string that merely CONTAINS the prefix
//    (must be startsWith, not a substring match).
// ---------------------------------------------------------------------------
{
  const notAPrefix = `some text ${KEYCHAIN_MARKER_PREFIX}0xdeadbeef`;
  assert(
    isKeychainMarker(notAPrefix) === false,
    'isKeychainMarker false when the marker prefix is not at the start of the string',
  );
}

// ---------------------------------------------------------------------------
// 6. The setup message is actionable and NEVER echoes a marker payload
//    (the wallet address / account suffix) — only the static, non-secret
//    guidance text.
// ---------------------------------------------------------------------------
{
  assert(
    typeof KEYCHAIN_MARKER_SETUP_MSG === 'string' && KEYCHAIN_MARKER_SETUP_MSG.length > 0,
    'KEYCHAIN_MARKER_SETUP_MSG is a non-empty string',
  );
  assert(
    /TOTALRECLAW_RECOVERY_PHRASE/.test(KEYCHAIN_MARKER_SETUP_MSG),
    'KEYCHAIN_MARKER_SETUP_MSG points to the TOTALRECLAW_RECOVERY_PHRASE escape hatch',
  );
  assert(
    /TOTALRECLAW_NO_KEYCHAIN/.test(KEYCHAIN_MARKER_SETUP_MSG),
    'KEYCHAIN_MARKER_SETUP_MSG mentions the TOTALRECLAW_NO_KEYCHAIN kill-switch',
  );
  // The message is a fixed constant, not built from any marker/account
  // value — assert it does not embed anything that looks like a derived
  // 0x-address payload (which would only happen if a caller mistakenly
  // interpolated a real marker into it).
  assert(!/0x[0-9a-fA-F]{40}/.test(KEYCHAIN_MARKER_SETUP_MSG), 'KEYCHAIN_MARKER_SETUP_MSG never embeds an address payload');
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
