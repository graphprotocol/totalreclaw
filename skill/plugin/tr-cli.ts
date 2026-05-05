#!/usr/bin/env node
/**
 * tr — TotalReclaw hybrid CLI (3.3.8-rc.1 workaround for OpenClaw 2026.5.2 issue #223)
 *
 * OpenClaw 2026.5.2 has a tool-policy-pipeline bug that strips non-bundled plugin tools
 * before they reach the agent toolset. This CLI bypasses the broken tool-registration
 * path entirely. The agent runs `tr <cmd>` from shell; the plugin keeps its hooks
 * (before_agent_start, agent_end, message_received) via the unbroken hook code path.
 *
 * Phrase-safety: this CLI reads credentials.json (mnemonic at rest) but NEVER
 * prints the mnemonic to stdout, stderr, or any log. Phrase only enters via QR-pair
 * browser tier (pair-cli.ts / pair-cli-relay.ts — unchanged).
 *
 * Commands:
 *   tr status               — print onboarding + credentials state
 *   tr pair [--json]        — start a relay pairing session, print URL+PIN+QR
 *   tr remember <text>      — store a memory in the encrypted vault
 *   tr recall <query>       — search the encrypted vault, print results as JSON
 *
 * Install: wired via package.json `bin.tr` → dist/tr-cli.js
 * Usage from container: `docker exec tr-openclaw tr status`
 */

import path from 'node:path';
import os from 'node:os';
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

function die(msg: string, code = 1): never {
  process.stderr.write(`tr: ${msg}\n`);
  process.exit(code);
}

