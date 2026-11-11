/**
 * TotalReclaw MCP — totalreclaw_pair tool
 *
 * Browser-mediated TotalReclaw account setup. The user opens the returned
 * URL on their phone or another browser, the browser generates (or imports)
 * a 12-word BIP-39 recovery phrase, encrypts it via x25519 ECDH + AES-256-GCM,
 * and uploads the ciphertext to the same relay this MCP server already talks
 * to. The MCP server decrypts the phrase locally and writes
 * `~/.totalreclaw/credentials.json` so a host restart picks the user up in
 * configured mode.
 *
 * Phrase-safety invariants (see project_phrase_safety_rule):
 *   - The phrase NEVER appears in the tool's return payload.
 *   - The phrase NEVER passes through stdout / stderr / logs.
 *   - The phrase is generated in the browser; the MCP server only ever sees
 *     the decrypted plaintext in-memory inside `completePairing` and writes
 *     it to the 0600-mode credentials.json on disk.
 *
 * Flow:
 *   1. Tool handler resolves the relay URL from
 *      `TOTALRECLAW_PAIR_RELAY_URL` (preferred) or `TOTALRECLAW_SERVER_URL`
 *      (rewrites http→ws / https→wss). Falls back to `wss://api.totalreclaw.xyz`.
 *   2. Opens a relay pair session (`openRemotePairSession`) and returns
 *      `{url, pin, expires_at_ms}` to the agent immediately.
 *   3. Schedules a background `awaitPhraseUpload` task so the WS stays open
 *      across the tool's return. When the browser POSTs the encrypted
 *      phrase, completePairing decrypts, derives keys, registers with the
 *      relay, and writes credentials.json.
 *
 * MCP-process lifecycle note: stdio MCP servers stay alive until the host
 * disconnects, so the background WS-await runs until the browser uploads or
 * the relay's pairing session expires (the `expires_at` the relay returns,
 * 10 minutes on the current relay, plus a short grace). It used to stop
 * after 60 seconds, which closed the session before a user in generate mode
 * could write down 12 words and retype 3 of them. Credentials are read once,
 * at startup (`index.ts` `main()`), so after completion the user must fully
 * quit and reopen their MCP host (Claude Desktop / Cursor / Claude Code) for
 * the server to re-read credentials.json and switch out of `unconfigured`
 * mode.
 *
 * Existing-credentials guard (mirrors Hermes, `python/src/totalreclaw/hermes/pair_tool.py`):
 *   `~/.totalreclaw/credentials.json` is shared with every TotalReclaw client
 *   on the machine (Hermes reads the same file). Pairing over it would move
 *   all of them to a different vault. So when the server is configured or the
 *   file exists, the tool refuses with `already_configured`. Only if the user
 *   asks to replace their account does the agent call again with
 *   `replace=true`. That opens a replace-mode relay session (relay PR #72):
 *   the pair page warns the user and makes THEM tick a confirmation box, and
 *   the upload is refused unless the relay forwards that confirmation. The
 *   agent never holds anything that authorises a replacement. Before a
 *   confirmed replacement overwrites the file, the old one is kept as
 *   `credentials.json.replaced-<UTC>-<rand>` (0600); the new file is written
 *   atomically (temp file + rename). A normal (non-replace) pairing
 *   creates the file with an exclusive open, so it can never overwrite one
 *   that appeared after the guard ran.
 *
 * Reference implementation: `skill/plugin/index.ts` (totalreclaw_pair tool
 * handler) and `skill/plugin/pair-cli-relay.ts` (CLI runner). The crypto +
 * WS-client code in `pair-crypto.ts` and `pair-remote-client.ts` is a
 * verbatim port of the plugin equivalents.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { randomBytes } from 'node:crypto';

import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';

import type { ToolContext } from './types.js';
import type { UnusableCredentials } from '../dispatch.js';
import { PAIR_TOOL_DESCRIPTION } from '../prompts.js';
import {
  openRemotePairSession,
  awaitPhraseUpload,
  ReplaceNotSupportedError,
  type RemotePairSession,
} from '../pair-remote-client.js';
import { deriveAuthKey, computeAuthKeyHash } from '../cli/setup.js';
import { redactedBundleSummary, type DerivedBundleV1 } from '../subgraph/bundle.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CREDENTIALS_DIR = path.join(os.homedir(), '.totalreclaw');
const CREDENTIALS_PATH = path.join(CREDENTIALS_DIR, 'credentials.json');

/**
 * Lifetime of a relay pairing session: `SESSION_TTL_MS` in totalreclaw-relay
 * `src/services/pair-sessions.ts` (10 minutes since internal#194; 5 minutes
 * was too tight for the browser flow). Only used when the relay's `opened`
 * frame has no usable `expires_at`; normally the wait follows the relay's own
 * expiry, so a future TTL change on the relay needs no change here.
 */
