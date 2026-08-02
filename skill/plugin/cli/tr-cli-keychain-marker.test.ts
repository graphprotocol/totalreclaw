/**
 * Wiring-level test for the #545 keychain-marker guard in tr-cli.ts.
 *
 * The helper-only unit tests in `../keychain-marker.test.ts` cover
 * `isKeychainMarker()` / `KEYCHAIN_MARKER_SETUP_MSG` in isolation, but they
 * don't prove the guard is actually WIRED into every read→derive call site.
 * A Sonnet-high review of this PR found exactly that gap: `index.ts`
 * `initialize()` was patched, but `cli/tr-cli.ts` `buildContext()` — the
 * shared credential-load path behind `tr remember`, `tr forget`, and
 * `tr export` — still fed a keychain marker straight into
 * `setRecoveryPhraseOverride()` + `deriveKeys()`, reproducing the exact
 * unhandled "invalid mnemonic: expected 12 or 24 words, got 1" crash this
 * PR exists to fix.
 *
 * `buildContext()`'s credential-read+marker-guard prelude was extracted
 * into the exported `resolveCliMnemonicOrDie()` specifically so this test
 * can drive it IN-PROCESS — no reliance on Node's subprocess/spawn APIs.
 * (An earlier revision of this test shelled out to run `tr-cli.ts` as a
 * separate process; the plugin's install scanner (`scripts/check-scanner.mjs`,
 * `SHELL_EXEC_PATTERN`) refuses ANY file in this tree that so much as
 * imports that module — bare-pattern match, no exceptions — because
 * OpenClaw's installer refuses plugins that can shell out. Rewritten here
 * to stub `process.exit` / `process.stderr.write` instead, same technique
 * `pairing/credentials-bootstrap.test.ts` and friends use for in-process
 * CLI-adjacent testing.)
 *
 * Asserts:
 *   - `resolveCliMnemonicOrDie()` calls `die()` (captured via a stubbed
 *     `process.exit` that throws instead of terminating the process) on a
 *     marker-shaped `credentials.json` fixture, for both the `mnemonic`
 *     and legacy `recovery_phrase` fields
 *   - the captured stderr carries `KEYCHAIN_MARKER_SETUP_MSG` verbatim
 *   - the raw BIP-39 crash text ("invalid mnemonic") never appears —
 *     proving `setRecoveryPhraseOverride()`/`deriveKeys()` were never
 *     reached (the function returns/exits before either could run)
 *   - the marker's payload is never echoed
 *   - a plaintext mnemonic does NOT trip the guard (control case) and is
 *     returned unchanged
 *
 * Run with: npx tsx cli/tr-cli-keychain-marker.test.ts
 *
 * TAP-style output, no jest dependency (mirrors keychain-marker.test.ts /
 * pairing/credentials-bootstrap.test.ts).
 */

import { resolveCliMnemonicOrDie } from './tr-cli.js';
import { KEYCHAIN_MARKER_SETUP_MSG } from '../keychain-marker.js';

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

/** Thrown by the stubbed `process.exit` in place of actually terminating. */
class FakeProcessExit extends Error {
  constructor(public code: number) {
    super(`process.exit(${code})`);
  }
}

/**
 * Run `fn` with `process.exit` and `process.stderr.write` stubbed, so a
 * `die()` call inside `fn` unwinds via a catchable error instead of
 * actually terminating this test process. Returns whichever of
 * `{ value }` (normal return) or `{ exitCode, stderr }` (die() was hit)
 * applies.
 */
function captureDie<T>(fn: () => T): { value?: T; exitCode?: number; stderr: string } {
  const originalExit = process.exit;
  const originalWrite = process.stderr.write.bind(process.stderr);
  let stderr = '';

  process.exit = ((code?: number): never => {
    throw new FakeProcessExit(code ?? 0);
  }) as typeof process.exit;

  process.stderr.write = ((chunk: unknown): boolean => {
    stderr += typeof chunk === 'string' ? chunk : String(chunk);
    return true;
  }) as typeof process.stderr.write;

  try {
    const value = fn();
    return { value, stderr };
  } catch (err) {
    if (err instanceof FakeProcessExit) {
      return { exitCode: err.code, stderr };
    }
    throw err;
  } finally {
    process.exit = originalExit;
    process.stderr.write = originalWrite;
  }
}

