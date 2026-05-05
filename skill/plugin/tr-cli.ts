#!/usr/bin/env node
/**
 * tr — TotalReclaw hybrid CLI (3.3.9-rc.1 primary architecture)
 *
 * OpenClaw 2026.5.2 has a tool-policy-pipeline bug (issue #223) that strips non-bundled plugin
 * tools before they reach the agent toolset. In 3.3.9-rc.1, this CLI is the PRIMARY path for
 * all agent memory operations (not a fallback). The agent runs `tr <cmd> --json` from shell;
 * hooks (before_agent_start, agent_end, message_received, before_reset) continue via the
 * unbroken hook code path.
 *
 * Phrase-safety: this CLI reads credentials.json (mnemonic at rest) but NEVER
 * prints the mnemonic to stdout, stderr, or any log. Phrase only enters via QR-pair
 * browser tier (pair-cli.ts / pair-cli-relay.ts — unchanged).
 *
 * Commands:
 *   tr status [--json]          — print onboarding + credentials state
 *   tr pair [--json]            — start a relay pairing session, print URL+PIN+QR
 *   tr remember [--json] <text> — store a memory in the encrypted vault
 *   tr recall [--json] [--limit N] <query> — search the encrypted vault
 *
 * --json flag: all agent-facing CLI calls MUST use --json for clean machine-parseable output.
 *              Plain text mode is for direct user CLI use only.
 *
 * Install: wired via package.json `bin.tr` → dist/tr-cli.js
 * Usage from container: `docker exec tr-openclaw node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js status --json`
 */

import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

import { CONFIG } from './config.js';
import { loadCredentialsJson } from './fs-helpers.js';
import { printStatus } from './onboarding-cli.js';
import {
  deriveKeys,
  computeAuthKeyHash,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateContentFingerprint,
} from './crypto.js';
import { createApiClient } from './api-client.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const CREDENTIALS_PATH = CONFIG.credentialsPath;
const SERVER_URL = CONFIG.serverUrl;
const STATE_PATH = CONFIG.onboardingStatePath;
const PLUGIN_VERSION = '3.3.10-rc.1';

// Sentinel for the detached-child re-spawn used by `cmdPair`. The parent
// invocation forks a child with this flag, the child holds the WS until
// the browser uploads the encrypted phrase. Survives the parent
// (agent shell tool) exit AND any subsequent gateway SIGUSR1 restart
// — the child runs in its own process group via `detached: true` and
// is `unref()`ed so the parent's event loop does not wait on it.
const PAIR_DETACH_FLAG = '--detached-child';
const PAIR_HANDOFF_ENV = 'TR_PAIR_HANDOFF';

function die(msg: string, code = 1): never {
  process.stderr.write(`tr: ${msg}\n`);
  process.exit(code);
}

function log(msg: string): void {
  process.stdout.write(msg + '\n');
}

/** Parse --flag from args array, returning the cleaned args without the flag. */
function popFlag(args: string[], flag: string): [boolean, string[]] {
  const idx = args.indexOf(flag);
  if (idx === -1) return [false, args];
  return [true, [...args.slice(0, idx), ...args.slice(idx + 1)]];
}

/** Parse --limit N from args, returning [limit, cleanedArgs]. Default: defaultLimit. */
function popLimitFlag(args: string[], defaultLimit: number): [number, string[]] {
  const idx = args.indexOf('--limit');
  if (idx === -1 || idx + 1 >= args.length) return [defaultLimit, args];
  const n = parseInt(args[idx + 1], 10);
  const limit = isNaN(n) || n < 1 ? defaultLimit : n;
  return [limit, [...args.slice(0, idx), ...args.slice(idx + 2)]];
}

// ---------------------------------------------------------------------------
// Core init — minimal version of index.ts initialize()
// ---------------------------------------------------------------------------

interface CliContext {
  authKeyHex: string;
  encryptionKey: Buffer;
  dedupKey: Buffer;
  apiClient: ReturnType<typeof createApiClient>;
  userId: string;
}