export const RELAY_PAIR_SESSION_TTL_MS = 10 * 60_000;

/** Slack after the relay's expiry, so an upload that lands at the deadline is still read. */
const PAIR_WAIT_GRACE_MS = 15_000;

/** Bounds on the wait, so a skewed clock or a bad `expires_at` can't make it absurd. */
const PAIR_WAIT_MIN_MS = 60_000;
const PAIR_WAIT_MAX_MS = 15 * 60_000;

/**
 * How long the background task keeps the relay WebSocket open waiting for
 * the browser's upload: until the relay's pairing session expires, plus
 * {@link PAIR_WAIT_GRACE_MS}. The user has to finish the whole browser step
 * (PIN, then writing down 12 words and retyping 3 in generate mode) inside
 * this window. Falls back to {@link RELAY_PAIR_SESSION_TTL_MS} when
 * `expiresAt` is unparsable or already in the past by the local clock, and
 * is clamped to [1 min, 15 min]. Exported for tests.
 */
export function pairWaitMs(expiresAt: string, now: number = Date.now()): number {
  const parsed = Date.parse(expiresAt);
  const untilExpiry =
    Number.isFinite(parsed) && parsed > now ? parsed - now : RELAY_PAIR_SESSION_TTL_MS;
  return Math.min(PAIR_WAIT_MAX_MS, Math.max(PAIR_WAIT_MIN_MS, untilExpiry + PAIR_WAIT_GRACE_MS));
}

const DEFAULT_RELAY_URL_WSS = 'wss://api.totalreclaw.xyz';

// ---------------------------------------------------------------------------
// Tool definition
// ---------------------------------------------------------------------------

export const pairToolDefinition = {
  name: 'totalreclaw_pair',
  description: PAIR_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      mode: {
        type: 'string',
        enum: ['generate', 'import'],
        description:
          '"generate" = the browser will create a NEW recovery phrase. "import" = the ' +
          'browser will accept an EXISTING phrase that the user pastes in their browser ' +
          '(never through chat).',
        default: 'generate',
      },
      replace: {
        type: 'boolean',
        description:
          'Only for an install that is already set up, and only when the user has asked to ' +
          'replace their current account with a different one. It does not authorise ' +
          'anything: it opens a pairing page that warns the user and asks THEM to confirm ' +
          'the replacement there. You cannot confirm it for them. Without it, the tool ' +
          'refuses to pair over existing credentials.',
      },
    },
    additionalProperties: false,
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: false,
  },
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Resolve the relay base URL for the pair WS.
 *
 * Order:
 *   1. `TOTALRECLAW_PAIR_RELAY_URL` if set (explicit override).
 *   2. `TOTALRECLAW_SERVER_URL` rewritten http→ws / https→wss (matches
 *      plugin's 3.3.12-rc.2 behavior — pair WS lives on the same host as
 *      the rest of the API, so RC users on api-staging stay on staging).
 *   3. Default `wss://api.totalreclaw.xyz`.
 */