// A syntactically-plausible fake marker — NOT a real wallet, this is a
// fixture, and the test never echoes it in an assertion message.
const FAKE_MARKER = '__keychain__:v1:0x' + 'b'.repeat(40);

// ---------------------------------------------------------------------------
// 1. marker-shaped credentials (mnemonic field) → die(), not a BIP-39 crash
// ---------------------------------------------------------------------------
{
  const { exitCode, stderr, value } = captureDie(() =>
    resolveCliMnemonicOrDie({ mnemonic: FAKE_MARKER, keychain_wrapped: true }),
  );

  assert(exitCode === 1, `mnemonic field: die() exits with code 1 (got ${exitCode})`);
  assert(value === undefined, 'mnemonic field: resolveCliMnemonicOrDie never returns a value');
  assert(
    stderr.includes(KEYCHAIN_MARKER_SETUP_MSG),
    'mnemonic field: stderr carries KEYCHAIN_MARKER_SETUP_MSG verbatim',
  );
  assert(
    !/invalid mnemonic/i.test(stderr),
    'mnemonic field: stderr does NOT contain the raw BIP-39 "invalid mnemonic" crash text (setRecoveryPhraseOverride/deriveKeys never reached)',
  );
  assert(!stderr.includes(FAKE_MARKER), 'mnemonic field: stderr never echoes the marker payload');
}

// ---------------------------------------------------------------------------
// 2. marker-shaped credentials (legacy recovery_phrase field) → same
// ---------------------------------------------------------------------------
{
  const { exitCode, stderr } = captureDie(() =>
    resolveCliMnemonicOrDie({ recovery_phrase: FAKE_MARKER, keychain_wrapped: true }),
  );

  assert(exitCode === 1, `recovery_phrase field: die() exits with code 1 (got ${exitCode})`);
  assert(
    stderr.includes(KEYCHAIN_MARKER_SETUP_MSG),
    'recovery_phrase field: stderr carries KEYCHAIN_MARKER_SETUP_MSG verbatim',
  );
  assert(!/invalid mnemonic/i.test(stderr), 'recovery_phrase field: no raw BIP-39 crash text');
}

// ---------------------------------------------------------------------------
// 3. no credentials.json at all → the pre-existing "not set up" die(),
//    unaffected by the marker guard (regression guard for the refactor).
// ---------------------------------------------------------------------------
{
  const { exitCode, stderr } = captureDie(() => resolveCliMnemonicOrDie(null));

  assert(exitCode === 1, `missing credentials: die() exits with code 1 (got ${exitCode})`);
  assert(
    stderr.includes('not set up'),
    'missing credentials: stderr is the pre-existing "not set up" message, not the marker message',
  );
  assert(
    !stderr.includes(KEYCHAIN_MARKER_SETUP_MSG),
    'missing credentials: marker message is NOT shown when there is no credentials.json at all',
  );
}

// ---------------------------------------------------------------------------
// 4. empty mnemonic field → the pre-existing "no recovery phrase" die(),
//    unaffected by the marker guard.
// ---------------------------------------------------------------------------
{
  const { exitCode, stderr } = captureDie(() => resolveCliMnemonicOrDie({ mnemonic: '' }));

  assert(exitCode === 1, `empty mnemonic: die() exits with code 1 (got ${exitCode})`);
  assert(
    stderr.includes('No recovery phrase'),
    'empty mnemonic: stderr is the pre-existing "no recovery phrase" message',
  );
}

// ---------------------------------------------------------------------------
// 5. sanity control: a plaintext (non-marker) mnemonic does NOT trip the
//    marker guard — resolveCliMnemonicOrDie returns it unchanged, no die().
// ---------------------------------------------------------------------------
{
  const PLAIN_MNEMONIC =
    'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';
  const { value, exitCode, stderr } = captureDie(() =>
    resolveCliMnemonicOrDie({ mnemonic: PLAIN_MNEMONIC }),
  );

  assert(exitCode === undefined, 'control: a plaintext mnemonic never trips die()');
  assert(value === PLAIN_MNEMONIC, 'control: resolveCliMnemonicOrDie returns the plaintext mnemonic unchanged');
  assert(stderr === '', 'control: no stderr output for a valid plaintext mnemonic');
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
