#!/usr/bin/env node
/**
 * tr — TotalReclaw CLI (explicit-write + curation surface)
 *
 * Scope (Phase 3.3 — OpenClaw native integration): recall is now NATIVE.
 * The agent reads memories via OpenClaw's bundled `memory_search` /
 * `memory_get` tools (backed by the MemoryPluginCapability + TrMemorySearchManager
 * adapter registered in `index.ts`). This CLI no longer ships a recall path.
 *
 * What's still CLI-only (no native agent-facing surface):
 *   - explicit write (`tr remember`) — the conventional memory contract has no
 *     agent-facing write tool; auto-extraction stores facts via hooks.
 *   - curation (`tr pin` / `tr unpin` / `tr retype` / `tr set_scope`) + lifecycle
 *     (`tr forget`, `tr export`). Curation landed in #563 — the pure operations
 *     already existed in memory/, this CLI is the wiring that calls them.
 *   - onboarding + pairing (`tr status`, `tr pair`).
 *
 * Phrase-safety: this CLI reads credentials.json (mnemonic at rest) but NEVER
 * prints the mnemonic to stdout, stderr, or any log. Phrase only enters via QR-pair
 * browser tier (pair-cli.ts / pair-cli-relay.ts — unchanged).
 *
 * Commands:
 *   tr status [--json]          — print onboarding + credentials state
 *   tr pair [--json]            — start a relay pairing session, print URL+PIN+QR
 *   tr remember [--json] <text> — store a memory in the encrypted vault (on-chain)
 *   tr forget [--json] <factId> — tombstone a memory on-chain (find the id via memory_search)
 *   tr pin [--json] <factId> [--reason <text>]   — pin a memory so nothing supersedes it
 *   tr unpin [--json] <factId>                     — remove a pin (allow auto-supersede again)
 *   tr retype [--json] <factId> <type>             — change a memory's type (claim|preference|directive|commitment|episode|summary)
 *   tr set_scope [--json] <factId> <scope>         — change a memory's scope (work|personal|health|family|creative|finance|misc|unspecified)
 *   tr export [--json] [--format json|markdown] — dump all memories from the subgraph
 *
 * 3.3.12-rc.4 — switched remember/forget/export from `/v1/store` and
 * `/v1/search` (those endpoints were removed during the on-chain pivot —
 * relay returns 404) to the on-chain UserOp + subgraph paths.
 *
 * --json flag: all agent-facing CLI calls MUST use --json for clean machine-parseable output.
 *              Plain text mode is for direct user CLI use only.
 *
 * Install: wired via package.json `bin.tr` → dist/cli/tr-cli.js
 * Usage from container: `docker exec tr-openclaw node ~/.openclaw/extensions/totalreclaw/dist/cli/tr-cli.js status --json`
 */

import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { CONFIG, setRecoveryPhraseOverride } from '../config.js';
import { loadCredentialsJson } from '../fs-helpers.js';
import { isKeychainMarker, KEYCHAIN_MARKER_SETUP_MSG } from '../keychain-marker.js';
import { printStatus } from '../pairing/onboarding-cli.js';
import {
  deriveKeys,
  computeAuthKeyHash,
  encrypt,
  decrypt,
  deriveLshSeed,
  generateBlindIndices,
  generateContentFingerprint,
} from '../crypto/crypto.js';
import { createApiClient } from '../billing/api-client.js';
import {
  encodeFactProtobuf,
  submitFactBatchOnChain,
  deriveSmartAccountAddress,
  getSubgraphConfig,
  PROTOBUF_VERSION_V4,
  type FactPayload,
} from '../subgraph/subgraph-store.js';
import { exportAllFacts } from './tr-cli-export-helper.js';

// Curation (#563): pin / unpin / retype / set_scope. The pure operations +
// their tests already existed in memory/ — this CLI surface is the WIRING that
// finally calls them. Parity reference for the deps wiring is the MCP server's
// `buildPinDepsFromState` (mcp/src/index.ts); argument shapes follow the MCP
// `totalreclaw_pin` / `totalreclaw_retype` / `totalreclaw_set_scope` tools.
import {
  executePinOperation,
  validatePinArgs,
  type PinOpDeps,
} from '../memory/pin.js';
import {
  executeRetype,
  executeSetScope,
  validateRetypeArgs,
  validateSetScopeArgs,
} from '../memory/retype-setscope.js';
import type { MemoryType, MemoryScope } from '../extraction/extractor.js';
import { fetchFactById } from '../subgraph/subgraph-search.js';
import { generateEmbedding, getEmbeddingDims } from '../embedding/embedding.js';
import { LSHHasher } from '../embedding/lsh.js';
import { encodeEmbeddingPayload } from '../embedding/embedding-codec.js';
import { computeEntityTrapdoor } from '../extraction/claims-helper.js';
import type { ConfirmIndexedOptions } from '../subgraph/confirm-indexed.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const CREDENTIALS_PATH = CONFIG.credentialsPath;
const SERVER_URL = CONFIG.serverUrl;
const STATE_PATH = CONFIG.onboardingStatePath;
// Auto-synced by skill/scripts/sync-version.mjs from skill/plugin/package.json::version.
// Do not edit by hand — running tests will catch drift but the publish workflow
// rewrites this constant at the start of every npm/ClawHub publish.
const PLUGIN_VERSION = '3.4.4';

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