export function resolvePairRelayUrl(env: NodeJS.ProcessEnv = process.env): string {
  const explicit = env.TOTALRECLAW_PAIR_RELAY_URL;
  if (explicit && explicit.trim().length > 0) {
    return explicit.trim().replace(/\/+$/, '');
  }
  const serverUrl = env.TOTALRECLAW_SERVER_URL;
  if (serverUrl && serverUrl.trim().length > 0) {
    return serverUrl
      .trim()
      .replace(/^https:\/\//, 'wss://')
      .replace(/^http:\/\//, 'ws://')
      .replace(/\/+$/, '');
  }
  return DEFAULT_RELAY_URL_WSS;
}

/**
 * Convert a wss:// or ws:// relay URL back to https:// or http:// for the
 * REST `/v1/register` call. Used by the background completePairing handler.
 */
function relayBaseToHttps(relayBase: string): string {
  return relayBase
    .replace(/^wss:\/\//, 'https://')
    .replace(/^ws:\/\//, 'http://')
    .replace(/\/+$/, '');
}

/** Thrown when pairing would overwrite a credentials.json that already exists. */
export class CredentialsExistError extends Error {
  constructor() {
    super('credentials.json already exists');
    this.name = 'CredentialsExistError';
  }
}

/**
 * Write credentials.json at mode 0600.
 *
 * `replace: false` creates the file exclusively (`wx`): if a credentials file
 * appeared after the tool's guard ran (another pairing, another client), the
 * write fails with `CredentialsExistError` instead of overwriting it.
 *
 * `replace: true` is only used after the user confirmed a replacement on the
 * pair page and the old file was preserved. It writes a temp file in the same
 * directory (`wx`, 0600), fsyncs it and renames it over credentials.json, so
 * the file is never half-written, a symlinked credentials.json is replaced
 * rather than written through, and the result is always mode 0600.
 */
function writeCredentialsFile(credentialsPath: string, body: string, replace: boolean): void {
  const dir = path.dirname(credentialsPath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  if (replace) {
    const tmp = path.join(
      dir,
      `.${path.basename(credentialsPath)}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`,
    );
    try {
      const fd = fs.openSync(tmp, 'wx', 0o600);
      try {
        fs.fchmodSync(fd, 0o600);
        fs.writeSync(fd, body, null, 'utf-8');
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmp, credentialsPath);
    } catch (err) {
      try {
        fs.unlinkSync(tmp);
      } catch {
        // Not created, or already renamed.
      }
      throw err;
    }
    return;
  }
  try {
    fs.writeFileSync(credentialsPath, body, {
      encoding: 'utf-8',
      mode: 0o600,
      flag: 'wx',
    });
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === 'EEXIST') {
      throw new CredentialsExistError();
    }
    throw err;
  }
}

/** `20261103T010203.456Z-1a2b3c`: UTC to the millisecond plus a random suffix. */
function replacedStamp(now: Date): string {
  return `${now.toISOString().replace(/[-:]/g, '')}-${randomBytes(3).toString('hex')}`;
}

/**
 * Step 1 of a user-confirmed replacement, before anything is overwritten.
 * Mirrors Python's `hermes/auto_migrate.py::preserve_credentials_before_replace`.
 *
 * Copies `credentials.json` to `credentials.json.replaced-<stamp>` (mode 0600
 * from the first byte). The original stays in place until the new credentials
 * are written over it. Bytes-for-bytes copy: no parsing, nothing logged but
 * paths. Throws if the copy cannot be made, so the caller fails the pairing
 * rather than overwrite an unpreserved file. Returns the copy's path, or
 * undefined when there is no credentials.json. Exported for tests.
 */
export function preserveCredentialsBeforeReplace(
  credentialsPath: string,
  now: Date = new Date(),
): string | undefined {
  if (!fs.existsSync(credentialsPath)) return undefined;
  const dest = `${credentialsPath}.replaced-${replacedStamp(now)}`;
  const data = fs.readFileSync(credentialsPath);
  let created = false;
  try {
    const fd = fs.openSync(dest, 'wx', 0o600);
    created = true;
    try {
      fs.writeSync(fd, data);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  } catch (err) {
    if (created) {
      try {
        fs.unlinkSync(dest);
      } catch {
        // Leave it; the caller fails the pairing either way.
      }
    }
    throw err;
  } finally {
    data.fill(0);
  }
  return dest;
}

/**
 * Step 2, only after the new credentials were written: rename Hermes'
 * `credentials.json.bak` (from an earlier phrase-to-bundle migration), if
 * present, to `credentials.json.bak.replaced-<stamp>` so the new account
 * cannot overwrite it. Returns the new path, or undefined. Exported for tests.
 */
export function moveBackupAsideAfterReplace(
  credentialsPath: string,
  now: Date = new Date(),
): string | undefined {
  const bakPath = `${credentialsPath}.bak`;
  if (!fs.existsSync(bakPath)) return undefined;
  const dest = `${bakPath}.replaced-${replacedStamp(now)}`;
  fs.renameSync(bakPath, dest);
  return dest;
}

/**
 * The replacement failed before the new credentials were written. Remove the
 * step-1 copy, but only if credentials.json is still a regular file
 * byte-identical to it; otherwise keep the copy. Returns true if removed.
 * Exported for tests.
 */
export function discardPreservedCopyIfUnchanged(credentialsPath: string, copyPath: string): boolean {
  let original: Buffer | undefined;
  let copy: Buffer | undefined;
  try {
    if (!fs.lstatSync(credentialsPath).isFile()) return false;
    original = fs.readFileSync(credentialsPath);
    copy = fs.readFileSync(copyPath);
    if (!original.equals(copy)) return false;
    fs.unlinkSync(copyPath);
    return true;
  } catch {
    return false;
  } finally {
    original?.fill(0);
    copy?.fill(0);
  }
}

/**
 * Write credentials.json with 0600 permissions. Mirrors `cli/setup.ts`
 * `saveCredentials` but accepts a richer payload (`mnemonic`, `salt` hex,
 * `userId`, `serverUrl`). Never overwrites an existing file.
 */
function writePairedCredentials(
  creds: {
    userId: string;
    salt: string;
    serverUrl: string;
    mnemonic: string;
  },
  credentialsPath: string,
  replace: boolean,
): void {
  writeCredentialsFile(credentialsPath, JSON.stringify(creds, null, 2) + '\n', replace);
}

/**
 * Write a `derived-bundle-v1` bundle to `credentials.json`, `version: 2`
 * (Option E Phase 2 / #581, P2-13).
 *
 * MCP has no OS-keychain integration (that is Python/Hermes-only — see
 * `subgraph/credentials.ts`'s module doc), so this always writes the
 * "headless / no keychain" storage form: the complete object, including
 * `vault` and `signing.private_key`, as plaintext at mode `0600`
 * (derived-bundle-v1.md §4.3). This is the SAME at-rest exposure class as
 * today's plaintext-mnemonic `credentials.json` — not a regression, and not
 * yet a security improvement for headless MCP hosts (the improvement is
 * "the root is gone", not "secrets at rest are now protected"; see
 * derived-bundle-v1.md §0's threat-model ceiling).
 *
 * Field order matches the canonical wire shape (client-consistency.md
 * "Credential Material") verbatim — `bundle` already carries every field
 * this object needs, since `DerivedBundleV1`'s shape IS the wire shape
 * (see `subgraph/bundle.ts`).
 */
function writePairedBundleCredentials(
  bundle: DerivedBundleV1,
  credentialsPath: string,
  replace: boolean,
): void {
  const wire = {
    version: 2 as const,
    schema: 'derived-bundle-v1' as const,
    vault: bundle.vault,
    signing: bundle.signing,
    account: bundle.account,
    provisioned_at: bundle.provisioned_at,
    provisioned_by: bundle.provisioned_by,
  };
  writeCredentialsFile(credentialsPath, JSON.stringify(wire, null, 2) + '\n', replace);
}

/**
 * POST /v1/register — best-effort. Returns user_id on success, or undefined
 * on USER_EXISTS (in which case the caller derives userId from the
 * auth-key hash). Other errors throw — the caller logs and continues with
 * mnemonic-only credentials so the next MCP-host restart can retry.
 */
async function registerUser(
  httpsBase: string,
  authKeyHash: string,
  saltHex: string,
): Promise<string | undefined> {
  const url = `${httpsBase}/v1/register`;
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      auth_key_hash: authKeyHash,
      salt: saltHex,
    }),
  });
  if (!response.ok) {
    const body = await response.text().catch(() => '');
    if (body.includes('USER_EXISTS') || response.status === 409) {
      return undefined;
    }
    throw new Error(`register failed (HTTP ${response.status}): ${body || response.statusText}`);
  }
  const pairJson = (await response.json()) as { user_id?: string };
  if (!pairJson.user_id) {
    throw new Error('register: response missing user_id');
  }
  return pairJson.user_id;
}

// ---------------------------------------------------------------------------
// Background completion task
// ---------------------------------------------------------------------------

/**
 * Run the full WS-await + decrypt + register + write-credentials pipeline.
 * Exported for testing — the tool handler schedules this as a background
 * task so the agent can return URL/PIN to the user immediately.
 *
 * Phrase NEVER leaves this function. Logs only the relay token prefix +
 * userId prefix.
 */
export async function runPairBackgroundTask(opts: {
  session: RemotePairSession;
  relayBaseUrl: string;
  serverUrl: string;
  hardTimeoutMs?: number;
  log?: (level: 'info' | 'warn' | 'error', msg: string) => void;
  /** Override the credentials.json path — for tests. */
  credentialsPath?: string;
}): Promise<void> {
  const log =
    opts.log ??
    ((level, msg) => {
      // Default logger goes to stderr so MCP stdout (JSON-RPC framing) stays clean.
      const prefix = `totalreclaw_pair[${level}]:`;
      if (level === 'error') {
        console.error(prefix, msg);
      } else {
        console.error(prefix, msg);
      }
    });
  const hardTimeoutMs = opts.hardTimeoutMs ?? pairWaitMs(opts.session.expiresAt);
  const httpsBase = relayBaseToHttps(opts.relayBaseUrl);
  const tokenTail = opts.session.token.slice(0, 8);
  const credentialsPath = opts.credentialsPath ?? CREDENTIALS_PATH;
  const replace = opts.session.replace === true;

  /**
   * Replace mode, step 1: copy the old credentials.json aside before anything
   * is overwritten. Reached only after `awaitPhraseUpload` saw the user's
   * confirmation from the pair page. On failure the relay is nacked and
   * nothing is overwritten.
   */
  const preserveIfReplacing = (): {
    copyPath?: string;
    failure?: { state: 'error'; error: string };
  } => {
    if (!replace) return {};
    try {
      const copyPath = preserveCredentialsBeforeReplace(credentialsPath);
      if (copyPath) log('info', `pair replace: kept the previous credentials at ${copyPath}`);
      return { copyPath };
    } catch (err) {
      const m = err instanceof Error ? err.message : String(err);
      log('error', `pair replace: could not keep the previous credentials, not replacing: ${m}`);
      return { failure: { state: 'error', error: 'credentials_preserve_failed' } };
    }
  };

  /**
   * Replace mode, last step. After a successful write, move Hermes' `.bak`
   * aside. If the write did not happen, drop the step-1 copy, but only when
   * credentials.json is still byte-identical to it.
   */
  const finishReplace = (written: boolean, copyPath: string | undefined): void => {
    if (!replace) return;
    if (written) {
      try {
        const moved = moveBackupAsideAfterReplace(credentialsPath);
        if (moved) log('info', `pair replace: moved the previous backup to ${moved}`);
      } catch (err) {
        const m = err instanceof Error ? err.message : String(err);
        log('error', `pair replace: new credentials written, but could not move credentials.json.bak aside: ${m}`);
      }
    } else if (copyPath && !discardPreservedCopyIfUnchanged(credentialsPath, copyPath)) {
      log('warn', `pair replace: not replaced; keeping the copy at ${copyPath}`);
    }
  };

  /** Map a credentials write failure to the nack the relay gets. */
  const writeFailure = (writeErr: unknown): { state: 'error'; error: string } => {
    if (writeErr instanceof CredentialsExistError) {
      log('warn', 'pair: credentials.json appeared during pairing; not overwriting it');
      return { state: 'error', error: 'already_configured' };
    }
    const m = writeErr instanceof Error ? writeErr.message : String(writeErr);
    log('error', `pair: credentials.json write failed: ${m}`);
    return { state: 'error', error: 'credentials_write_failed' };
  };

  const phraseUploadPromise = awaitPhraseUpload(opts.session, {
    phraseValidator: (p: string) => validateMnemonic(p, wordlist),
    completePairing: async ({ mnemonic }) => {
      const preserved = preserveIfReplacing();
      if (preserved.failure) return preserved.failure;
      let written = false;
      try {
        // Derive auth key + salt — matches setup.ts deriveAuthKey shape.
        const { authKeyHex, saltHex } = deriveAuthKey(mnemonic);
        const authKeyHash = computeAuthKeyHash(authKeyHex);

        // Register with relay — best-effort. USER_EXISTS → derive userId
        // deterministically from the auth-key hash so credentials are
        // still complete. Other errors → continue with mnemonic-only
        // creds and let the next host restart retry register.
        let registeredUserId: string | undefined;
        try {
          registeredUserId = await registerUser(httpsBase, authKeyHash, saltHex);
          if (registeredUserId) {
            log('info', `pair: registered user_id=${registeredUserId.slice(0, 8)}…`);
          } else {
            registeredUserId = authKeyHash.slice(0, 32);
            log('info', `pair: USER_EXISTS — using derived userId=${registeredUserId.slice(0, 8)}…`);
          }
        } catch (regErr) {
          const m = regErr instanceof Error ? regErr.message : String(regErr);
          log('warn', `pair: /v1/register failed (best-effort, will retry on host restart): ${m}`);
          registeredUserId = authKeyHash.slice(0, 32);
        }

        try {
          writePairedCredentials(
            {
              userId: registeredUserId,
              salt: saltHex,
              serverUrl: opts.serverUrl,
              mnemonic,
            },
            credentialsPath,
            replace,
          );
          written = true;
        } catch (writeErr) {
          return writeFailure(writeErr);
        }

        log(
          'info',
          `pair: session ${tokenTail}… completed; credentials written (userId=${registeredUserId.slice(0, 8)}…)`,
        );
        return { state: 'active' };
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        log('error', `pair: completePairing failed: ${msg}`);
        return { state: 'error', error: msg };
      } finally {
        finishReplace(written, preserved.copyPath);
      }
    },
    // Option E Phase 2 / #581 (P2-13) — payload_type: "derived-bundle-v1"
    // sibling. Inert in production today: as documented in
    // pair-remote-client.ts's module doc, the relay does not yet forward a
    // `payload_type` field on the pair envelope (P2-11), so this branch
    // only ever fires in `mcp/tests/pair-bundle.test.ts`'s fixture-driven
    // tests. Wired here anyway so the contract is complete and P2-11
    // landing on the relay side needs zero MCP-side follow-up.
    completePairingBundle: async ({ bundle }) => {
      const preserved = preserveIfReplacing();
      if (preserved.failure) return preserved.failure;
      let written = false;
      try {
        // NO registerWithServer()/registerUser() call — derived-bundle-v1.md
        // §4.5's normative rule: a bundle-configured client MUST NOT call
        // POST /v1/register. The vault is registered by the provisioning
        // origin (the SPA, which holds the root), which is the only party
        // able to register correctly.
        try {
          writePairedBundleCredentials(bundle, credentialsPath, replace);
          written = true;
        } catch (writeErr) {
          return writeFailure(writeErr);
        }
        log(
          'info',
          `pair: session ${tokenTail}… completed via derived-bundle-v1; ` +
            `credentials written — ${JSON.stringify(redactedBundleSummary(bundle))}`,
        );
        return { state: 'active' };
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        log('error', `pair: completePairingBundle failed: ${msg}`);
        return { state: 'error', error: msg };
      } finally {
        finishReplace(written, preserved.copyPath);
      }
    },
    timeoutMs: hardTimeoutMs,
  });

  // Outer race against a hard timeout — covers the case where the WS stays
  // open but the browser never POSTs (rare; matches plugin's 3.3.4-rc.2 fix).
  const TIMEOUT_SENTINEL = {
    status: 'timed_out' as const,
    message: `Pair flow timed out (${Math.round(hardTimeoutMs / 1000)}s) — generate a new URL with totalreclaw_pair.`,
  };
  let hardTimer: ReturnType<typeof setTimeout> | undefined;
  const hardTimeoutPromise = new Promise<typeof TIMEOUT_SENTINEL>((resolve) => {
    hardTimer = setTimeout(() => resolve(TIMEOUT_SENTINEL), hardTimeoutMs);
  });

  try {
    const raced = await Promise.race([phraseUploadPromise, hardTimeoutPromise]);
    if (
      raced &&
      typeof raced === 'object' &&
      (raced as { status?: unknown }).status === 'timed_out'
    ) {
      log('warn', `pair: hard timeout (token=${tokenTail}…)`);
    }
  } catch (bgErr: unknown) {
    const bgMsg = bgErr instanceof Error ? bgErr.message : String(bgErr);
    log('warn', `pair: background task ended for token=${tokenTail}…: ${bgMsg}`);
  } finally {
    if (hardTimer) clearTimeout(hardTimer);
  }
}

// ---------------------------------------------------------------------------
// Tool handler
// ---------------------------------------------------------------------------

export interface HandlePairOptions {
  /** Override relay URL — for tests. */
  relayBaseUrl?: string;
  /** Override server URL written into credentials.json — defaults to env. */
  serverUrl?: string;
  /**
   * If true, await the background task before returning. Used by tests so
   * they can assert on the post-completion state. In production this is
   * false — the tool handler returns immediately after URL/PIN emit so the
   * agent can hand the URL to the user.
   */
  awaitBackground?: boolean;
  /** Logger override (tests). Default writes to stderr. */
  log?: (level: 'info' | 'warn' | 'error', msg: string) => void;
  /**
   * True when the running server already has a credential (managed or
   * self-hosted mode). Wired by `index.ts`. The guard also checks
   * credentials.json on disk, which covers a pairing completed earlier in
   * this process (the server stays unconfigured until restart) and a file
   * this server can't read.
   */
  isConfigured?: () => boolean;
  /** Set when startup found a credentials file it can't use. Wired by `index.ts`. */
  getUnusableCredentials?: () => UnusableCredentials | undefined;
  /** Override the credentials.json path — for tests. */
  credentialsPath?: string;
  /** Override the session opener — for tests. */
  openSession?: typeof openRemotePairSession;
}

/**
 * Refusal returned when pairing would overwrite existing credentials.
 * Wording mirrors Hermes (`python/src/totalreclaw/hermes/pair_tool.py`), plus
 * one sentence on the shared credentials file.
 */
export const ALREADY_CONFIGURED_MESSAGE =
  'This install is already paired to a TotalReclaw account. ' +
  'Starting a new pair flow would switch this agent to a DIFFERENT account, and the current ' +
  "account's memories would no longer be available here. Other TotalReclaw clients on this " +
  'machine (such as Hermes) share the same credentials file, so they would switch too. ' +
  'Tell the user their account is already set up. Only if they ask to REPLACE it with a ' +
  'different account, call again with replace=true: the pairing page then shows them a ' +
  'warning and they confirm the replacement there. You cannot confirm it for them, and do ' +
  'not move, rename or delete the credentials file to get around this. (Restoring this same ' +
  'account on another device happens on that device, not here.) If the user just finished ' +
  'pairing, they need to fully quit and reopen this app so the server loads the new credentials.';

/** Refusal when a credentials file exists that this server doesn't (yet) use: same text. */
export const CREDENTIALS_FILE_EXISTS_MESSAGE = ALREADY_CONFIGURED_MESSAGE;

const REPLACE_USER_INSTRUCTION =
  'Tell the user: open this link yourself; the page warns that this replaces the account currently on this agent, so tick the box only if you want to replace it. Do not open or act on the page yourself.';

export const REPLACE_NOT_SUPPORTED_MESSAGE =
  'This TotalReclaw relay needs updating before an account can be replaced: it cannot ask ' +
  'the user to confirm the replacement on the pairing page. No pairing was started and the ' +
  'current account is unchanged.';

/**
 * Handle a totalreclaw_pair tool call. Returns the URL + PIN + expires_at_ms
 * structured payload immediately and schedules the encrypted-phrase WS-await
 * as a background task.
 */
export async function handlePair(
  _ctx: ToolContext,
  args: Record<string, unknown> | undefined,
  opts: HandlePairOptions = {},
): Promise<{ content: Array<{ type: string; text: string }>; isError?: boolean }> {
  const credentialsPath = opts.credentialsPath ?? CREDENTIALS_PATH;

  // Existing-credentials guard. Completing a fresh pair on a configured
  // install switches it (and every other client sharing credentials.json) to
  // a different vault, so only the user may confirm it, on the pair page.
  // `replace=true` only opens a replace-mode session; it authorises nothing.
  const configured = opts.isConfigured?.() ?? false;
  let replace = false;
  if (configured || fs.existsSync(credentialsPath)) {
    if (args?.replace !== true) {
      const unusable = configured ? undefined : opts.getUnusableCredentials?.();
      const message = configured
        ? ALREADY_CONFIGURED_MESSAGE
        : unusable
          ? `${unusable.message} Do not move, rename or delete the credentials file to get around this. ` +
            'Only if the user asks to REPLACE that account, call again with replace=true (the pairing page makes the user confirm it).'
          : CREDENTIALS_FILE_EXISTS_MESSAGE;
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              already_configured: true,
              ...(unusable ? { reason: unusable.reason } : {}),
              message,
            }),
          },
        ],
      };
    }
    replace = true;
  }

  const rawMode = args?.mode;
  const mode: 'generate' | 'import' = rawMode === 'import' ? 'import' : 'generate';

  const relayBaseUrl = opts.relayBaseUrl ?? resolvePairRelayUrl();
  const serverUrl = opts.serverUrl ?? process.env.TOTALRECLAW_SERVER_URL ?? 'https://api.totalreclaw.xyz';
  const openSession = opts.openSession ?? openRemotePairSession;

  let session: RemotePairSession;
  try {
    session = await openSession({
      relayBaseUrl,
      mode: mode === 'generate' ? 'generate' : 'import',
      ...(replace ? { replace: true } : {}),
    });
  } catch (err) {
    if (err instanceof ReplaceNotSupportedError) {
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              error: 'replace_not_supported',
              message: REPLACE_NOT_SUPPORTED_MESSAGE,
              already_configured: true,
            }),
          },
        ],
        isError: true,
      };
    }
    const msg = err instanceof Error ? err.message : String(err);
    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            error: 'pair_session_open_failed',
            message: `Failed to open relay pair session: ${msg}`,
            relay_base_url: relayBaseUrl,
          }),
        },
      ],
      isError: true,
    };
  }

  // ISO-8601 → ms for tool-payload parity with the plugin.
  const parsedExpiresMs = Date.parse(session.expiresAt);
  const expiresAtMs = Number.isFinite(parsedExpiresMs)
    ? parsedExpiresMs
    : Date.now() + RELAY_PAIR_SESSION_TTL_MS;
  const minutesLeft = Math.min(15, Math.max(1, Math.round((expiresAtMs - Date.now()) / 60_000)));

  // Schedule the background WS-await. In production we DO NOT await it —
  // the tool returns URL+PIN immediately so the agent can hand the URL to
  // the user. The MCP server process stays alive (stdio host child) so the
  // background task can run to completion.
  const bgPromise = runPairBackgroundTask({
    session,
    relayBaseUrl,
    serverUrl,
    log: opts.log,
    credentialsPath,
  });

  if (opts.awaitBackground) {
    await bgPromise;
  } else {
    // Detach; surface unhandled rejections via the same logger.
    bgPromise.catch((err) => {
      const msg = err instanceof Error ? err.message : String(err);
      const log =
        opts.log ??
        ((level: string, m: string) => console.error(`totalreclaw_pair[${level}]:`, m));
      log('warn', `pair: background task rejected: ${msg}`);
    });
  }

  const instructions = pairInstructions(mode, minutesLeft);

  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify({
          url: session.url,
          pin: session.pin,
          expires_at_ms: expiresAtMs,
          mode,
          replace,
          relay_base_url: relayBaseUrl,
          expires_in_minutes: minutesLeft,
          instructions: replace
            ? [
                instructions[0],
                REPLACE_USER_INSTRUCTION,
                ...instructions.slice(1),
              ]
            : instructions,
        }),
      },
    ],
  };
}

