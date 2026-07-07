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
 * disconnects, so the background WS-await runs to completion or the relay
 * 5-minute TTL, whichever is first. There is a 60-second hard timeout
 * (matching the plugin) so a slow / abandoned browser flow doesn't hold the
 * process hostage. After completion the user must restart their MCP host
 * (Claude Desktop / Cursor / Claude Code) for the server to re-read
 * credentials.json and switch out of `unconfigured` mode.
 *
 * Reference implementation: `skill/plugin/index.ts` (totalreclaw_pair tool
 * handler) and `skill/plugin/pair-cli-relay.ts` (CLI runner). The crypto +
 * WS-client code in `pair-crypto.ts` and `pair-remote-client.ts` is a
 * verbatim port of the plugin equivalents.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';

import type { ToolContext } from './types.js';
import { PAIR_TOOL_DESCRIPTION } from '../prompts.js';
import {
  openRemotePairSession,
  awaitPhraseUpload,
  type RemotePairSession,
} from '../pair-remote-client.js';
import { deriveAuthKey, computeAuthKeyHash } from '../cli/setup.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CREDENTIALS_DIR = path.join(os.homedir(), '.totalreclaw');
const CREDENTIALS_PATH = path.join(CREDENTIALS_DIR, 'credentials.json');

/**
 * Hard timeout for the background WS-await. Matches the plugin
 * (`PAIR_TOOL_HARD_TIMEOUT_MS = 60_000`) — long enough for a slow
 * scan-and-paste, short enough that a stuck browser doesn't hold the
 * MCP process indefinitely.
 */
const PAIR_TOOL_HARD_TIMEOUT_MS = 60_000;

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

/**
 * Write credentials.json with 0600 permissions. Mirrors `cli/setup.ts`
 * `saveCredentials` but accepts a richer payload (`mnemonic`, `salt` hex,
 * `userId`, `serverUrl`).
 */
function writePairedCredentials(creds: {
  userId: string;
  salt: string;
  serverUrl: string;
  mnemonic: string;
}): void {
  if (!fs.existsSync(CREDENTIALS_DIR)) {
    fs.mkdirSync(CREDENTIALS_DIR, { recursive: true, mode: 0o700 });
  }
  fs.writeFileSync(CREDENTIALS_PATH, JSON.stringify(creds, null, 2) + '\n', {
    encoding: 'utf-8',
    mode: 0o600,
  });
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
  const hardTimeoutMs = opts.hardTimeoutMs ?? PAIR_TOOL_HARD_TIMEOUT_MS;
  const httpsBase = relayBaseToHttps(opts.relayBaseUrl);
  const tokenTail = opts.session.token.slice(0, 8);

  const phraseUploadPromise = awaitPhraseUpload(opts.session, {
    phraseValidator: (p: string) => validateMnemonic(p, wordlist),
    completePairing: async ({ mnemonic }) => {
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
          writePairedCredentials({
            userId: registeredUserId,
            salt: saltHex,
            serverUrl: opts.serverUrl,
            mnemonic,
          });
        } catch (writeErr) {
          const m = writeErr instanceof Error ? writeErr.message : String(writeErr);
          log('error', `pair: credentials.json write failed: ${m}`);
          return { state: 'error', error: 'credentials_write_failed' };
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
      }
    },
    timeoutMs: hardTimeoutMs,
  });

  // Outer race against a hard timeout — covers the case where the WS stays
  // open but the browser never POSTs (rare; matches plugin's 3.3.4-rc.2 fix).
  const TIMEOUT_SENTINEL = {
    status: 'timed_out' as const,
    message: `Pair flow timed out (${hardTimeoutMs / 1000}s) — generate a new URL with totalreclaw_pair.`,
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
}

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
  const rawMode = args?.mode;
  const mode: 'generate' | 'import' = rawMode === 'import' ? 'import' : 'generate';

  const relayBaseUrl = opts.relayBaseUrl ?? resolvePairRelayUrl();
  const serverUrl = opts.serverUrl ?? process.env.TOTALRECLAW_SERVER_URL ?? 'https://api.totalreclaw.xyz';

  let session: RemotePairSession;
  try {
    session = await openRemotePairSession({
      relayBaseUrl,
      mode: mode === 'generate' ? 'generate' : 'import',
    });
  } catch (err) {
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
    : Date.now() + 5 * 60_000;

  // Schedule the background WS-await. In production we DO NOT await it —
  // the tool returns URL+PIN immediately so the agent can hand the URL to
  // the user. The MCP server process stays alive (stdio host child) so the
  // background task can run to completion.
  const bgPromise = runPairBackgroundTask({
    session,
    relayBaseUrl,
    serverUrl,
    log: opts.log,
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

  const instructions =
    mode === 'generate'
      ? `The browser will generate a NEW 12-word recovery phrase and ask the user to write it down + retype 3 words before finalizing.`
      : `The browser will accept an EXISTING phrase that the user pastes in the browser (never through chat).`;

  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify({
          url: session.url,
          pin: session.pin,
          expires_at_ms: expiresAtMs,
          mode,
          relay_base_url: relayBaseUrl,
          instructions: [
            `Open the URL above on the user's phone or another browser (copy-paste).`,
            instructions,
            `Enter the 6-digit PIN shown above into the browser.`,
            `The encrypted phrase uploads to this MCP server — it NEVER touches the LLM.`,
            `After the browser shows "Pairing complete", restart the MCP host (Claude Desktop / Cursor / Claude Code) so the server re-reads ~/.totalreclaw/credentials.json. The session expires in ~5 minutes.`,
          ],
        }),
      },
    ],
  };
}