function log(msg: string): void {
  process.stdout.write(msg + '\n');
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
    die('TotalReclaw is not set up. Run: openclaw totalreclaw onboard\n(or: tr pair)');
  }

  const mnemonic =
    (typeof creds.mnemonic === 'string' && creds.mnemonic.trim()) ||
    (typeof creds.recovery_phrase === 'string' && creds.recovery_phrase.trim()) ||
    '';

  if (!mnemonic) {
    die('No recovery phrase in credentials.json. Run: openclaw totalreclaw onboard');
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

async function cmdStatus(): Promise<void> {
  // Print onboarding + credentials state (never prints mnemonic — same as
  // the `openclaw totalreclaw status` subcommand surface).
  printStatus(CREDENTIALS_PATH, STATE_PATH, process.stdout);

  // Additional: loaded.json check to confirm plugin hooks are active.
  // Reads manifest written by register() in index.ts.
  // Probe both install paths: extensions/ (local tgz installs) and npm/ (registry installs).
  try {
    const fs = await import('node:fs');
    const candidatePaths = [
      // extensions-path (local tgz / --force install) — .loaded.json sits at root, not dist/
      path.join(os.homedir(), '.openclaw', 'extensions', 'totalreclaw', '.loaded.json'),
      // npm-path (registry install) — .loaded.json inside dist/
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
      const ageMs = Date.now() - (manifest.loadedAt ?? 0);
      const ageSec = Math.round(ageMs / 1000);
      process.stdout.write(
        `\n  plugin:      loaded (version=${manifest.version ?? '?'} bootCount=${manifest.bootCount ?? '?'} loaded=${ageSec}s ago)\n` +
        `  hybrid-mode: ${manifest.hybridMode ? 'yes (use tr <cmd>)' : 'no'}\n` +
        `  hooks:       before_agent_start, agent_end, message_received, before_reset\n` +
        `  note:        tools from .loaded.json are STRIPPED by OC 2026.5.2 issue #223;\n` +
        `               use \`tr <cmd>\` from shell instead\n`,
      );
    } else {
      process.stdout.write('\n  plugin:      .loaded.json not found — plugin may not be loaded\n');
    }
  } catch {
    // Best-effort
  }
}

// ---------------------------------------------------------------------------
// Command: pair
// ---------------------------------------------------------------------------

async function cmdPair(args: string[]): Promise<void> {
  // Delegate to the existing pair-cli-relay.ts via a thin wrapper.
  // The pair flow is relay-brokered (works through Docker NAT).
  // Phrase-safety: pair-cli-relay.ts is x25519-only; mnemonic never appears.
  const outputMode = args.includes('--json') ? 'json' : args.includes('--url-pin') ? 'url-pin' : 'human';

  const { runRelayPairCli } = await import('./pair-cli-relay.js');
  const { defaultRenderQr, buildDefaultPairCliIo } = await import('./pair-cli.js');

  const io = buildDefaultPairCliIo();
  const outcome = await runRelayPairCli('generate', {
    relayBaseUrl: CONFIG.pairRelayUrl,
    credentialsPath: CREDENTIALS_PATH,
    onboardingStatePath: STATE_PATH,
    logger: {
      info: (m: string) => process.stderr.write(`[info] ${m}\n`),
      warn: (m: string) => process.stderr.write(`[warn] ${m}\n`),
      error: (m: string) => process.stderr.write(`[error] ${m}\n`),
    },
    pluginVersion: '3.3.8-rc.1',
    deriveScopeAddress: undefined,
    renderQr: defaultRenderQr,
    io,
    outputMode: outputMode as import('./pair-cli.js').PairCliOutputMode,
  });

  if (outcome.status !== 'completed' && outcome.status !== 'canceled') {
    die(`Pairing ${outcome.status}`, 1);
  }
  if (outcome.status === 'canceled') {
    process.exit(130);
  }
}

// ---------------------------------------------------------------------------
// Command: remember
// ---------------------------------------------------------------------------

async function cmdRemember(args: string[]): Promise<void> {
  const text = args.join(' ').trim();
  if (!text) {
    die('Usage: tr remember <text>');
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
    log(JSON.stringify({ ok: true, id: factId, text }));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`remember failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Command: recall
// ---------------------------------------------------------------------------

async function cmdRecall(args: string[]): Promise<void> {
  const query = args.join(' ').trim();
  if (!query) {
    die('Usage: tr recall <query>');
  }

  const ctx = await buildContext();

  // Generate word trapdoors for blind search
  const trapdoors = generateBlindIndices(query);

  if (trapdoors.length === 0) {
    log(JSON.stringify({ ok: true, count: 0, memories: [] }));
    return;
  }

  try {
    const candidates = await ctx.apiClient.search(ctx.userId, trapdoors, 12, ctx.authKeyHex);

    const memories: Array<{ id: string; text: string; score: number; timestamp: string }> = [];

    for (const c of candidates) {
      try {
        const raw = decrypt(c.encrypted_blob, ctx.encryptionKey);
        const parsed = JSON.parse(raw) as { text?: string };
        if (parsed.text) {
          memories.push({
            id: c.fact_id,
            text: parsed.text,
            score: c.decay_score,
            timestamp: new Date(c.timestamp).toISOString(),
          });
        }
      } catch {
        // Skip undecryptable
      }
    }

    // Simple relevance sort by decay_score (descending)
    memories.sort((a, b) => b.score - a.score);

    log(JSON.stringify({ ok: true, count: memories.length, query, memories }));
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
    case 'status':
      await cmdStatus();
      break;

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
        'TotalReclaw hybrid CLI (OpenClaw 2026.5.2 issue #223 workaround)\n\n' +
        'Usage:\n' +
        '  tr status              — onboarding + plugin load state\n' +
        '  tr pair [--json]       — start a relay pairing session\n' +
        '  tr remember <text>     — store a memory\n' +
        '  tr recall <query>      — search memories (outputs JSON)\n\n' +
        'Environment:\n' +
        '  TOTALRECLAW_SERVER_URL — relay URL (default: api-staging.totalreclaw.xyz)\n' +
        '  TOTALRECLAW_CREDENTIALS_PATH — override credentials.json path\n',
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