/**
 * The steps the agent relays to the user. Kept in step with what actually
 * happens: the relay page ends on "You're all set", the link lives as long as
 * the relay session, and the memory tools only work after the host restarts
 * (credentials are read once, at startup). Exported for tests.
 */
export function pairInstructions(mode: 'generate' | 'import', minutesLeft: number): string[] {
  const phraseStep =
    mode === 'generate'
      ? `The page creates a NEW 12-word recovery phrase. The user writes it down somewhere safe (paper or a password manager) and retypes 3 of the words to confirm. That phrase is the only way to recover their memories.`
      : `The user enters their EXISTING 12-word recovery phrase on the page, never in this chat.`;
  return [
    `Give the user the link and the 6-digit PIN above. They open the link in a browser (on this computer or their phone) and enter the PIN there.`,
    phraseStep,
    `The link works for about ${minutesLeft} minutes, so there is time to write the phrase down carefully. If it expires, call totalreclaw_pair again for a new link.`,
    `The phrase is encrypted in the browser and sent to this MCP server. It never passes through this chat. Never ask the user to paste it here.`,
    `When the page says "You're all set", ask the user to fully quit and reopen this app (Claude Desktop, Cursor, Claude Code or whichever app runs this server; closing the window is not enough), or to start a new session. The server reads the credentials only at startup, so the memory tools return not_configured until then.`,
  ];
}