async function buildContext(): Promise<CliContext> {
  const creds = loadCredentialsJson(CREDENTIALS_PATH);
  if (!creds) {
    die('TotalReclaw is not set up. Run: node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js pair --json');
  }

  const mnemonic =
    (typeof creds.mnemonic === 'string' && creds.mnemonic.trim()) ||
    (typeof creds.recovery_phrase === 'string' && creds.recovery_phrase.trim()) ||
    '';

  if (!mnemonic) {
    die('No recovery phrase in credentials.json. Run: tr pair --json');
  }

  // Parse existing salt/userId from credentials.json
  let existingSalt: Buffer | undefined;
  let existingUserId: string | undefined;

  const saltStr = typeof creds.salt === 'string' ? creds.salt : undefined;
  if (saltStr) {
    if (/^[0-9a-f]{64}$/i.test(saltStr)) {
      existingSalt = Buffer.from(saltStr, 'hex');
    } else {
      existingSalt = Buffer.from(saltStr, 'base64');
    }
  }
  existingUserId = typeof creds.userId === 'string' ? creds.userId : undefined;

  const keys = deriveKeys(mnemonic, existingSalt);
  const authKeyHex = keys.authKey.toString('hex');

  const apiClient = createApiClient(SERVER_URL);

  let userId: string;
  if (existingUserId) {
    userId = existingUserId;
  } else {
    // Register to get userId (idempotent on relay)
    const authHash = computeAuthKeyHash(keys.authKey);
    const saltHex = keys.salt.toString('hex');
    try {
      const result = await apiClient.register(authHash, saltHex);
      userId = result.user_id;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes('USER_EXISTS')) {
        userId = authHash.slice(0, 32);
      } else {
        die(`Relay registration failed: ${msg}`);
      }
    }
  }

  return {
    authKeyHex,
    encryptionKey: keys.encryptionKey,
    dedupKey: keys.dedupKey,
    apiClient,
    userId,
  };
}

// ---------------------------------------------------------------------------
// Command: status
// ---------------------------------------------------------------------------