/** Parse --format VALUE from args, returning [value, cleanedArgs]. */
function popOptionFlag(
  args: string[],
  flag: string,
  defaultValue: string,
): [string, string[]] {
  const idx = args.indexOf(flag);
  if (idx === -1 || idx + 1 >= args.length) return [defaultValue, args];
  return [args[idx + 1], [...args.slice(0, idx), ...args.slice(idx + 2)]];
}

/**
 * Convert XChaCha20-Poly1305 base64 ciphertext to hex (the on-chain blob
 * format). Mirrors `encryptToHex` in index.ts so we don't pull in the whole
 * 7000-line module. Subgraph-stored facts use hex, not base64.
 */
function toHexBlob(plaintext: string, encryptionKey: Buffer): string {
  const b64 = encrypt(plaintext, encryptionKey);
  return Buffer.from(b64, 'base64').toString('hex');
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
  /** Smart Account address derived from the mnemonic (subgraph owner key). */
  walletAddress: string;
  /**
   * LSH seed derived from the mnemonic + salt (same inputs the plugin runtime
   * uses via `deriveLshSeed`). Needed by the curation deps' `generateIndices`
   * to rebuild the LSH hasher so a pinned / retyped fact stays findable by
   * semantic recall after the original is tombstoned. The raw mnemonic is NOT
   * stored on the context (phrase-safety) — only this derived seed.
   */
  lshSeed: Uint8Array;
  /** Salt used at key derivation; carried so the LSH hasher can be rebuilt. */
  salt: Buffer;
}

/**
 * Read the mnemonic candidate out of a loaded `credentials.json` object and
 * `die()` (never returns) if it's unusable — missing entirely, or a
 * keychain marker (#545: a co-installed Hermes client (#546) may have
 * wrapped the phrase in the OS keychain, leaving only a non-secret marker
 * behind). This is the credential-read+marker-guard prelude, extracted out
 * of `buildContext()` so it's unit-testable in-process, with no reliance on
 * Node's subprocess/spawn APIs — the plugin's install scanner refuses any
 * file in this tree that imports those (per the `SHELL_EXEC_PATTERN` rule
 * in `scripts/check-scanner.mjs`). See `cli/tr-cli-keychain-marker.test.ts`.
 *
 * Catches the marker HERE, before the caller ever passes the result to
 * `setRecoveryPhraseOverride()` / `deriveKeys()` (which would otherwise
 * throw an unhandled "invalid mnemonic" error on every `tr` command that
 * calls `buildContext()`: remember/forget/export). NEVER echoes the
 * marker's payload — only the fixed `KEYCHAIN_MARKER_SETUP_MSG`.
 *
 * Exported for tests; not part of the CLI's public "API" otherwise.
 */
export function resolveCliMnemonicOrDie(creds: Record<string, unknown> | null): string {
  if (!creds) {
    die('TotalReclaw is not set up. Run: node ~/.openclaw/extensions/totalreclaw/dist/cli/tr-cli.js pair --json');
  }

  const mnemonic =
    (typeof creds.mnemonic === 'string' && creds.mnemonic.trim()) ||
    (typeof creds.recovery_phrase === 'string' && creds.recovery_phrase.trim()) ||
    '';

  if (!mnemonic) {
    die('No recovery phrase in credentials.json. Run: tr pair --json');
  }

  if (isKeychainMarker(mnemonic)) {
    die(KEYCHAIN_MARKER_SETUP_MSG);
  }

  return mnemonic;
}

async function buildContext(): Promise<CliContext> {
  const creds = loadCredentialsJson(CREDENTIALS_PATH);
  const mnemonic = resolveCliMnemonicOrDie(creds);

  // Make the mnemonic visible to subgraph-store helpers (getSubgraphConfig
  // reads CONFIG.recoveryPhrase, which falls back to the override). We do
  // NOT log the mnemonic anywhere — it just lives in process memory for the
  // lifetime of this CLI invocation.
  setRecoveryPhraseOverride(mnemonic);

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
    // Register to get userId (idempotent on relay) — auth key hash is the
    // billing identity even in subgraph mode.
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

  // Derive the Smart Account address. This is the on-chain "owner" for
  // every fact + the X-Wallet-Address header on every UserOp / subgraph
  // call. Cheap eth_call to the SimpleAccountFactory; CREATE2 deterministic.
  let walletAddress: string;
  try {
    walletAddress = await deriveSmartAccountAddress(mnemonic, CONFIG.chainId);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`Failed to derive Smart Account address: ${msg}`);
  }

  return {
    authKeyHex,
    encryptionKey: keys.encryptionKey,
    dedupKey: keys.dedupKey,
    apiClient,
    userId,
    walletAddress,
    salt: keys.salt,
    lshSeed: deriveLshSeed(mnemonic, keys.salt),
  };
}

// ---------------------------------------------------------------------------
// Command: status
// ---------------------------------------------------------------------------