async function cmdStatus(jsonMode: boolean): Promise<void> {
  // Probe plugin manifest for version/hybridMode/toolCount.
  let pluginVersion: string | undefined;
  let bootCount: number | undefined;
  let hybridMode = true; // default true in 3.3.9-rc.1 (hybrid-primary)
  let toolCount: number | undefined;
  let loadedAgeSec: number | undefined;

  try {
    const fs = await import('node:fs');
    const candidatePaths = [
      path.join(os.homedir(), '.openclaw', 'extensions', 'totalreclaw', '.loaded.json'),
      path.join(os.homedir(), '.openclaw', 'npm', 'node_modules', '@totalreclaw', 'totalreclaw', 'dist', '.loaded.json'),
    ];
    const resolvedPath = candidatePaths.find((p) => fs.existsSync(p));
    if (resolvedPath) {
      const raw = fs.readFileSync(resolvedPath, 'utf-8');
      const manifest = JSON.parse(raw) as {
        version?: string;
        bootCount?: number;
        loadedAt?: number;
        hybridMode?: boolean;
        tools?: string[];
      };
      pluginVersion = manifest.version ?? PLUGIN_VERSION;
      bootCount = manifest.bootCount;
      hybridMode = manifest.hybridMode !== false; // default true
      toolCount = manifest.tools?.length;
      const ageMs = Date.now() - (manifest.loadedAt ?? 0);
      loadedAgeSec = Math.round(ageMs / 1000);
    }
  } catch {
    // Best-effort
  }

  // Check onboarding state
  const creds = loadCredentialsJson(CREDENTIALS_PATH);
  const onboarded = !!creds;

  if (jsonMode) {
    // JSON-first output for agent parsing
    const out: Record<string, unknown> = {
      version: pluginVersion ?? PLUGIN_VERSION,
      onboarded,
      next_step: onboarded ? 'none' : 'pair',
      tool_count: toolCount ?? 17,
      hybrid_mode: hybridMode,
    };
    if (bootCount !== undefined) out.boot_count = bootCount;
    if (loadedAgeSec !== undefined) out.loaded_age_sec = loadedAgeSec;
    log(JSON.stringify(out));
  } else {
    // Human-readable plain text for direct user CLI use
    printStatus(CREDENTIALS_PATH, STATE_PATH, process.stdout);
    process.stdout.write(
      `\n  plugin:      ${pluginVersion ? `loaded (version=${pluginVersion}` + (bootCount !== undefined ? ` bootCount=${bootCount}` : '') + (loadedAgeSec !== undefined ? ` loaded=${loadedAgeSec}s ago` : '') + ')' : 'not found in .loaded.json'}\n` +
      `  hybrid-mode: ${hybridMode ? 'yes (primary — use tr <cmd> --json)' : 'no'}\n` +
      `  hooks:       before_agent_start, agent_end, message_received, before_reset\n`,
    );
  }
}

// ---------------------------------------------------------------------------
// Command: pair
// ---------------------------------------------------------------------------

async function cmdPair(args: string[]): Promise<void> {
  // Outcome flags. `--detached-child` is a sentinel set by the
  // parent re-spawn — the child runs the actual pair flow and holds
  // the WS until the browser uploads the encrypted phrase. The parent
  // (which the agent shell-tool waits on) prints URL+PIN and exits 0
  // immediately so the agent can respond to the user without holding
  // a 5-minute open shell-tool.
  const isDetachedChild = args.includes(PAIR_DETACH_FLAG);
  const outputMode = args.includes('--json') ? 'json' : args.includes('--url-pin') ? 'url-pin' : 'human';

  if (!isDetachedChild) {
    // PARENT MODE: fork self detached, wait for handoff line on a
    // tmp file, print it to stdout, exit. This shape — agent-facing
    // tool returns immediately with URL+PIN — was forced by Pedro's
    // 2026-05-05 502 QA: each `openclaw plugins install` writes a
    // config-change that the gateway eventually applies via SIGUSR1
    // restart, killing any in-flight subprocess. With the WS in a
    // detached child (own process group), it survives the gateway
    // restart that kills the agent's shell-tool subprocess.
    await runPairParent(outputMode);
    return;
  }

  // CHILD MODE: hold the WS until the browser uploads the encrypted
  // phrase, write credentials.json, exit. Output goes to a handoff
  // file the parent reads, then to /dev/null.
  await runPairDetachedChild(outputMode, args);
}

async function runPairParent(outputMode: string): Promise<void> {
  // Disk-handoff path — child writes URL+PIN here, parent polls.
  // Crypto-random suffix keeps concurrent invocations non-colliding.
  const handoffPath = path.join(os.tmpdir(), `tr-pair-handoff-${randomUUID()}.json`);
  // Pre-touch the file so the child can `appendFile`/`writeFile`
  // without a race against a non-existent parent dir.
  fs.writeFileSync(handoffPath, '');

  const selfPath = fileURLToPath(import.meta.url);
  const childArgs = ['pair'];
  if (outputMode === 'json') childArgs.push('--json');
  else if (outputMode === 'url-pin') childArgs.push('--url-pin');
  childArgs.push(PAIR_DETACH_FLAG);

  const child = spawn(process.execPath, [selfPath, ...childArgs], {
    detached: true,
    stdio: ['ignore', 'ignore', 'ignore'],
    env: { ...process.env, [PAIR_HANDOFF_ENV]: handoffPath },
  });
  child.unref();

  // Poll the handoff file. `tr pair` typical wall-clock to URL+PIN
  // is 100-500ms (one WS open + one frame round-trip). Cap the wait
  // at 15s — beyond that the child has either failed (relay down,
  // network issue) or gateway killed it before it could write.
  const start = Date.now();
  const POLL_INTERVAL_MS = 100;
  const MAX_WAIT_MS = 15_000;
  while (Date.now() - start < MAX_WAIT_MS) {
    let payload: string;
    try {
      payload = fs.readFileSync(handoffPath, 'utf-8');
    } catch {
      payload = '';
    }
    if (payload.trim().length > 0) {
      process.stdout.write(payload);
      // Best-effort cleanup — if the child is still active, it'll
      // re-write or it doesn't matter (we're about to exit).
      try { fs.unlinkSync(handoffPath); } catch { /* ignore */ }
      // Parent exits 0 — agent's tool call returns. Detached child
      // continues holding WS until phrase upload or relay TTL.
      process.exit(0);
    }
    await new Promise<void>((r) => setTimeout(r, POLL_INTERVAL_MS));
  }

  // Timed out — child never wrote URL+PIN. Surface the failure.
  try { fs.unlinkSync(handoffPath); } catch { /* ignore */ }
  die('pair handoff timed out — child failed to open relay session within 15s', 1);
}

async function runPairDetachedChild(outputMode: string, _args: string[]): Promise<void> {
  // We're the detached child. Open WS, write URL+PIN to the handoff
  // file (parent polls + prints), then block on phrase upload.
  const handoffPath = process.env[PAIR_HANDOFF_ENV];
  if (!handoffPath) {
    process.stderr.write(`tr-pair-child: missing ${PAIR_HANDOFF_ENV}\n`);
    process.exit(1);
  }

  const { runRelayPairCli } = await import('./pair-cli-relay.js');
  const { defaultRenderQr, buildDefaultPairCliIo } = await import('./pair-cli.js');

  // Capture stdout writes so we can route the URL+PIN line to the
  // handoff file before passing through to the (ignored) child stdout.
  const io = buildDefaultPairCliIo();
  const realStdout = process.stdout.write.bind(process.stdout);
  let handoffWritten = false;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (process.stdout as any).write = (chunk: any, ...rest: any[]): boolean => {
    if (!handoffWritten) {
      const text = typeof chunk === 'string' ? chunk : (chunk?.toString?.('utf-8') ?? String(chunk));
      // The pair-cli-relay's first stdout write in --json/--url-pin
      // mode is the URL+PIN payload as a single line.
      if (text.trim().length > 0 && (text.includes('"url"') || text.includes('"pair_url"'))) {
        try {
          fs.writeFileSync(handoffPath, text);
          handoffWritten = true;
        } catch (err) {
          process.stderr.write(`tr-pair-child: handoff write failed: ${String(err)}\n`);
        }
      }
    }
    return realStdout(chunk, ...rest);
  };

  const outcome = await runRelayPairCli('generate', {
    relayBaseUrl: CONFIG.pairRelayUrl,
    credentialsPath: CREDENTIALS_PATH,
    onboardingStatePath: STATE_PATH,
    logger: {
      info: (m: string) => process.stderr.write(`[info] ${m}\n`),
      warn: (m: string) => process.stderr.write(`[warn] ${m}\n`),
      error: (m: string) => process.stderr.write(`[error] ${m}\n`),
    },
    pluginVersion: PLUGIN_VERSION,
    deriveScopeAddress: undefined,
    renderQr: defaultRenderQr,
    io,
    outputMode: outputMode as import('./pair-cli.js').PairCliOutputMode,
  });

  // If the child exits before parent has read the handoff file
  // (the WS open frame failed before any URL+PIN to write), make sure
  // the parent doesn't hang on its 15s poll — write an error payload.
  if (!handoffWritten) {
    try {
      fs.writeFileSync(handoffPath, JSON.stringify({ error: `pair_failed:${outcome.status}` }) + '\n');
    } catch { /* ignore */ }
  }

  if (outcome.status !== 'completed' && outcome.status !== 'canceled') {
    process.exit(1);
  }
  if (outcome.status === 'canceled') {
    process.exit(130);
  }
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Command: remember
// ---------------------------------------------------------------------------

async function cmdRemember(rawArgs: string[]): Promise<void> {
  const [jsonMode, args] = popFlag(rawArgs, '--json');
  const text = args.join(' ').trim();
  if (!text) {
    die('Usage: tr remember [--json] <text>');
  }

  const ctx = await buildContext();

  // Build a minimal MemoryTaxonomy v1 claim blob (same format as storeExtractedFacts)
  const now = new Date().toISOString();
  const factId = randomUUID().replace(/-/g, '');

  // Encrypt the memory text
  const blob = JSON.stringify({
    text,
    type: 'claim',
    source: 'user',
    scope: 'unspecified',
    importance: 8,
    metadata: {
      type: 'claim',
      source: 'user',
      scope: 'unspecified',
      importance: 8,
    },
    timestamp: now,
    version: 'v1',
  });
  const encrypted_blob = encrypt(blob, ctx.encryptionKey);
  const blind_indices = generateBlindIndices(text);
  const content_fp = generateContentFingerprint(text, ctx.dedupKey);

  const payload = {
    id: factId,
    timestamp: now,
    encrypted_blob,
    blind_indices,
    decay_score: 8,
    source: 'cli:tr-remember',
    content_fp,
  };

  try {
    await ctx.apiClient.store(ctx.userId, [payload], ctx.authKeyHex);
    if (jsonMode) {
      // JSON-first output for agent parsing
      // claim_count requires an extra relay call to tally stored claims; not worth the latency — use 0
      log(JSON.stringify({ ok: true, id: factId, claim_count: 0 }));
    } else {
      log(`ok — stored memory (id=${factId})`);
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`remember failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Command: recall
// ---------------------------------------------------------------------------

async function cmdRecall(rawArgs: string[]): Promise<void> {
  const [jsonMode, argsAfterJson] = popFlag(rawArgs, '--json');
  const [limit, argsAfterLimit] = popLimitFlag(argsAfterJson, 5);
  const query = argsAfterLimit.join(' ').trim();
  if (!query) {
    die('Usage: tr recall [--json] [--limit N] <query>');
  }

  const ctx = await buildContext();

  // Generate word trapdoors for blind search
  const trapdoors = generateBlindIndices(query);

  if (trapdoors.length === 0) {
    if (jsonMode) {
      log(JSON.stringify({ results: [] }));
    } else {
      log('No results (0 searchable terms in query).');
    }
    return;
  }

  try {
    const candidates = await ctx.apiClient.search(ctx.userId, trapdoors, Math.min(limit * 2, 20), ctx.authKeyHex);

    const results: Array<{ text: string; score: number }> = [];

    for (const c of candidates) {
      try {
        const raw = decrypt(c.encrypted_blob, ctx.encryptionKey);
        const parsed = JSON.parse(raw) as { text?: string };
        if (parsed.text) {
          results.push({
            text: parsed.text,
            score: c.decay_score,
          });
        }
      } catch {
        // Skip undecryptable
      }
    }

    // Sort by score descending, then trim to limit
    results.sort((a, b) => b.score - a.score);
    const trimmed = results.slice(0, limit);

    if (jsonMode) {
      // JSON-first output for agent parsing — canonical format per spec
      log(JSON.stringify({ results: trimmed }));
    } else {
      log(`Found ${trimmed.length} result(s) for: ${query}`);
      for (const r of trimmed) {
        log(`  [score=${r.score.toFixed(2)}] ${r.text}`);
      }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`recall failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const cmd = args[0];

  switch (cmd) {
    case 'status': {
      const [jsonMode] = popFlag(args.slice(1), '--json');
      await cmdStatus(jsonMode);
      break;
    }

    case 'pair':
      await cmdPair(args.slice(1));
      break;

    case 'remember':
      await cmdRemember(args.slice(1));
      break;

    case 'recall':
      await cmdRecall(args.slice(1));
      break;

    case undefined:
    case '--help':
    case '-h':
      process.stdout.write(
        `TotalReclaw hybrid CLI v${PLUGIN_VERSION} (primary mode — OpenClaw 2026.5.2+)\n\n` +
        'Usage:\n' +
        '  tr status [--json]                       — onboarding + plugin load state\n' +
        '  tr pair [--json]                         — start a relay pairing session\n' +
        '  tr remember [--json] <text>              — store a memory\n' +
        '  tr recall [--json] [--limit N] <query>   — search memories (default limit: 5)\n\n' +
        'Flags:\n' +
        '  --json    Output machine-parseable JSON (required for agent shell calls)\n' +
        '  --limit N Limit recall results (default: 5)\n\n' +
        'JSON output shapes:\n' +
        '  status:   {"version":"...","onboarded":bool,"next_step":"pair|none","tool_count":N,"hybrid_mode":bool}\n' +
        '  pair:     {"url":"...","pin":"123456","expires_at":"..."}\n' +
        '  remember: {"ok":true,"id":"...","claim_count":N}\n' +
        '  recall:   {"results":[{"text":"...","score":0.8}]}\n\n' +
        'Environment:\n' +
        '  TOTALRECLAW_SERVER_URL           — relay URL (default: api-staging.totalreclaw.xyz)\n' +
        '  TOTALRECLAW_CREDENTIALS_PATH     — override credentials.json path\n',
      );
      break;

    default:
      die(`Unknown command: ${cmd}. Run \`tr --help\` for usage.`);
  }
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  process.stderr.write(`tr: fatal: ${msg}\n`);
  process.exit(2);
});