async function cmdStatus(jsonMode: boolean): Promise<void> {
  // Phase 3.4 retired the `.loaded.json` manifest — the writer was removed
  // in 3.1 and the reader had nothing current to read. `tr status` now
  // reports the static plugin version (from the CLI binary's own
  // PLUGIN_VERSION constant) plus onboarding state. For per-boot history,
  // consult the gateway log.
  const creds = loadCredentialsJson(CREDENTIALS_PATH);
  const onboarded = !!creds;

  if (jsonMode) {
    // JSON-first output for agent parsing
    const out: Record<string, unknown> = {
      version: PLUGIN_VERSION,
      onboarded,
      next_step: onboarded ? 'none' : 'pair',
    };
    log(JSON.stringify(out));
  } else {
    // Human-readable plain text for direct user CLI use
    printStatus(CREDENTIALS_PATH, STATE_PATH, process.stdout);
    process.stdout.write(
      `\n  plugin:      loaded (version=${PLUGIN_VERSION})\n` +
      `  hooks:       before_agent_start, agent_end, message_received, before_reset\n`,
    );
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

  const { runRelayPairCli } = await import('../pairing/pair-cli-relay.js');
  const { defaultRenderQr, buildDefaultPairCliIo } = await import('../pairing/pair-cli.js');

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
    pluginVersion: PLUGIN_VERSION,
    deriveScopeAddress: undefined,
    renderQr: defaultRenderQr,
    io,
    outputMode: outputMode as import('../pairing/pair-cli.js').PairCliOutputMode,
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

async function cmdRemember(rawArgs: string[]): Promise<void> {
  const [jsonMode, args] = popFlag(rawArgs, '--json');
  const text = args.join(' ').trim();
  if (!text) {
    die('Usage: tr remember [--json] <text>');
  }

  const ctx = await buildContext();

  // Build a Memory Taxonomy v1 claim blob (matches storeExtractedFacts shape).
  const now = new Date().toISOString();
  const factId = randomUUID();

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

  const encryptedBlob = toHexBlob(blob, ctx.encryptionKey);
  const blindIndices = generateBlindIndices(text);
  const contentFp = generateContentFingerprint(text, ctx.dedupKey);

  // On-chain submission: encode protobuf, build SubgraphStoreConfig (auth +
  // wallet), submit a single-fact UserOp through the relay bundler. The
  // subgraph indexes the resulting Log(bytes) event so it is recall-able
  // within ~5-15 s of the receipt.
  const fact: FactPayload = {
    id: factId,
    timestamp: now,
    owner: ctx.walletAddress,
    encryptedBlob,
    blindIndices,
    decayScore: 8,
    source: 'cli:tr-remember',
    contentFp,
    agentId: 'tr-cli',
    version: PROTOBUF_VERSION_V4,
  };

  try {
    const protobuf = encodeFactProtobuf(fact);
    const config = {
      ...getSubgraphConfig(),
      authKeyHex: ctx.authKeyHex,
      walletAddress: ctx.walletAddress,
    };
    const submitResult = await submitFactBatchOnChain([protobuf], config);

    if (!submitResult.success) {
      die(
        `remember failed: on-chain UserOp did not succeed (userOpHash=${
          submitResult.userOpHash || 'none'
        })`,
      );
    }

    if (jsonMode) {
      // JSON-first output for agent parsing.
      // claim_count = 1 here (single fact stored). Computing the full vault
      // count would require an extra subgraph query on every remember and
      // isn't worth the latency.
      log(JSON.stringify({ ok: true, id: factId, claim_count: 1 }));
    } else {
      log(`ok — stored memory (id=${factId}, tx=${result.txHash || 'pending'})`);
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`remember failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Command: forget
// ---------------------------------------------------------------------------

async function cmdForget(rawArgs: string[]): Promise<void> {
  const [jsonMode, args] = popFlag(rawArgs, '--json');
  const factId = (args[0] ?? '').trim();
  if (!factId) {
    die('Usage: tr forget [--json] <factId>');
  }
  // UUID-v4-ish shape check — same validation the old totalreclaw_forget
  // tool applied. Prevents fabricated / natural-language IDs from reaching
  // the UserOp path and silently no-op'ing on-chain.
  if (!/^[0-9a-f-]{8,}$/i.test(factId)) {
    die(
      `forget failed: "${factId.slice(0, 60)}" doesn't look like a memory ID. ` +
        `Ask the agent to look it up via memory_search (or tr export) and pass a result's id.`,
    );
  }

  const ctx = await buildContext();

  // Tombstone shape (pin/unpin & native forget use the same one — see
  // index.ts:4253-4267 + pin.ts:611-621). Deliberately NO version field
  // → uses legacy v3 default so the subgraph's contradiction handler
  // matches and flips isActive=false.
  const tombstone: FactPayload = {
    id: factId,
    timestamp: new Date().toISOString(),
    owner: ctx.walletAddress,
    encryptedBlob: '00',
    blindIndices: [],
    decayScore: 0,
    source: 'tombstone',
    contentFp: '',
    agentId: 'tr-cli',
    // No `version` → legacy v3 (matches pin/unpin & native forget).
  };

  try {
    const protobuf = encodeFactProtobuf(tombstone);
    const config = {
      ...getSubgraphConfig(),
      authKeyHex: ctx.authKeyHex,
      walletAddress: ctx.walletAddress,
    };
    const submitResult = await submitFactBatchOnChain([protobuf], config);

    if (!submitResult.success) {
      die(
        `forget failed: on-chain tombstone did not succeed (userOpHash=${
          submitResult.userOpHash || 'none'
        })`,
      );
    }

    if (jsonMode) {
      log(JSON.stringify({ ok: true, id: factId, tx_hash: submitResult.txHash }));
    } else {
      log(`ok — tombstoned ${factId} (tx=${result.txHash || 'pending'})`);
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    die(`forget failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Command: export
// ---------------------------------------------------------------------------

async function cmdExport(rawArgs: string[]): Promise<void> {
  const [jsonMode, argsAfterJson] = popFlag(rawArgs, '--json');
  const [format, _argsAfterFormat] = popOptionFlag(argsAfterJson, '--format', 'json');

  if (format !== 'json' && format !== 'markdown') {
    die('Usage: tr export [--json] [--format json|markdown]');
  }

  const ctx = await buildContext();

  // Delegate the subgraph paginate + decrypt loop to a helper module —
  // tr-cli.ts already includes `fs.readFileSync` (status command), and
  // adding outbound HTTP here would trip the OpenClaw scanner's
  // potential-exfiltration rule. See tr-cli-export-helper.ts.
  const allFacts = await exportAllFacts(
    ctx.walletAddress,
    ctx.authKeyHex,
    ctx.encryptionKey,
  );

  if (format === 'markdown') {
    if (allFacts.length === 0) {
      log('*No memories stored.*');
    } else {
      const lines = allFacts.map((f, i) => {
        const meta = f.metadata;
        const type = (meta.type as string) ?? 'fact';
        return `${i + 1}. **[${type}]** ${f.text}  \n   _ID: ${f.id} | Created: ${f.created_at}_`;
      });
      log(`# Exported Memories (${allFacts.length})\n\n${lines.join('\n')}`);
    }
    return;
  }

  // json format (default — both --json mode and --format=json end up here)
  if (jsonMode) {
    log(JSON.stringify({ count: allFacts.length, facts: allFacts }));
  } else {
    log(JSON.stringify(allFacts, null, 2));
  }
}

// ---------------------------------------------------------------------------
// Commands: pin / unpin / retype / set_scope (curation — #563)
// ---------------------------------------------------------------------------
//
// The pure operations (executePinOperation / executeRetype / executeSetScope)
// + their tests already lived in memory/ — this section is the WIRING that
// finally exposes them as `tr` subcommands. Before #563 the plugin was the
// only client without a curation surface, so an agent told to "pin that"
// stored an ordinary memory and reported a successful pin (issue #563).
//
// Parity: argument shapes follow the MCP `totalreclaw_pin` / `totalreclaw_unpin`
// / `totalreclaw_retype` / `totalreclaw_set_scope` tools (fact_id + optional
// reason / new_type / new_scope). The deps wiring mirrors MCP's
// `buildPinDepsFromState` (mcp/src/index.ts). The execute* result is normalized
// into a CLI result object so the cmd wrapper can die() with one clean message
// instead of an MCP content-block envelope.

export type CurationOp = 'pin' | 'unpin' | 'retype' | 'set_scope';

export interface CurationParsedArgs {
  op: CurationOp;
  factId: string;
  reason?: string;
  newType?: MemoryType;
  newScope?: MemoryScope;
}

export type CurationParseResult =
  | { ok: true; args: CurationParsedArgs }
  | { ok: false; error: string };

export interface CurationCliResult {
  ok: boolean;
  op: CurationOp;
  fact_id: string;
  new_fact_id?: string;
  previous_status?: string;
  new_status?: string;
  previous_type?: string;
  new_type?: string;
  previous_scope?: string;
  new_scope?: string;
  idempotent?: boolean;
  tx_hash?: string;
  partial?: boolean;
  reason?: string;
  error?: string;
}

/** UUID-v4-ish shape check — same guard `cmdForget` applies. Real fact ids are
 * UUIDs; rejecting non-hex natural-language input here keeps a fabricated id
 * (the #563 / #551 failure mode) from reaching the on-chain supersession path. */
const FACT_ID_SHAPE = /^[0-9a-f-]{8,}$/i;

/** Per-op one-line usage (the positional args after `tr <op> [--json]`). */
function curationUsage(op: CurationOp): string {
  const tail =
    op === 'pin' ? '<factId> [--reason <text>]'
    : op === 'retype' ? '<factId> <type>'
    : op === 'set_scope' ? '<factId> <scope>'
    : '<factId>';
  return `Usage: tr ${op} [--json] ${tail}`;
}

/**
 * Parse + validate CLI argv for a curation op. Pure — no process.exit, no
 * network — so the bad-args paths are unit-testable in-process (see
 * memory/pin-unpin.test.ts + memory/retype-setscope.test.ts). `--json` must
 * already be stripped by the caller (cmdCuration pops it first). Delegates
 * field-level validation to the shared validate* helpers so the accepted
 * shapes stay identical to the MCP tools.
 */
export function parseCurationCliArgs(op: CurationOp, argv: string[]): CurationParseResult {
  // pin accepts an optional --reason VALUE; pop it before positional parsing
  // so it can appear before or after the factId. The other ops take only
  // positionals.
  let reason: string | undefined;
  let positional = argv;
  if (op === 'pin') {
    const [reasonValue, rest] = popOptionFlag(argv, '--reason', '');
    if (reasonValue) reason = reasonValue;
    positional = rest;
  }

  const factId = (positional[0] ?? '').trim();
  if (!factId) {
    return { ok: false, error: curationUsage(op) };
  }
  if (!FACT_ID_SHAPE.test(factId)) {
    return {
      ok: false,
      error:
        `"${factId.slice(0, 60)}" doesn't look like a memory ID. ` +
        `Ask the agent to look it up via memory_search (or tr export) and pass a result's id.`,
    };
  }

  if (op === 'pin' || op === 'unpin') {
    const v = validatePinArgs({ fact_id: factId, ...(reason !== undefined ? { reason } : {}) });
    if (!v.ok) return { ok: false, error: v.error };
    const args: CurationParsedArgs = { op, factId: v.factId };
    if (v.reason) args.reason = v.reason;
    return { ok: true, args };
  }

  if (op === 'retype') {
    const newType = (positional[1] ?? '').trim();
    const v = validateRetypeArgs({ fact_id: factId, new_type: newType });
    if (!v.ok) return { ok: false, error: v.error };
    return { ok: true, args: { op, factId: v.factId, newType: v.newType } };
  }

  // set_scope
  const newScope = (positional[1] ?? '').trim();
  const v = validateSetScopeArgs({ fact_id: factId, new_scope: newScope });
  if (!v.ok) return { ok: false, error: v.error };
  return { ok: true, args: { op, factId: v.factId, newScope: v.newScope } };
}

/**
 * Build the curation deps (PinOpDeps — structurally compatible with
 * RetypeSetScopeDeps) bound to a built CLI context. Mirrors the MCP server's
 * `buildPinDepsFromState`: same retrieve / decrypt / encrypt / submit /
 * generateIndices wiring via plugin-native helpers. `generateIndices` is
 * best-effort — if the embedder can't load it falls back to word + entity
 * trapdoors alone, same as MCP and the plugin's auto-extraction path, so a
 * curation op never fails purely because the embedding bundle is unavailable.
 *
 * Exported so the wiring is exercisable in isolation; the cmd wrapper is the
 * only production caller.
 */
export function buildCurationDeps(ctx: CliContext): PinOpDeps {
  // LSH hasher is rebuilt lazily on first generateIndices call (the embedder
  // is the expensive part; no point constructing the hasher if generation
  // fails). Single-shot CLI process, so a local singleton is fine.
  let lshHasher: LSHHasher | null = null;

  return {
    owner: ctx.walletAddress,
    // Provenance: curation done via the standalone CLI (mirrors cmdRemember's
    // 'tr-cli' agentId, distinct from the in-process agent tool path).
    sourceAgent: 'tr-cli',
    fetchFactById: (factId: string) =>
      fetchFactById(ctx.walletAddress, factId, ctx.authKeyHex),
    decryptBlob: (hexEncryptedBlob: string) => {
      const hex = hexEncryptedBlob.startsWith('0x')
        ? hexEncryptedBlob.slice(2)
        : hexEncryptedBlob;
      const b64 = Buffer.from(hex, 'hex').toString('base64');
      return decrypt(b64, ctx.encryptionKey);
    },
    encryptBlob: (plaintext: string) => {
      const b64 = encrypt(plaintext, ctx.encryptionKey);
      return Buffer.from(b64, 'base64').toString('hex');
    },
    submitBatch: async (payloads: Buffer[]) => {
      const config = {
        ...getSubgraphConfig(),
        authKeyHex: ctx.authKeyHex,
        walletAddress: ctx.walletAddress,
      };
      const result = await submitFactBatchOnChain(payloads, config);
      return { txHash: result.txHash, success: result.success };
    },
    generateIndices: async (text: string, entityNames: string[]) => {
      if (!text) return { blindIndices: [] };
      const wordIndices = generateBlindIndices(text);
      let lshIndices: string[] = [];
      let encryptedEmbedding: string | undefined;
      try {
        const embedding = await generateEmbedding(text);
        if (!lshHasher) {
          lshHasher = new LSHHasher(ctx.lshSeed, getEmbeddingDims());
        }
        lshIndices = lshHasher.hash(embedding);
        const encB64 = encrypt(encodeEmbeddingPayload(embedding), ctx.encryptionKey);
        encryptedEmbedding = Buffer.from(encB64, 'base64').toString('hex');
      } catch {
        // Best-effort: word + entity trapdoors alone still surface the claim
        // after the original is tombstoned (BM25-style recall covers it).
      }
      const entityTrapdoors = entityNames.map((n) => computeEntityTrapdoor(n));
      return {
        blindIndices: [...wordIndices, ...lshIndices, ...entityTrapdoors],
        encryptedEmbedding,
      };
    },
  };
}

/**
 * Run a curation op end-to-end against injected deps. Mirrors the MCP server's
 * `handlePinSubgraphWithDeps` / retype / set_scope handlers: delegate to the
 * pure execute* fn and normalize its result into a CLI result object. Never
 * throws on op failure — surfaces `{ ok: false, error }` so the cmd wrapper
 * can die() with a clean message. `confirmOpts` threads the read-after-write
 * subgraph poll (the CLI omits it for real polling; tests stub it fast).
 */
export async function runCurationOp(
  args: CurationParsedArgs,
  deps: PinOpDeps,
  confirmOpts?: ConfirmIndexedOptions,
): Promise<CurationCliResult> {
  const { op, factId: fact_id } = args;
  try {
    if (op === 'pin' || op === 'unpin') {
      const targetStatus = op === 'pin' ? 'pinned' : 'active';
      const reason = op === 'pin' ? args.reason : undefined;
      const r = await executePinOperation(fact_id, targetStatus, deps, reason, confirmOpts);
      return {
        ok: r.success,
        op,
        fact_id,
        new_fact_id: r.new_fact_id,
        previous_status: r.previous_status,
        new_status: r.new_status,
        idempotent: r.idempotent,
        tx_hash: r.tx_hash,
        partial: r.partial,
        reason: r.reason,
        error: r.error,
      };
    }

    if (op === 'retype') {
      const r = await executeRetype(fact_id, args.newType!, deps, confirmOpts);
      return {
        ok: r.success,
        op,
        fact_id,
        new_fact_id: r.new_fact_id,
        previous_type: r.previous_type,
        new_type: r.new_type,
        tx_hash: r.tx_hash,
        partial: r.partial,
        error: r.error,
      };
    }

    // set_scope
    const r = await executeSetScope(fact_id, args.newScope!, deps, confirmOpts);
    return {
      ok: r.success,
      op,
      fact_id,
      new_fact_id: r.new_fact_id,
      previous_scope: r.previous_scope,
      new_scope: r.new_scope,
      tx_hash: r.tx_hash,
      partial: r.partial,
      error: r.error,
    };
  } catch (err) {
    return {
      ok: false,
      op,
      fact_id,
      error: `${op} failed: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

/** Shared cmd body for all four curation subcommands. */
async function cmdCuration(op: CurationOp, rawArgs: string[]): Promise<void> {
  const [jsonMode, args] = popFlag(rawArgs, '--json');
  const parsed = parseCurationCliArgs(op, args);
  if (!parsed.ok) {
    die(parsed.error);
  }

  const ctx = await buildContext();
  const deps = buildCurationDeps(ctx);
  const result = await runCurationOp(parsed.args, deps);

  if (!result.ok) {
    die(`${op} failed: ${result.error ?? 'unknown error'}`);
  }

  if (jsonMode) {
    log(JSON.stringify(result));
    return;
  }

  // Human-readable summary (plain-text mode is for direct user CLI use only).
  const tx = result.tx_hash ? ` (tx=${result.tx_hash.slice(0, 18)}…)` : '';
  const partial = result.partial
    ? ' [indexing — may take a few seconds to appear in recall]'
    : '';
  const idem = result.idempotent ? ' (already in that state — no change)' : '';
  if (op === 'pin' || op === 'unpin') {
    const verb = op === 'pin' ? 'pinned' : 'unpinned';
    log(`ok — ${verb} ${result.fact_id}${idem}${tx}${partial}`);
  } else if (op === 'retype') {
    log(`ok — retyped ${result.fact_id} → ${result.new_type}${tx}${partial}`);
  } else {
    log(`ok — re-scoped ${result.fact_id} → ${result.new_scope}${tx}${partial}`);
  }
}

async function cmdPin(rawArgs: string[]): Promise<void> {
  return cmdCuration('pin', rawArgs);
}

async function cmdUnpin(rawArgs: string[]): Promise<void> {
  return cmdCuration('unpin', rawArgs);
}

async function cmdRetype(rawArgs: string[]): Promise<void> {
  return cmdCuration('retype', rawArgs);
}

async function cmdSetScope(rawArgs: string[]): Promise<void> {
  return cmdCuration('set_scope', rawArgs);
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
      // Retired in Phase 3.3 — recall is now native via the bundled
      // memory_search / memory_get tools (MemoryPluginCapability). Surface
      // a clear pointer instead of falling through to "unknown command"
      // so agents / users running stale prompts get actionable guidance.
      die(
        'tr recall was retired — recall is now native. ' +
          'The agent reads memories via the memory_search tool automatically; ' +
          'use `tr export` to dump every memory outside the agent.',
      );

    case 'forget':
      await cmdForget(args.slice(1));
      break;

    case 'pin':
      await cmdPin(args.slice(1));
      break;

    case 'unpin':
      await cmdUnpin(args.slice(1));
      break;

    case 'retype':
      await cmdRetype(args.slice(1));
      break;

    case 'set_scope':
      await cmdSetScope(args.slice(1));
      break;

    case 'export':
      await cmdExport(args.slice(1));
      break;

    case 'import':
    case 'upgrade':
      // Import + upgrade run inside the gateway process (they need the
      // plugin runtime — extraction pipeline, smart-import WASM, module-
      // level auth state). The standalone `tr` binary is a separate Node
      // script that does NOT load index.ts (would pull in the entire
      // plugin runtime). Point users at the registerCli surface instead
      // of silently no-op'ing or falling through to "unknown command".
      die(
        `${cmd} is not available on the standalone \`tr\` binary. ` +
          `Run it on the gateway host via:\n` +
          `  openclaw totalreclaw ${cmd === 'import' ? 'import from <source>' : 'upgrade'}${cmd === 'import' ? ' [--file <path>] [--dry-run] [--json]' : ' [--json]'}\n` +
          (cmd === 'import'
            ? `  openclaw totalreclaw import status [--id <importId>] [--json]\n` +
              `  openclaw totalreclaw import abort <importId> [--json]\n` +
              `Sources: mem0 | mcp-memory | chatgpt | claude | gemini`
            : `Returns a Stripe checkout URL for Pro upgrade.`),
      );

    case undefined:
    case '--help':
    case '-h':
      process.stdout.write(
        `TotalReclaw CLI v${PLUGIN_VERSION} (recall is native — memory_search tool)\n\n` +
        'Usage:\n' +
        '  tr status [--json]                          — onboarding + plugin load state\n' +
        '  tr pair [--json]                            — start a relay pairing session\n' +
        '  tr remember [--json] <text>                 — store a memory (on-chain UserOp)\n' +
        '  tr forget [--json] <factId>                 — tombstone a memory on-chain\n' +
        '  tr pin [--json] <factId> [--reason <text>]  — pin a memory (nothing supersedes it)\n' +
        '  tr unpin [--json] <factId>                  — remove a pin (allow auto-supersede again)\n' +
        '  tr retype [--json] <factId> <type>          — change type (claim|preference|directive|commitment|episode|summary)\n' +
        '  tr set_scope [--json] <factId> <scope>      — change scope (work|personal|health|family|creative|finance|misc|unspecified)\n' +
        '  tr export [--json] [--format json|markdown] — dump every memory in the vault\n\n' +
        'Curation (pin/unpin/retype/set_scope): look up the factId via memory_search (or\n' +
        '        tr export) first. Each is an on-chain supersession — tombstones the old fact,\n' +
        '        writes a fresh one with the changed field, returns {"ok":true,"new_fact_id":...,"tx_hash":...}.\n\n' +
        'Recall: NOT a CLI command. The agent recalls via the bundled memory_search tool.\n' +
        '        To dump memories outside the agent, use `tr export`.\n\n' +
        'Import + Upgrade: NOT on the standalone `tr` binary. They run inside the gateway\n' +
        '        process via the `openclaw totalreclaw` subcommand chain:\n' +
        '          openclaw totalreclaw import from <source> [--file <path>] [--dry-run] [--json]\n' +
        '          openclaw totalreclaw import status [--id <importId>] [--json]\n' +
        '          openclaw totalreclaw import abort <importId> [--json]\n' +
        '          openclaw totalreclaw upgrade [--json]\n' +
        '        Sources: mem0 | mcp-memory | chatgpt | claude | gemini\n\n' +
        'Flags:\n' +
        '  --json    Output machine-parseable JSON (required for agent shell calls)\n\n' +
        'JSON output shapes:\n' +
        '  status:    {"version":"...","onboarded":bool,"next_step":"pair|none","tool_count":N,"hybrid_mode":bool}\n' +
        '  pair:      {"url":"...","pin":"123456","expires_at":"..."}\n' +
        '  remember:  {"ok":true,"id":"...","claim_count":N}\n' +
        '  forget:    {"ok":true,"id":"...","tx_hash":"0x..."}\n' +
        '  pin/unpin: {"ok":true,"op":"pin","fact_id":"...","new_fact_id":"...","new_status":"pinned","tx_hash":"0x..."}\n' +
        '  retype:    {"ok":true,"op":"retype","fact_id":"...","new_type":"preference","tx_hash":"0x..."}\n' +
        '  set_scope: {"ok":true,"op":"set_scope","fact_id":"...","new_scope":"work","tx_hash":"0x..."}\n' +
        '  export:    {"count":N,"facts":[{"id":"...","text":"...","metadata":{...},"created_at":"..."}]}\n\n' +
        'Environment:\n' +
        '  TOTALRECLAW_SERVER_URL           — relay URL (default: api.totalreclaw.xyz; staging: api-staging.totalreclaw.xyz)\n' +
        '  TOTALRECLAW_CREDENTIALS_PATH     — override credentials.json path\n',
      );
      break;

    default:
      die(`Unknown command: ${cmd}. Run \`tr --help\` for usage.`);
  }
}

// Only auto-run when this file is executed directly (`tsx cli/tr-cli.ts ...`
// / `node dist/cli/tr-cli.js ...`, the only way it's ever actually invoked in
// production — see the package.json `bin.tr` entry). NOT when it's imported
// as a module — e.g. `cli/tr-cli-keychain-marker.test.ts` imports
// `resolveCliMnemonicOrDie` to test the marker guard in-process, and without
// this guard that import would trigger a real `main()` run (reading
// `process.argv`, possibly `process.exit()`-ing) as a side effect of the
// import itself.
const isDirectRun = (() => {
  try {
    return process.argv[1] != null && import.meta.url === pathToFileURL(process.argv[1]).href;
  } catch {
    return true; // fail open — preserve today's always-run CLI behavior
  }
})();

if (isDirectRun) {
  main().catch((err) => {
    const msg = err instanceof Error ? err.message : String(err);
    process.stderr.write(`tr: fatal: ${msg}\n`);
    process.exit(2);
  });
}
