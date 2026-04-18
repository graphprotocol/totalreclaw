/**
 * TotalReclaw Plugin for OpenClaw
 *
 * Registers runtime tools so OpenClaw can execute TotalReclaw operations:
 *   - totalreclaw_remember     -- store an encrypted memory
 *   - totalreclaw_recall       -- search and decrypt memories
 *   - totalreclaw_forget       -- soft-delete a memory
 *   - totalreclaw_export       -- export all memories (JSON or Markdown)
 *   - totalreclaw_status       -- check billing/subscription status
 *   - totalreclaw_consolidate  -- scan and merge near-duplicate memories
 *   - totalreclaw_pin          -- pin a memory so auto-resolution can never supersede it
 *   - totalreclaw_unpin        -- remove a pin, returning the memory to active status
 *   - totalreclaw_import_from  -- import memories from other tools (Mem0, MCP Memory, etc.)
 *   - totalreclaw_upgrade      -- create Stripe checkout for Pro upgrade
 *   - totalreclaw_migrate      -- migrate testnet memories to mainnet after Pro upgrade
 *   - totalreclaw_setup        -- initialize with recovery phrase (no gateway restart needed)
 *
 * Also registers a `before_agent_start` hook that automatically injects
 * relevant memories into the agent's context.
 *
 * All data is encrypted client-side with XChaCha20-Poly1305. The server never
 * sees plaintext.
 */

import {
  deriveKeys,
  deriveLshSeed,
  computeAuthKeyHash,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateContentFingerprint,
} from './crypto.js';
import { createApiClient, type StoreFactPayload } from './api-client.js';
import {
  extractFacts,
  extractDebrief,
  isValidMemoryType,
  parseEntity,
  VALID_MEMORY_TYPES,
  LEGACY_V0_MEMORY_TYPES,
  VALID_MEMORY_SOURCES,
  VALID_MEMORY_SCOPES,
  EXTRACTION_SYSTEM_PROMPT,
  extractFactsForCompaction,
  type ExtractedFact,
  type ExtractedEntity,
  type MemoryType,
  type MemorySource,
  type MemoryScope,
} from './extractor.js';
import { initLLMClient, resolveLLMConfig, chatCompletion, generateEmbedding, getEmbeddingDims } from './llm-client.js';
import { LSHHasher } from './lsh.js';
import { rerank, cosineSimilarity, detectQueryIntent, INTENT_WEIGHTS, type RerankerCandidate } from './reranker.js';
import { deduplicateBatch } from './semantic-dedup.js';
import {
  findNearDuplicate,
  shouldSupersede,
  clusterFacts,
  getStoreDedupThreshold,
  getConsolidationThreshold,
  STORE_DEDUP_MAX_CANDIDATES,
  type DecryptedCandidate,
} from './consolidation.js';
import { isSubgraphMode, getSubgraphConfig, encodeFactProtobuf, submitFactOnChain, submitFactBatchOnChain, deriveSmartAccountAddress, PROTOBUF_VERSION_V4, type FactPayload } from './subgraph-store.js';
import {
  DIGEST_TRAPDOOR,
  buildCanonicalClaim,
  computeEntityTrapdoor,
  computeEntityTrapdoors,
  isDigestBlob,
  normalizeToV1Type,
  readClaimFromBlob,
  resolveDigestMode,
  type DigestMode,
} from './claims-helper.js';
import {
  maybeInjectDigest,
  recompileDigest,
  fetchAllActiveClaims,
  isRecompileInProgress,
  tryBeginRecompile,
  endRecompile,
} from './digest-sync.js';
import {
  detectAndResolveContradictions,
  runWeightTuningLoop,
  type ResolutionDecision as ContradictionDecision,
} from './contradiction-sync.js';
import { searchSubgraph, searchSubgraphBroadened, getSubgraphFactCount, fetchFactById } from './subgraph-search.js';
import {
  executePinOperation,
  validatePinArgs,
  type PinOpDeps,
} from './pin.js';
import { PluginHotCache, type HotFact } from './hot-cache-wrapper.js';
import { CONFIG, setRecoveryPhraseOverride, setChainIdOverride } from './config.js';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

// ---------------------------------------------------------------------------
// OpenClaw Plugin API type (defined locally to avoid SDK dependency)
// ---------------------------------------------------------------------------

interface OpenClawPluginApi {
  logger: {
    info(...args: unknown[]): void;
    warn(...args: unknown[]): void;
    error(...args: unknown[]): void;
  };
  config?: {
    agents?: {
      defaults?: {
        model?: {
          primary?: string;
        };
      };
    };
    models?: {
      providers?: Record<string, {
        baseUrl: string;
        apiKey?: string;
        api?: string;
        models?: Array<{ id: string; [k: string]: unknown }>;
        [k: string]: unknown;
      }>;
      [k: string]: unknown;
    };
    [key: string]: unknown;
  };
  pluginConfig?: Record<string, unknown>;
  registerTool(tool: unknown, opts?: { name?: string; names?: string[] }): void;
  registerService(service: { id: string; start(): void; stop?(): void }): void;
  on(hookName: string, handler: (...args: unknown[]) => unknown, opts?: { priority?: number }): void;
}

// ---------------------------------------------------------------------------
// Human-friendly error messages
// ---------------------------------------------------------------------------

/**
 * Translate technical error messages from the on-chain submission pipeline
 * into user-friendly messages. The original technical details are still
 * logged via api.logger — this only affects what the agent sees.
 */
function humanizeError(rawMessage: string): string {
  if (rawMessage.includes('AA23')) {
    return 'Memory storage temporarily unavailable. Will retry next time.';
  }
  if (rawMessage.includes('AA10')) {
    return 'Please wait a moment before storing more memories.';
  }
  if (rawMessage.includes('AA25')) {
    return 'Memory storage busy. Will retry.';
  }
  if (rawMessage.includes('pm_sponsorUserOperation')) {
    return 'Memory storage service temporarily unavailable.';
  }
  if (/Relay returned HTTP\s*404/.test(rawMessage)) {
    return 'Memory service is temporarily offline.';
  }
  if (/Relay returned HTTP\s*5\d\d/.test(rawMessage)) {
    return 'Memory service encountered a temporary error. Will retry next time.';
  }
  // Pass through non-technical messages as-is.
  return rawMessage;
}

// ---------------------------------------------------------------------------
// Persistent credential storage
// ---------------------------------------------------------------------------

/** Path where we persist userId + salt across restarts. */
const CREDENTIALS_PATH = CONFIG.credentialsPath;

// ---------------------------------------------------------------------------
// Cosine similarity threshold — skip injection when top result is below this
// ---------------------------------------------------------------------------

/**
 * Minimum cosine similarity of the top reranked result required to inject
 * memories into context. Below this threshold, the query is considered
 * irrelevant to any stored memories and results are suppressed.
 *
 * Default 0.15 is tuned for local ONNX models which produce lower
 * similarity scores than OpenAI models. Configurable via env var.
 */
const COSINE_THRESHOLD = CONFIG.cosineThreshold;

// ---------------------------------------------------------------------------
// Module-level state (persists across tool calls within a session)
// ---------------------------------------------------------------------------

let authKeyHex: string | null = null;
let encryptionKey: Buffer | null = null;
let dedupKey: Buffer | null = null;
let userId: string | null = null;
let subgraphOwner: string | null = null; // Smart Account address for subgraph queries
let apiClient: ReturnType<typeof createApiClient> | null = null;
let initPromise: Promise<void> | null = null;

// LSH hasher — lazily initialized on first use (needs credentials + embedding dims)
let lshHasher: LSHHasher | null = null;
let lshInitFailed = false; // If true, skip LSH on future calls (provider doesn't support embeddings)

// Hot cache for managed service (subgraph mode) — lazily initialized
let pluginHotCache: PluginHotCache | null = null;

// Two-tier search state (C1): skip redundant searches when query is semantically similar
let lastSearchTimestamp = 0;
let lastQueryEmbedding: number[] | null = null;

// Feature flags — configurable for A/B testing
const CACHE_TTL_MS = CONFIG.cacheTtlMs;
const SEMANTIC_SKIP_THRESHOLD = CONFIG.semanticSkipThreshold;

// Auto-extract throttle (C3): only extract every N turns in agent_end hook
let turnsSinceLastExtraction = 0;

// BUG-2 fix: Skip agent_end extraction during import operations.
// Import failures previously triggered agent_end → re-extraction → re-import loops.
let _importInProgress = false;
const AUTO_EXTRACT_EVERY_TURNS_ENV = CONFIG.extractInterval;

// Hard cap on facts per extraction to prevent LLM over-extraction from dense conversations
const MAX_FACTS_PER_EXTRACTION = 15;

// Store-time near-duplicate detection is always ON in v1.
// The TOTALRECLAW_STORE_DEDUP env var was removed.
const STORE_DEDUP_ENABLED = true;

// One-time welcome-back message for returning Pro users (set during init, consumed by first before_agent_start)
let welcomeBackMessage: string | null = null;

// B2: COSINE_THRESHOLD (above) is the single relevance gate for both
// the before_agent_start hook and the recall tool.  The former "RELEVANCE_THRESHOLD"
// (0.3) was too aggressive and silently suppressed auto-recall at session start.

// ---------------------------------------------------------------------------
// Billing cache infrastructure
// ---------------------------------------------------------------------------

const BILLING_CACHE_PATH = CONFIG.billingCachePath;
const BILLING_CACHE_TTL = 2 * 60 * 60 * 1000; // 2 hours
const QUOTA_WARNING_THRESHOLD = 0.8; // 80%

interface BillingCache {
  tier: string;
  free_writes_used: number;
  free_writes_limit: number;
  features?: {
    llm_dedup?: boolean;
    custom_extract_interval?: boolean;
    min_extract_interval?: number;
    extraction_interval?: number;
    max_facts_per_extraction?: number;
    max_candidate_pool?: number;
  };
  checked_at: number;
}

/**
 * Apply the billing tier to the runtime chain override.
 *
 * Pro tier → chain 100 (Gnosis mainnet). Free tier (or unknown) stays on
 * 84532 (Base Sepolia). The relay routes Pro UserOps to Gnosis, so the
 * client MUST sign them against chain 100 — otherwise the bundler returns
 * AA23 (invalid signature). See MCP's equivalent path in mcp/src/index.ts.
 *
 * Called from `readBillingCache` and `writeBillingCache` so that every cache
 * read or write keeps the chain override in sync with the cached tier.
 * Idempotent — calling with the same tier is a no-op.
 */
function syncChainIdFromTier(tier: string | undefined): void {
  if (tier === 'pro') {
    setChainIdOverride(100);
  } else {
    // Free or unknown → reset to the default free-tier chain.
    setChainIdOverride(84532);
  }
}

function readBillingCache(): BillingCache | null {
  try {
    if (!fs.existsSync(BILLING_CACHE_PATH)) return null;
    const raw = JSON.parse(fs.readFileSync(BILLING_CACHE_PATH, 'utf-8')) as BillingCache;
    if (!raw.checked_at || Date.now() - raw.checked_at > BILLING_CACHE_TTL) return null;
    // Keep chain override in sync with persisted tier across process restarts.
    syncChainIdFromTier(raw.tier);
    return raw;
  } catch {
    return null;
  }
}

function writeBillingCache(cache: BillingCache): void {
  try {
    const dir = path.dirname(BILLING_CACHE_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(BILLING_CACHE_PATH, JSON.stringify(cache));
  } catch {
    // Best-effort — don't block on cache write failure.
  }
  // Sync chain override AFTER the write so in-process UserOp signing picks
  // up the correct chain immediately, even if the disk write failed.
  syncChainIdFromTier(cache.tier);
}

/**
 * Check if LLM-guided dedup is enabled.
 *
 * Always returns true — LLM extraction runs client-side using the user's
 * own API key, so there is no cost to us. The server flag is respected as
 * a kill-switch but defaults to true for all tiers.
 */
function isLlmDedupEnabled(): boolean {
  const cache = readBillingCache();
  if (cache?.features?.llm_dedup === false) return false; // Server kill-switch
  return true;
}

/**
 * Get the effective extraction interval.
 * Server-side config takes priority (from billing cache), then env var fallback.
 * This allows the relay admin to tune extraction without an npm publish.
 */
function getExtractInterval(): number {
  const cache = readBillingCache();
  if (cache?.features?.extraction_interval != null) return cache.features.extraction_interval;
  return AUTO_EXTRACT_EVERY_TURNS_ENV;
}

/**
 * Get the max facts per extraction cycle.
 * Server-side config takes priority (from billing cache), then env var / constant fallback.
 */
function getMaxFactsPerExtraction(): number {
  const cache = readBillingCache();
  if (cache?.features?.max_facts_per_extraction != null) return cache.features.max_facts_per_extraction;
  return MAX_FACTS_PER_EXTRACTION;
}

/**
 * Ensure MEMORY.md has a TotalReclaw header so the agent knows encrypted
 * memories are injected automatically via the before_agent_start hook.
 *
 * Option 3 approach: don't delete or stub MEMORY.md — let the agent use it
 * for workspace-level notes, but make clear that user facts/preferences are
 * handled by TotalReclaw's E2EE pipeline. This avoids confusing the agent
 * (OpenClaw's system prompt still tells it to search MEMORY.md) while
 * guiding it away from writing sensitive data in cleartext.
 */
const MEMORY_HEADER = `# Memory

> **TotalReclaw is active.** Your encrypted memories are loaded automatically
> at the start of each conversation — no need to search this file for them.
> Use \`totalreclaw_remember\` to store new memories and \`totalreclaw_recall\`
> to search. Do NOT write user facts, preferences, or decisions to this file.
> This file is for workspace-level notes only (non-sensitive).

`;

function ensureMemoryHeader(logger: OpenClawPluginApi['logger']): void {
  try {
    const workspace = CONFIG.openclawWorkspace;
    const memoryMd = path.join(workspace, 'MEMORY.md');

    if (fs.existsSync(memoryMd)) {
      const content = fs.readFileSync(memoryMd, 'utf-8');
      if (!content.includes('TotalReclaw is active')) {
        fs.writeFileSync(memoryMd, MEMORY_HEADER + content);
        logger.info('Added TotalReclaw header to MEMORY.md');
      }
    } else {
      // Create MEMORY.md with the header so the agent doesn't get ENOENT
      const dir = path.dirname(memoryMd);
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(memoryMd, MEMORY_HEADER);
      logger.info('Created MEMORY.md with TotalReclaw header');
    }
  } catch {
    // Best-effort — don't block the hook
  }
}

// ---------------------------------------------------------------------------
// Dynamic candidate pool sizing
// ---------------------------------------------------------------------------

/** Cached fact count for dynamic candidate pool sizing. */
let cachedFactCount: number | null = null;
/** Timestamp of last fact count fetch (ms). */
let lastFactCountFetch: number = 0;
/** Cache TTL for fact count: 5 minutes. */
const FACT_COUNT_CACHE_TTL = 5 * 60 * 1000;

/**
 * Compute the candidate pool size from a fact count.
 *
 * Server-side config takes priority (from billing cache), then local fallback.
 * The server computes the optimal pool based on vault size and tier caps.
 *
 * Local fallback formula: pool = min(max(factCount * 3, 400), 5000)
 *   - At least 400 candidates (even for tiny vaults)
 *   - At most 5000 candidates (to bound decryption + reranking cost)
 *   - 3x fact count in between
 */
function computeCandidatePool(factCount: number): number {
  const cache = readBillingCache();
  if (cache?.features?.max_candidate_pool != null) return cache.features.max_candidate_pool;
  // Fallback to local formula if no server config
  return Math.min(Math.max(factCount * 3, 400), 5000);
}

/**
 * Fetch the user's fact count from the server, with caching.
 *
 * Uses the /v1/export endpoint with limit=1 to get `total_count` without
 * downloading all facts. Falls back to 400 (which gives pool=1200) if
 * the server is unreachable or returns no count.
 */
async function getFactCount(logger: OpenClawPluginApi['logger']): Promise<number> {
  const now = Date.now();

  // Return cached value if fresh.
  if (cachedFactCount !== null && (now - lastFactCountFetch) < FACT_COUNT_CACHE_TTL) {
    return cachedFactCount;
  }

  try {
    if (!apiClient || !authKeyHex) {
      return cachedFactCount ?? 400; // Not initialized yet, use default
    }

    const page = await apiClient.exportFacts(authKeyHex, 1);
    const count = page.total_count ?? page.facts.length;

    cachedFactCount = count;
    lastFactCountFetch = now;
    logger.info(`Fact count updated: ${count} (candidate pool: ${computeCandidatePool(count)})`);
    return count;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Failed to fetch fact count (using ${cachedFactCount ?? 400}): ${msg}`);
    return cachedFactCount ?? 400; // Fall back to cached or default
  }
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

/** True when recovery phrase is missing — tools return setup instructions. */
let needsSetup = false;

/** True on first before_agent_start after successful init — show welcome message once. */
let firstRunAfterInit = true;

/**
 * Derive keys from the recovery phrase, load or create credentials, and
 * register with the server if this is the first run.
 */
async function initialize(logger: OpenClawPluginApi['logger']): Promise<void> {
  const serverUrl = CONFIG.serverUrl || 'https://api.totalreclaw.xyz';
  const masterPassword = CONFIG.recoveryPhrase;

  if (!masterPassword) {
    needsSetup = true;
    logger.info('TOTALRECLAW_RECOVERY_PHRASE not set — setup required (see SKILL.md Post-Install Setup)');
    return;
  }

  apiClient = createApiClient(serverUrl);

  // --- Attempt to load existing credentials ---
  let existingSalt: Buffer | undefined;
  let existingUserId: string | undefined;

  try {
    if (fs.existsSync(CREDENTIALS_PATH)) {
      const creds = JSON.parse(fs.readFileSync(CREDENTIALS_PATH, 'utf8'));
      // Salt may be stored as base64 (plugin-written) or hex (MCP setup-written).
      // Detect format: hex strings are 64 chars of [0-9a-f], base64 uses [A-Z+/=].
      const saltStr: string = creds.salt;
      if (saltStr && /^[0-9a-f]{64}$/i.test(saltStr)) {
        existingSalt = Buffer.from(saltStr, 'hex');
      } else if (saltStr) {
        existingSalt = Buffer.from(saltStr, 'base64');
      }
      existingUserId = creds.userId;
      logger.info(`Loaded existing credentials for user ${existingUserId}`);
    }
  } catch (e) {
    logger.warn('Failed to load credentials, will register new account');
  }

  // --- Derive keys ---
  const keys = deriveKeys(masterPassword, existingSalt);
  authKeyHex = keys.authKey.toString('hex');
  encryptionKey = keys.encryptionKey;
  dedupKey = keys.dedupKey;

  // Cache credentials for lazy LSH seed derivation
  masterPasswordCache = masterPassword;
  saltCache = keys.salt;

  if (existingUserId) {
    userId = existingUserId;
    logger.info(`Authenticated as user ${userId}`);

    // Idempotent registration — ensure auth key is registered with the relay.
    // Without this, returning users get 401 if the relay database was reset or
    // if credentials were created by the MCP setup CLI (different process).
    try {
      const authHash = computeAuthKeyHash(keys.authKey);
      const saltHex = keys.salt.toString('hex');
      await apiClient.register(authHash, saltHex);
    } catch {
      // Best-effort — relay returns 200 for already-registered users.
      // Only fails on network errors; bearer token auth still works if
      // a prior registration succeeded.
      logger.warn('Idempotent relay registration failed (best-effort, will retry on next start)');
    }
  } else {
    // First run -- register with the server.
    const authHash = computeAuthKeyHash(keys.authKey);
    const saltHex = keys.salt.toString('hex');

    let registeredUserId: string | undefined;
    try {
      const result = await apiClient.register(authHash, saltHex);
      registeredUserId = result.user_id;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes('USER_EXISTS') && isSubgraphMode()) {
        // In managed mode, derive a deterministic userId from the auth key
        // hash. The server is only a relay proxy — userId is used as the
        // subgraph owner field and must be consistent between store/search.
        registeredUserId = authHash.slice(0, 32);
        logger.info(`Using derived userId for managed mode (server returned USER_EXISTS)`);
      } else {
        throw err;
      }
    }

    userId = registeredUserId!;

    // Persist credentials so we can resume later.
    // Include the mnemonic so hot-reload works without env var.
    const dir = path.dirname(CREDENTIALS_PATH);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    const credsToSave: Record<string, string> = {
      userId,
      salt: keys.salt.toString('base64'),
    };
    // Only persist mnemonic if we have one (avoid writing empty string).
    if (masterPassword) {
      credsToSave.mnemonic = masterPassword;
    }
    fs.writeFileSync(CREDENTIALS_PATH, JSON.stringify(credsToSave), { mode: 0o600 });

    logger.info(`Registered new user: ${userId}`);
  }

  // Derive Smart Account address for subgraph queries (on-chain owner identity).
  if (isSubgraphMode()) {
    try {
      const config = getSubgraphConfig();
      subgraphOwner = await deriveSmartAccountAddress(config.mnemonic, config.chainId);
      logger.info(`Subgraph owner (Smart Account): ${subgraphOwner}`);
    } catch (err) {
      logger.warn(`Failed to derive Smart Account address: ${err instanceof Error ? err.message : String(err)}`);
      // Fall back to userId — won't match subgraph Bytes format, but better than null
      subgraphOwner = userId;
    }
  }

  // One-time billing check for returning users (imported recovery phrase).
  // If they already have an active Pro subscription, inform them on next conversation start.
  if (existingUserId && authKeyHex) {
    try {
      const walletAddr = subgraphOwner || userId || '';
      if (walletAddr) {
        const billingUrl = CONFIG.serverUrl;
        const resp = await fetch(`${billingUrl}/v1/billing/status?wallet_address=${encodeURIComponent(walletAddr)}`, {
          method: 'GET',
          headers: {
            'Authorization': `Bearer ${authKeyHex}`,
            'Accept': 'application/json',
            'X-TotalReclaw-Client': 'openclaw-plugin',
          },
        });
        if (resp.ok) {
          const billingData = await resp.json() as Record<string, unknown>;
          const tier = billingData.tier as string;
          const expiresAt = billingData.expires_at as string | undefined;
          // Populate billing cache for future use.
          writeBillingCache({
            tier: tier || 'free',
            free_writes_used: (billingData.free_writes_used as number) ?? 0,
            free_writes_limit: (billingData.free_writes_limit as number) ?? 0,
            features: billingData.features as BillingCache['features'] | undefined,
            checked_at: Date.now(),
          });
          if (tier === 'pro' && expiresAt) {
            const expiryDate = new Date(expiresAt).toLocaleDateString();
            welcomeBackMessage = `Welcome back! Your Pro subscription is active (expires: ${expiryDate}).`;
            logger.info(`Returning Pro user detected — expires ${expiryDate}`);
          }
        }
      }
    } catch {
      // Best-effort — don't block initialization on billing check failure.
    }
  }
}

function isDocker(): boolean {
  try {
    return fs.existsSync('/.dockerenv') ||
      (fs.existsSync('/proc/1/cgroup') &&
        fs.readFileSync('/proc/1/cgroup', 'utf8').includes('docker'));
  } catch { return false; }
}

function buildSetupErrorMsg(): string {
  return 'TotalReclaw setup required. Use the `totalreclaw_setup` tool with a 12-word BIP-39 recovery phrase.\n\n' +
    '1. Ask the user if they have an existing recovery phrase, or generate a new one with `npx @totalreclaw/mcp-server setup`.\n' +
    '2. Call `totalreclaw_setup` with the phrase — no gateway restart needed.\n' +
    '   (Optional: set TOTALRECLAW_SELF_HOSTED=true if using your own server instead of the managed service.)';
}

function buildSetupErrorMsgLegacy(): string {
  const base =
    'TotalReclaw setup required:\n' +
    '1. Set TOTALRECLAW_RECOVERY_PHRASE — ask the user if they have an existing recovery phrase or generate a new 12-word BIP-39 mnemonic.\n' +
    '2. Restart the gateway to apply changes.\n' +
    '   (Optional: set TOTALRECLAW_SELF_HOSTED=true if using your own server instead of the managed service.)\n\n';

  if (isDocker()) {
    return base +
      'Running in Docker — pass env vars via `-e` flags or your compose file:\n' +
      '  -e TOTALRECLAW_RECOVERY_PHRASE="word1 word2 ..."';
  }

  if (process.platform === 'darwin') {
    return base +
      'Running on macOS — add env vars to the LaunchAgent plist at\n' +
      '~/Library/LaunchAgents/ai.openclaw.gateway.plist under <key>EnvironmentVariables</key>:\n' +
      '  <key>TOTALRECLAW_RECOVERY_PHRASE</key><string>word1 word2 ...</string>\n' +
      'Then run: openclaw gateway restart';
  }

  return base +
    'Running on Linux — add env vars to the systemd unit override or your shell profile:\n' +
    '  export TOTALRECLAW_RECOVERY_PHRASE="word1 word2 ..."\n' +
    'Then run: openclaw gateway restart';
}

const SETUP_ERROR_MSG = buildSetupErrorMsg();

/**
 * Ensure `initialize()` has completed (runs at most once).
 *
 * If `needsSetup` is true after init, attempts a hot-reload from
 * credentials.json in case the mnemonic was written there by a
 * `totalreclaw_setup` tool call or `npx @totalreclaw/mcp-server setup`.
 */
async function ensureInitialized(logger: OpenClawPluginApi['logger']): Promise<void> {
  if (!initPromise) {
    initPromise = initialize(logger);
  }
  await initPromise;

  // Hot-reload: if setup is still needed, check if credentials.json
  // now has a mnemonic (written by totalreclaw_setup or MCP setup CLI).
  if (needsSetup) {
    await attemptHotReload(logger);
  }
}

/**
 * Attempt to hot-reload credentials from credentials.json.
 *
 * Called when `needsSetup` is true — checks if credentials.json contains
 * a mnemonic (written by the `totalreclaw_setup` tool or MCP setup CLI).
 * If found, re-derives keys and completes initialization without requiring
 * a gateway restart.
 */
async function attemptHotReload(logger: OpenClawPluginApi['logger']): Promise<void> {
  try {
    if (!fs.existsSync(CREDENTIALS_PATH)) return;

    const creds = JSON.parse(fs.readFileSync(CREDENTIALS_PATH, 'utf8'));
    if (!creds.mnemonic) return;

    logger.info('Hot-reloading credentials from credentials.json (no restart needed)');

    // Set the runtime override so CONFIG.recoveryPhrase returns the mnemonic.
    setRecoveryPhraseOverride(creds.mnemonic);

    // Re-run initialization with the newly available mnemonic.
    needsSetup = false;
    initPromise = initialize(logger);
    await initPromise;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Hot-reload from credentials.json failed: ${msg}`);
    // Leave needsSetup as true — user will see the setup prompt.
  }
}

/**
 * Force re-initialization with a specific mnemonic.
 *
 * Called by the `totalreclaw_setup` tool. Clears stale credentials from
 * disk so that `initialize()` treats this as a fresh registration and
 * persists the NEW mnemonic + freshly derived salt/userId.
 *
 * Without clearing credentials.json first, `initialize()` would load the
 * OLD salt and userId, derive keys from (new mnemonic + old salt), skip
 * writing credentials (because existingUserId is set), and the new
 * mnemonic would never be persisted — a critical data-loss bug.
 */
async function forceReinitialization(mnemonic: string, logger: OpenClawPluginApi['logger']): Promise<void> {
  // Set the runtime override so CONFIG.recoveryPhrase returns this mnemonic.
  setRecoveryPhraseOverride(mnemonic);

  // CRITICAL: Remove stale credentials so initialize() does a fresh
  // registration with a new salt. If we leave the old file, initialize()
  // loads the old salt + userId and never writes the new mnemonic.
  try {
    if (fs.existsSync(CREDENTIALS_PATH)) {
      fs.unlinkSync(CREDENTIALS_PATH);
      logger.info('Cleared stale credentials.json for fresh setup');
    }
  } catch (err) {
    logger.warn(`Could not remove old credentials.json: ${err instanceof Error ? err.message : String(err)}`);
  }

  // Reset module state for a clean re-init.
  needsSetup = false;
  authKeyHex = null;
  encryptionKey = null;
  dedupKey = null;
  userId = null;
  subgraphOwner = null;
  apiClient = null;
  lshHasher = null;
  lshInitFailed = false;
  masterPasswordCache = null;
  saltCache = null;
  pluginHotCache = null;
  firstRunAfterInit = true;

  // Re-run initialization — will register fresh and persist new credentials.
  initPromise = initialize(logger);
  await initPromise;
}

/**
 * Like ensureInitialized, but throws if setup is still needed.
 * Use in tool handlers where we need a fully configured plugin.
 */
async function requireFullSetup(logger: OpenClawPluginApi['logger']): Promise<void> {
  await ensureInitialized(logger);
  if (needsSetup) {
    throw new Error(SETUP_ERROR_MSG);
  }
}

// ---------------------------------------------------------------------------
// LSH + Embedding helpers
// ---------------------------------------------------------------------------

/** Recovery phrase cached for LSH seed derivation (set during initialize()). */
let masterPasswordCache: string | null = null;
/** Salt cached for LSH seed derivation (set during initialize()). */
let saltCache: Buffer | null = null;

/**
 * Get or initialize the LSH hasher.
 *
 * The hasher is created lazily because it needs:
 *   1. The recovery phrase + salt (available after initialize())
 *   2. The embedding dimensions (available after initLLMClient())
 *
 * If the provider doesn't support embeddings, this returns null and
 * sets `lshInitFailed` to avoid retrying.
 */
function getLSHHasher(logger: OpenClawPluginApi['logger']): LSHHasher | null {
  if (lshHasher) return lshHasher;
  if (lshInitFailed) return null;

  try {
    if (!masterPasswordCache || !saltCache) {
      logger.warn('LSH hasher: credentials not available yet');
      return null;
    }

    const dims = getEmbeddingDims();
    const lshSeed = deriveLshSeed(masterPasswordCache, saltCache);
    lshHasher = new LSHHasher(lshSeed, dims);
    logger.info(`LSH hasher initialized (dims=${dims}, tables=${lshHasher.tables}, bits=${lshHasher.bits})`);
    return lshHasher;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`LSH hasher initialization failed (will use word-only indices): ${msg}`);
    lshInitFailed = true;
    return null;
  }
}

/**
 * Generate an embedding for the given text and compute LSH bucket hashes.
 *
 * Returns null if embedding generation fails (provider doesn't support it,
 * network error, etc.). In that case, the caller should fall back to
 * word-only blind indices.
 */
async function generateEmbeddingAndLSH(
  text: string,
  logger: OpenClawPluginApi['logger'],
): Promise<{ embedding: number[]; lshBuckets: string[]; encryptedEmbedding: string } | null> {
  try {
    const embedding = await generateEmbedding(text);

    const hasher = getLSHHasher(logger);
    const lshBuckets = hasher ? hasher.hash(embedding) : [];

    // Encrypt the embedding (JSON array of numbers) for server-blind storage
    const encryptedEmbedding = encryptToHex(JSON.stringify(embedding), encryptionKey!);

    return { embedding, lshBuckets, encryptedEmbedding };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Embedding/LSH generation failed (falling back to word-only indices): ${msg}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Store-time near-duplicate search helper
// ---------------------------------------------------------------------------

/**
 * Search the vault for near-duplicates of a fact about to be stored.
 *
 * Uses the fact's blind indices as trapdoors to fetch candidates, decrypts
 * them, extracts embeddings, and calls `findNearDuplicate()` from the
 * consolidation module.
 *
 * Returns null on any failure (fail-open: we'd rather store a duplicate than
 * lose a fact).
 */
async function searchForNearDuplicates(
  factText: string,
  factEmbedding: number[],
  allIndices: string[],
  logger: OpenClawPluginApi['logger'],
): Promise<{ match: DecryptedCandidate; similarity: number } | null> {
  try {
    if (!encryptionKey || !authKeyHex || !userId) return null;

    // Fetch candidates from the vault using the fact's blind indices as trapdoors.
    let decryptedCandidates: DecryptedCandidate[] = [];

    if (isSubgraphMode()) {
      const results = await searchSubgraph(
        subgraphOwner || userId,
        allIndices,
        STORE_DEDUP_MAX_CANDIDATES,
        authKeyHex,
      );
      for (const result of results) {
        try {
          const docJson = decryptFromHex(result.encryptedBlob, encryptionKey);
          if (isDigestBlob(docJson)) continue;
          const doc = readClaimFromBlob(docJson);

          let embedding: number[] | null = null;
          if (result.encryptedEmbedding) {
            try {
              embedding = JSON.parse(decryptFromHex(result.encryptedEmbedding, encryptionKey));
            } catch { /* skip */ }
          }

          decryptedCandidates.push({
            id: result.id,
            text: doc.text,
            embedding,
            importance: doc.importance,
            decayScore: 5,
            createdAt: result.timestamp ? parseInt(result.timestamp, 10) * 1000 : Date.now(),
            version: 1,
          });
        } catch { /* skip undecryptable */ }
      }
    } else if (apiClient) {
      const candidates = await apiClient.search(
        userId,
        allIndices,
        STORE_DEDUP_MAX_CANDIDATES,
        authKeyHex,
      );
      for (const candidate of candidates) {
        try {
          const docJson = decryptFromHex(candidate.encrypted_blob, encryptionKey);
          if (isDigestBlob(docJson)) continue;
          const doc = readClaimFromBlob(docJson);

          let embedding: number[] | null = null;
          if (candidate.encrypted_embedding) {
            try {
              embedding = JSON.parse(decryptFromHex(candidate.encrypted_embedding, encryptionKey));
            } catch { /* skip */ }
          }

          decryptedCandidates.push({
            id: candidate.fact_id,
            text: doc.text,
            embedding,
            importance: doc.importance,
            decayScore: candidate.decay_score,
            createdAt: typeof candidate.timestamp === 'number'
              ? candidate.timestamp
              : new Date(candidate.timestamp).getTime(),
            version: candidate.version,
          });
        } catch { /* skip undecryptable */ }
      }
    }

    if (decryptedCandidates.length === 0) return null;

    const result = findNearDuplicate(factEmbedding, decryptedCandidates, getStoreDedupThreshold());
    if (!result) return null;

    return { match: result.existingFact, similarity: result.similarity };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Store-time dedup search failed (proceeding with store): ${msg}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

/**
 * Encrypt a plaintext document string and return its hex-encoded ciphertext.
 *
 * The server stores blobs as hex (not base64), so we convert the base64
 * output of `encrypt()` into hex.
 */
function encryptToHex(plaintext: string, key: Buffer): string {
  const b64 = encrypt(plaintext, key);
  return Buffer.from(b64, 'base64').toString('hex');
}

// Plugin v3.0.0 removed the legacy claim-format fallback. Write path
// always emits Memory Taxonomy v1 JSON blobs. The logClaimFormatOnce
// helper is gone along with TOTALRECLAW_CLAIM_FORMAT / TOTALRECLAW_TAXONOMY_VERSION.

let _loggedDigestMode = false;
function logDigestModeOnce(mode: DigestMode, logger: OpenClawPluginApi['logger']): void {
  if (_loggedDigestMode) return;
  _loggedDigestMode = true;
  logger.info(`TotalReclaw: digest injection mode = ${mode}`);
}

/**
 * How many active facts to pull into a digest recompilation.
 * Digest compiler itself will apply DIGEST_CLAIM_CAP for the LLM path.
 */
const DIGEST_FETCH_LIMIT = 500;

/**
 * Schedule a background digest recompile. Fire-and-forget.
 *
 * The caller must check `!isRecompileInProgress()` before invoking.
 * Errors are logged and swallowed; the guard flag is always released.
 */
function scheduleDigestRecompile(
  previousClaimId: string | null,
  logger: OpenClawPluginApi['logger'],
): void {
  if (!isRecompileInProgress()) {
    if (!tryBeginRecompile()) return;
  } else {
    return;
  }

  const mode = resolveDigestMode();
  const owner = subgraphOwner || userId;
  const authKey = authKeyHex;
  const encKey = encryptionKey;
  const ownerForBatch = subgraphOwner ?? undefined;

  if (!owner || !authKey || !encKey) {
    endRecompile();
    return;
  }

  // Capture llmFn from the current LLM config (cheap variant of the user's
  // provider, already resolved by resolveLLMConfig).
  const llmConfig = resolveLLMConfig();
  const llmFn = llmConfig
    ? async (prompt: string): Promise<string> => {
        const out = await chatCompletion(
          llmConfig,
          [
            { role: 'system', content: 'You return only valid JSON. No markdown fences, no commentary.' },
            { role: 'user', content: prompt },
          ],
          { maxTokens: 800, temperature: 0 },
        );
        return out ?? '';
      }
    : null;

  // Build the I/O deps closures. We capture the owner/auth/key values so the
  // background task doesn't race with module-level state resets.
  const fetchFn = () =>
    fetchAllActiveClaims(
      owner,
      authKey,
      encKey,
      DIGEST_FETCH_LIMIT,
      {
        searchSubgraphBroadened: async (o, n, a) => searchSubgraphBroadened(o, n, a),
        decryptFromHex: (hex, key) => decryptFromHex(hex, key),
      },
      logger,
    );

  const storeFn = async (canonicalClaimJson: string, compiledAt: string): Promise<void> => {
    if (!isSubgraphMode()) {
      // Self-hosted mode — store via the REST API.
      if (!apiClient) throw new Error('apiClient not initialized');
      const encryptedBlob = encryptToHex(canonicalClaimJson, encKey);
      const contentFp = generateContentFingerprint(canonicalClaimJson, dedupKey!);
      const payload: StoreFactPayload = {
        id: crypto.randomUUID(),
        timestamp: compiledAt,
        encrypted_blob: encryptedBlob,
        blind_indices: [DIGEST_TRAPDOOR],
        decay_score: 10,
        source: 'openclaw-plugin-digest',
        content_fp: contentFp,
        agent_id: 'openclaw-plugin-digest',
      };
      await apiClient.store(userId!, [payload], authKey);
      return;
    }

    // Subgraph / managed-service mode — encrypt, encode, submit as a single-fact UserOp.
    const encryptedBlob = encryptToHex(canonicalClaimJson, encKey);
    const contentFp = generateContentFingerprint(canonicalClaimJson, dedupKey!);
    const protobuf = encodeFactProtobuf({
      id: crypto.randomUUID(),
      timestamp: compiledAt,
      owner,
      encryptedBlob,
      blindIndices: [DIGEST_TRAPDOOR],
      decayScore: 10,
      source: 'openclaw-plugin-digest',
      contentFp,
      agentId: 'openclaw-plugin-digest',
      version: PROTOBUF_VERSION_V4,
    });
    const config = { ...getSubgraphConfig(), authKeyHex: authKey, walletAddress: ownerForBatch };
    const result = await submitFactBatchOnChain([protobuf], config);
    if (!result.success) {
      throw new Error('Digest store UserOp did not succeed on-chain');
    }
  };

  const tombstoneFn = async (claimId: string): Promise<void> => {
    if (!isSubgraphMode()) {
      if (apiClient) {
        try { await apiClient.deleteFact(claimId, authKey); } catch { /* best-effort */ }
      }
      return;
    }
    const tombstone: FactPayload = {
      id: claimId,
      timestamp: new Date().toISOString(),
      owner,
      encryptedBlob: '00',
      blindIndices: [],
      decayScore: 0,
      source: 'tombstone',
      contentFp: '',
      agentId: 'openclaw-plugin-digest',
      version: PROTOBUF_VERSION_V4,
    };
    const protobuf = encodeFactProtobuf(tombstone);
    const config = { ...getSubgraphConfig(), authKeyHex: authKey, walletAddress: ownerForBatch };
    const result = await submitFactBatchOnChain([protobuf], config);
    if (!result.success) {
      throw new Error('Digest tombstone UserOp did not succeed on-chain');
    }
  };

  // Slice 2f: run the weight-tuning loop as a fire-and-forget pre-compile step.
  // This consumes any feedback.jsonl entries written since the last compile
  // and nudges ~/.totalreclaw/weights.json, so the NEXT contradiction detection
  // uses the adjusted weights. Rate-limited and idempotent — see
  // runWeightTuningLoop for details. Failures are logged, never fatal.
  void runWeightTuningLoop(Math.floor(Date.now() / 1000), logger).catch((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Digest: tuning loop threw: ${msg}`);
  });

  void recompileDigest({
    mode,
    previousClaimId,
    nowUnixSeconds: Math.floor(Date.now() / 1000),
    deps: {
      storeDigestClaim: storeFn,
      tombstoneDigest: tombstoneFn,
      fetchAllActiveClaimsFn: fetchFn,
      llmFn,
    },
    logger,
  })
    .catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Digest: background recompile threw: ${msg}`);
    })
    .finally(() => {
      endRecompile();
    });
}

/**
 * Decrypt a hex-encoded ciphertext blob into a UTF-8 string.
 */
function decryptFromHex(hexBlob: string, key: Buffer): string {
  const hex = hexBlob.startsWith('0x') ? hexBlob.slice(2) : hexBlob;
  const b64 = Buffer.from(hex, 'hex').toString('base64');
  return decrypt(b64, key);
}

// ---------------------------------------------------------------------------
// Migration GraphQL helpers
// ---------------------------------------------------------------------------

interface MigrationFact {
  id: string;
  owner: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  isActive: boolean;
  contentFp: string;
  source: string;
  agentId: string;
  version: number;
  timestamp: string;
}

const MIGRATION_PAGE_SIZE = 1000;

/** Execute a GraphQL query against a subgraph endpoint. Returns null on error. */
async function migrationGqlQuery<T>(
  endpoint: string,
  query: string,
  variables: Record<string, unknown>,
  authKey?: string,
): Promise<T | null> {
  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-TotalReclaw-Client': 'openclaw-plugin',
    };
    if (authKey) headers['Authorization'] = `Bearer ${authKey}`;
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, variables }),
    });
    if (!response.ok) return null;
    const json = await response.json() as { data?: T; errors?: unknown[] };
    return json.data ?? null;
  } catch {
    return null;
  }
}

/** Fetch all active facts by owner from a subgraph, paginated. */
async function fetchAllFactsByOwner(
  subgraphUrl: string,
  owner: string,
  authKey: string,
): Promise<MigrationFact[]> {
  const allFacts: MigrationFact[] = [];
  let lastId = '';

  while (true) {
    const hasLastId = lastId !== '';
    const query = hasLastId
      ? `query($owner:Bytes!,$first:Int!,$lastId:String!){facts(where:{owner:$owner,isActive:true,id_gt:$lastId},first:$first,orderBy:id,orderDirection:asc){id owner encryptedBlob encryptedEmbedding decayScore isActive contentFp source agentId version timestamp}}`
      : `query($owner:Bytes!,$first:Int!){facts(where:{owner:$owner,isActive:true},first:$first,orderBy:id,orderDirection:asc){id owner encryptedBlob encryptedEmbedding decayScore isActive contentFp source agentId version timestamp}}`;
    const vars: Record<string, unknown> = hasLastId
      ? { owner, first: MIGRATION_PAGE_SIZE, lastId }
      : { owner, first: MIGRATION_PAGE_SIZE };

    const data = await migrationGqlQuery<{ facts?: MigrationFact[] }>(subgraphUrl, query, vars, authKey);
    const facts = data?.facts ?? [];
    if (facts.length === 0) break;
    allFacts.push(...facts);
    if (facts.length < MIGRATION_PAGE_SIZE) break;
    lastId = facts[facts.length - 1].id;
  }

  return allFacts;
}

/** Fetch content fingerprints from a subgraph for idempotency. */
async function fetchContentFingerprintsByOwner(
  subgraphUrl: string,
  owner: string,
  authKey: string,
): Promise<Set<string>> {
  const fps = new Set<string>();
  let lastId = '';

  while (true) {
    const hasLastId = lastId !== '';
    const query = hasLastId
      ? `query($owner:Bytes!,$first:Int!,$lastId:String!){facts(where:{owner:$owner,isActive:true,id_gt:$lastId},first:$first,orderBy:id,orderDirection:asc){id contentFp}}`
      : `query($owner:Bytes!,$first:Int!){facts(where:{owner:$owner,isActive:true},first:$first,orderBy:id,orderDirection:asc){id contentFp}}`;
    const vars: Record<string, unknown> = hasLastId
      ? { owner, first: MIGRATION_PAGE_SIZE, lastId }
      : { owner, first: MIGRATION_PAGE_SIZE };

    const data = await migrationGqlQuery<{ facts?: Array<{ id: string; contentFp: string }> }>(subgraphUrl, query, vars, authKey);
    const facts = data?.facts ?? [];
    if (facts.length === 0) break;
    for (const f of facts) {
      if (f.contentFp) fps.add(f.contentFp);
    }
    if (facts.length < MIGRATION_PAGE_SIZE) break;
    lastId = facts[facts.length - 1].id;
  }

  return fps;
}

/** Fetch blind index hashes for given fact IDs. */
async function fetchBlindIndicesByFactIds(
  subgraphUrl: string,
  factIds: string[],
  authKey: string,
): Promise<Map<string, string[]>> {
  const result = new Map<string, string[]>();
  const CHUNK = 50;

  for (let i = 0; i < factIds.length; i += CHUNK) {
    const chunk = factIds.slice(i, i + CHUNK);
    const query = `query($factIds:[String!]!,$first:Int!){blindIndexes(where:{fact_in:$factIds},first:$first){hash fact{id}}}`;
    const data = await migrationGqlQuery<{
      blindIndexes?: Array<{ hash: string; fact: { id: string } }>;
    }>(subgraphUrl, query, { factIds: chunk, first: 1000 }, authKey);

    for (const entry of data?.blindIndexes ?? []) {
      const existing = result.get(entry.fact.id) || [];
      existing.push(entry.hash);
      result.set(entry.fact.id, existing);
    }
  }

  return result;
}

/**
 * Fetch existing memories from the vault to provide dedup context for extraction.
 * Returns a lightweight list of {id, text} pairs for the LLM prompt.
 * Fails silently — returns empty array on any error.
 */
async function fetchExistingMemoriesForExtraction(
  logger: { warn: (msg: string) => void },
  limit: number = 30,
  rawMessages: unknown[] = [],
): Promise<Array<{ id: string; text: string }>> {
  try {
    if (!encryptionKey || !authKeyHex || !userId) return [];

    // Extract key terms from the last few messages to generate meaningful trapdoors.
    // Using '*' would produce zero trapdoors (stripped as punctuation), so we pull
    // text from the conversation to find memories relevant to the current context.
    const recentMessages = rawMessages.slice(-4);
    const textChunks: string[] = [];
    for (const msg of recentMessages) {
      const m = msg as { content?: string | Array<{ text?: string }>; text?: string };
      if (typeof m.content === 'string') {
        textChunks.push(m.content);
      } else if (Array.isArray(m.content)) {
        for (const block of m.content) {
          if (block.text) textChunks.push(block.text);
        }
      } else if (typeof m.text === 'string') {
        textChunks.push(m.text);
      }
    }
    const queryText = textChunks.join(' ').slice(0, 500); // cap to avoid giant trapdoor sets
    if (!queryText.trim()) return [];

    const trapdoors = generateBlindIndices(queryText);
    if (trapdoors.length === 0) return [];

    const results: Array<{ id: string; text: string }> = [];

    if (isSubgraphMode()) {
      const rawResults = await searchSubgraph(
        subgraphOwner || userId,
        trapdoors,
        limit,
        authKeyHex,
      );
      for (const r of rawResults) {
        try {
          const docJson = decryptFromHex(r.encryptedBlob, encryptionKey);
          if (isDigestBlob(docJson)) continue;
          const doc = readClaimFromBlob(docJson);
          results.push({ id: r.id, text: doc.text });
        } catch { /* skip undecryptable */ }
      }
    } else if (apiClient) {
      const candidates = await apiClient.search(userId, trapdoors, limit, authKeyHex);
      for (const c of candidates) {
        try {
          const docJson = decryptFromHex(c.encrypted_blob, encryptionKey);
          if (isDigestBlob(docJson)) continue;
          const doc = readClaimFromBlob(docJson);
          results.push({ id: c.fact_id, text: doc.text });
        } catch { /* skip undecryptable */ }
      }
    }

    return results;
  } catch (err) {
    logger.warn(`Failed to fetch existing memories for extraction context: ${err instanceof Error ? err.message : String(err)}`);
    return [];
  }
}

/**
 * Simple text-overlap scoring between a query and a candidate document.
 * Returns the number of overlapping lowercase words.
 */
function textScore(query: string, docText: string): number {
  const queryWords = new Set(
    query.toLowerCase().split(/\s+/).filter((w) => w.length >= 2),
  );
  const docWords = docText.toLowerCase().split(/\s+/);
  let score = 0;
  for (const word of docWords) {
    if (queryWords.has(word)) score++;
  }
  return score;
}

/**
 * Format a relative time string (e.g. "2 hours ago").
 */
function relativeTime(isoOrMs: string | number): string {
  const ms = typeof isoOrMs === 'number' ? isoOrMs : new Date(isoOrMs).getTime();
  const diffMs = Date.now() - ms;
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// ---------------------------------------------------------------------------
// Importance filter for auto-extraction
// ---------------------------------------------------------------------------

/**
 * Minimum importance score (1-10) for auto-extracted facts to be stored.
 * Facts below this threshold are silently dropped to save storage and gas.
 * Configurable via TOTALRECLAW_MIN_IMPORTANCE env var (default: 3).
 *
 * NOTE: This filter is ONLY applied to auto-extraction (hooks).
 * The explicit `totalreclaw_remember` tool always stores regardless of importance.
 */
const MIN_IMPORTANCE_THRESHOLD = CONFIG.minImportance;

/**
 * Filter extracted facts by importance threshold.
 * Facts with importance < MIN_IMPORTANCE_THRESHOLD are dropped.
 * Facts with missing/undefined importance are treated as importance=5 (kept).
 */
function filterByImportance(
  facts: ExtractedFact[],
  logger: OpenClawPluginApi['logger'],
): { kept: ExtractedFact[]; dropped: number } {
  const kept: ExtractedFact[] = [];
  let dropped = 0;

  for (const fact of facts) {
    const importance = fact.importance ?? 5;
    if (importance >= MIN_IMPORTANCE_THRESHOLD) {
      kept.push(fact);
    } else {
      dropped++;
    }
  }

  // Phase 2.2.5: always log the filter outcome so the agent_end path can
  // distinguish "LLM returned 0 facts" from "LLM returned N facts all dropped
  // below threshold" from "LLM returned N facts, all kept". Prior to 2.2.5
  // this only logged on drops, which made empty-input invisible.
  if (facts.length === 0) {
    logger.info('Importance filter: input=0 (nothing to filter)');
  } else if (dropped > 0) {
    logger.info(
      `Importance filter: dropped ${dropped}/${facts.length} facts below threshold ${MIN_IMPORTANCE_THRESHOLD}`,
    );
  } else {
    logger.info(
      `Importance filter: kept all ${facts.length} facts (threshold ${MIN_IMPORTANCE_THRESHOLD})`,
    );
  }

  return { kept, dropped };
}

// ---------------------------------------------------------------------------
// Auto-extraction helper
// ---------------------------------------------------------------------------

/**
 * Store extracted facts in the TotalReclaw server.
 * Encrypts each fact, generates blind indices and fingerprint, stores via API.
 * Silently skips duplicates.
 *
 * Before storing, performs semantic near-duplicate detection within the batch:
 * facts whose embeddings have cosine similarity >= threshold (default 0.9)
 * against an already-accepted fact in the same batch are skipped.
 */
async function storeExtractedFacts(
  facts: ExtractedFact[],
  logger: OpenClawPluginApi['logger'],
  sourceOverride?: string,
): Promise<number> {
  if (!encryptionKey || !dedupKey || !authKeyHex || !userId || !apiClient) return 0;

  // Phase 1: Generate embeddings for all facts (needed for dedup + storage).
  const embeddingMap = new Map<string, number[]>();
  const embeddingResultMap = new Map<
    string,
    { embedding: number[]; lshBuckets: string[]; encryptedEmbedding: string }
  >();

  for (const fact of facts) {
    try {
      const result = await generateEmbeddingAndLSH(fact.text, logger);
      if (result) {
        embeddingMap.set(fact.text, result.embedding);
        embeddingResultMap.set(fact.text, result);
      }
    } catch {
      // Embedding generation failed for this fact -- proceed without it.
    }
  }

  // Phase 2: Semantic batch dedup.
  const dedupedFacts = deduplicateBatch(facts, embeddingMap, logger);

  if (dedupedFacts.length < facts.length) {
    logger.info(
      `Semantic dedup: ${facts.length - dedupedFacts.length} near-duplicate(s) removed from batch of ${facts.length}`,
    );
  }

  // Phase 3: Store the deduplicated facts (with optional store-time dedup).
  // In subgraph mode, collect all protobuf payloads (tombstones + new facts)
  // and submit them in a single batched UserOp for gas efficiency.
  let stored = 0;
  let superseded = 0;
  let skipped = 0;
  let failedFacts = 0;
  const pendingPayloads: Buffer[] = []; // Batched subgraph payloads
  let preparedForSubgraph = 0;

  // Plugin v3.0.0: always emit Memory Taxonomy v1 JSON blobs. The
  // TOTALRECLAW_TAXONOMY_VERSION opt-in and the TOTALRECLAW_CLAIM_FORMAT
  // legacy fallback have both been retired — v1 is the single write path.

  for (const fact of dedupedFacts) {
    try {
      const blindIndices = generateBlindIndices(fact.text);
      const entityTrapdoors = computeEntityTrapdoors(fact.entities);

      // Use pre-computed embedding result if available.
      const embeddingResult = embeddingResultMap.get(fact.text) ?? null;
      const allIndices = embeddingResult
        ? [...blindIndices, ...embeddingResult.lshBuckets, ...entityTrapdoors]
        : [...blindIndices, ...entityTrapdoors];

      // LLM-guided dedup: handle UPDATE/DELETE/NOOP actions.
      if (fact.action === 'NOOP') {
        logger.info(`LLM dedup: NOOP — skipping "${fact.text.slice(0, 60)}…"`);
        skipped++;
        continue;
      }

      if (fact.action === 'DELETE' && fact.existingFactId) {
        // Tombstone the old fact, don't store anything new.
        if (isSubgraphMode()) {
          const tombstone: FactPayload = {
            id: fact.existingFactId,
            timestamp: new Date().toISOString(),
            owner: subgraphOwner || userId!,
            encryptedBlob: '00',
            blindIndices: [],
            decayScore: 0,
            source: 'tombstone',
            contentFp: '',
            agentId: 'openclaw-plugin-auto',
            version: PROTOBUF_VERSION_V4,
          };
          pendingPayloads.push(encodeFactProtobuf(tombstone));
          logger.info(`LLM dedup: DELETE — queued tombstone for ${fact.existingFactId}`);
        } else if (apiClient && authKeyHex) {
          try {
            await apiClient.deleteFact(fact.existingFactId, authKeyHex);
            logger.info(`LLM dedup: DELETE — removed ${fact.existingFactId}`);
          } catch (delErr) {
            logger.warn(`LLM dedup: DELETE failed for ${fact.existingFactId}: ${delErr instanceof Error ? delErr.message : String(delErr)}`);
          }
        }
        superseded++;
        continue;
      }

      if (fact.action === 'UPDATE' && fact.existingFactId) {
        // Tombstone the old fact, then fall through to store the new version.
        if (isSubgraphMode()) {
          const tombstone: FactPayload = {
            id: fact.existingFactId,
            timestamp: new Date().toISOString(),
            owner: subgraphOwner || userId!,
            encryptedBlob: '00',
            blindIndices: [],
            decayScore: 0,
            source: 'tombstone',
            contentFp: '',
            agentId: 'openclaw-plugin-auto',
            version: PROTOBUF_VERSION_V4,
          };
          pendingPayloads.push(encodeFactProtobuf(tombstone));
          logger.info(`LLM dedup: UPDATE — queued tombstone for ${fact.existingFactId}, storing replacement`);
        } else if (apiClient && authKeyHex) {
          try {
            await apiClient.deleteFact(fact.existingFactId, authKeyHex);
            logger.info(`LLM dedup: UPDATE — deleted ${fact.existingFactId}, storing replacement`);
          } catch (delErr) {
            logger.warn(`LLM dedup: UPDATE delete failed for ${fact.existingFactId}: ${delErr instanceof Error ? delErr.message : String(delErr)}`);
          }
        }
        superseded++;
        // Fall through to store the new replacement fact below.
      }

      // ADD (default) or UPDATE (after tombstoning old) — proceed to store.
      // The cosine-based store-time dedup below provides an additional safety net.

      // Store-time near-duplicate check: search vault before writing.
      let effectiveImportance = fact.importance;

      if (STORE_DEDUP_ENABLED && embeddingResult) {
        const dupResult = await searchForNearDuplicates(
          fact.text,
          embeddingResult.embedding,
          allIndices,
          logger,
        );

        if (dupResult) {
          const action = shouldSupersede(fact.importance, dupResult.match);
          if (action === 'skip') {
            logger.info(
              `Store-time dedup: skipping "${fact.text.slice(0, 60)}…" (sim=${dupResult.similarity.toFixed(3)}, existing ID=${dupResult.match.id})`,
            );
            skipped++;
            continue;
          }
          // action === 'supersede': delete old fact, inherit higher importance
          if (isSubgraphMode()) {
            const tombstone: FactPayload = {
              id: dupResult.match.id,
              timestamp: new Date().toISOString(),
              owner: subgraphOwner || userId!,
              encryptedBlob: '00',
              blindIndices: [],
              decayScore: 0,
              source: 'tombstone',
              contentFp: '',
              agentId: 'openclaw-plugin-auto',
              version: PROTOBUF_VERSION_V4,
            };
            pendingPayloads.push(encodeFactProtobuf(tombstone));
            logger.info(
              `Store-time dedup: queued supersede for ${dupResult.match.id} (sim=${dupResult.similarity.toFixed(3)})`,
            );
          } else if (apiClient && authKeyHex) {
            try {
              await apiClient.deleteFact(dupResult.match.id, authKeyHex);
              logger.info(
                `Store-time dedup: superseding ${dupResult.match.id} (sim=${dupResult.similarity.toFixed(3)})`,
              );
            } catch (delErr) {
              logger.warn(
                `Store-time dedup: failed to delete superseded fact ${dupResult.match.id}: ${delErr instanceof Error ? delErr.message : String(delErr)}`,
              );
            }
          }
          effectiveImportance = Math.max(fact.importance, dupResult.match.decayScore);
          superseded++;
        }
      }

      const factSource = sourceOverride || 'auto-extraction';

      // Plugin v3.0.0: always build a Memory Taxonomy v1 JSON blob. The
      // blob is decryptable by `readClaimFromBlob` which prefers v1 →
      // falls back to v0 short-key → then plugin-legacy {text, metadata}
      // for pre-v3 vault entries.
      //
      // We build it BEFORE the on-chain write so Phase 2 contradiction
      // detection can inspect the same canonical Claim the write path will
      // actually store. The string is encrypted byte-identically below.
      //
      // Defensive: if the extraction hook didn't populate `fact.source`
      // (e.g. explicit tool path, legacy caller), default to 'user-inferred'
      // so v1 schema validation passes.
      const factForBlob: ExtractedFact = fact.source
        ? fact
        : { ...fact, source: 'user-inferred' };
      const blobPlaintext = buildCanonicalClaim({
        fact: factForBlob,
        importance: effectiveImportance,
        sourceAgent: factSource,
      });

      const factId = crypto.randomUUID();

      // Phase 2 Slice 2d: contradiction detection + auto-resolution.
      //
      // Runs only when the canonical Claim format is active (legacy blobs
      // carry no entity refs, so there is nothing to check), only for
      // Subgraph / managed-service mode (self-hosted contradiction handling
      // can come later), and only when the new fact has entities. The helper
      // is a no-op in all other cases.
      //
      // Returns one decision per candidate contradicting claim:
      //   - supersede_existing → queue a tombstone + proceed with the new write
      //   - skip_new → do not write the new fact; record the skip reason
      //   - empty list → no contradiction, proceed unchanged
      //
      // On any error (subgraph, decrypt, WASM), the helper returns [] and we
      // fall back to Phase 1 behaviour.
      let contradictionSkipNew = false;
      if (
        isSubgraphMode() &&
        fact.entities &&
        fact.entities.length > 0 &&
        embeddingResult
      ) {
        const newClaimObj = JSON.parse(blobPlaintext) as Record<string, unknown>;
        let decisions: ContradictionDecision[] = [];
        try {
          decisions = await detectAndResolveContradictions({
            newClaim: newClaimObj,
            newClaimId: factId,
            newEmbedding: embeddingResult.embedding,
            subgraphOwner: subgraphOwner || userId!,
            authKeyHex: authKeyHex!,
            encryptionKey: encryptionKey!,
            deps: {
              searchSubgraph: (owner, trapdoors, maxCandidates, authKey) =>
                searchSubgraph(owner, trapdoors, maxCandidates, authKey).then((rows) =>
                  rows.map((r) => ({
                    id: r.id,
                    encryptedBlob: r.encryptedBlob,
                    encryptedEmbedding: r.encryptedEmbedding ?? null,
                    timestamp: r.timestamp,
                    isActive: r.isActive,
                  })),
                ),
              decryptFromHex: (hex, key) => decryptFromHex(hex, key),
            },
            logger: {
              info: (m) => logger.info(m),
              warn: (m) => logger.warn(m),
            },
          });
        } catch (crErr) {
          // detectAndResolveContradictions is supposed to never throw — if
          // it does, we log and continue with Phase 1 behaviour.
          const msg = crErr instanceof Error ? crErr.message : String(crErr);
          logger.warn(`Contradiction detection failed (proceeding with store): ${msg}`);
          decisions = [];
        }

        for (const decision of decisions) {
          if (decision.action === 'supersede_existing') {
            const tombstone: FactPayload = {
              id: decision.existingFactId,
              timestamp: new Date().toISOString(),
              owner: subgraphOwner || userId!,
              encryptedBlob: '00',
              blindIndices: [],
              decayScore: 0,
              source: 'tombstone',
              contentFp: '',
              agentId: 'openclaw-plugin-auto',
              version: PROTOBUF_VERSION_V4,
            };
            pendingPayloads.push(encodeFactProtobuf(tombstone));
            superseded++;
            logger.info(
              `Auto-resolve: queued supersede for ${decision.existingFactId.slice(0, 10)}… ` +
                `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
            );
          } else if (decision.action === 'skip_new') {
            if (decision.reason === 'existing_pinned') {
              logger.warn(
                `Auto-resolve: skipped new write — existing claim ${decision.existingFactId.slice(0, 10)}… is pinned ` +
                  `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
              );
            } else {
              logger.info(
                `Auto-resolve: skipped new write — existing ${decision.existingFactId.slice(0, 10)}… wins ` +
                  `(sim=${decision.similarity.toFixed(3)}, entity=${decision.entityId})`,
              );
            }
            contradictionSkipNew = true;
          }
        }
      }

      if (contradictionSkipNew) {
        skipped++;
        continue;
      }

      const encryptedBlob = encryptToHex(blobPlaintext, encryptionKey);
      const contentFp = generateContentFingerprint(fact.text, dedupKey);

      if (isSubgraphMode()) {
        const protobuf = encodeFactProtobuf({
          id: factId,
          timestamp: new Date().toISOString(),
          owner: subgraphOwner || userId!,
          encryptedBlob: encryptedBlob,
          blindIndices: allIndices,
          decayScore: effectiveImportance,
          source: factSource,
          contentFp: contentFp,
          agentId: 'openclaw-plugin-auto',
          version: PROTOBUF_VERSION_V4,
          encryptedEmbedding: embeddingResult?.encryptedEmbedding,
        });
        pendingPayloads.push(protobuf);
        preparedForSubgraph++;
      } else {
        const payload: StoreFactPayload = {
          id: factId,
          timestamp: new Date().toISOString(),
          encrypted_blob: encryptedBlob,
          blind_indices: allIndices,
          decay_score: effectiveImportance,
          source: factSource,
          content_fp: contentFp,
          agent_id: 'openclaw-plugin-auto',
          encrypted_embedding: embeddingResult?.encryptedEmbedding,
        };
        await apiClient.store(userId, [payload], authKeyHex);
        stored++;
      }
    } catch (err: unknown) {
      // Check for 403 / quota exceeded — invalidate billing cache so next
      // before_agent_start re-fetches and warns the user.
      const factErrMsg = err instanceof Error ? err.message : String(err);
      if (factErrMsg.includes('403') || factErrMsg.toLowerCase().includes('quota')) {
        try { fs.unlinkSync(BILLING_CACHE_PATH); } catch { /* ignore */ }
        logger.warn(`Quota exceeded — billing cache invalidated. ${factErrMsg}`);
        break; // Stop trying to store remaining facts — they'll all fail too
      }
      // Otherwise log and continue — individual fact failures shouldn't block remaining facts
      logger.warn(`Failed to store fact "${fact.text.slice(0, 60)}…": ${factErrMsg}`);
      failedFacts++;
    }
  }

  // Submit subgraph payloads one fact at a time (sequential single-call UserOps).
  // Batch executeBatch UserOps have persistent gas estimation issues on Base Sepolia
  // that cause on-chain reverts. Single-fact UserOps use the simpler submitFactOnChain
  // path which works reliably (same path as totalreclaw_remember). Each submission
  // polls for receipt (120s) before proceeding, so nonce is consumed before the next.
  let batchError: string | undefined;
  if (pendingPayloads.length > 0 && isSubgraphMode()) {
    const batchConfig = { ...getSubgraphConfig(), authKeyHex: authKeyHex!, walletAddress: subgraphOwner ?? undefined };
    for (let i = 0; i < pendingPayloads.length; i++) {
      const slice = [pendingPayloads[i]]; // Single fact per UserOp
      try {
        const result = await submitFactBatchOnChain(slice, batchConfig);
        if (result.success) {
          stored += slice.length;
          logger.info(`Fact ${i + 1}/${pendingPayloads.length}: submitted on-chain (tx=${result.txHash.slice(0, 10)}…)`);
        } else {
          batchError = `On-chain batch submission failed (tx=${result.txHash.slice(0, 10)}…)`;
          logger.warn(batchError);
          break; // Stop submitting remaining batches
        }
      } catch (err: unknown) {
        const errMsg = err instanceof Error ? err.message : String(err);
        if (errMsg.includes('403') || errMsg.toLowerCase().includes('quota')) {
          try { fs.unlinkSync(BILLING_CACHE_PATH); } catch { /* ignore */ }
          batchError = `Quota exceeded — billing cache invalidated. ${errMsg}`;
          logger.warn(batchError);
          break;
        } else {
          batchError = `Batch submission failed: ${errMsg}`;
          logger.warn(batchError);
          break;
        }
      }
    }
  }

  if (stored > 0 || superseded > 0 || skipped > 0 || failedFacts > 0) {
    logger.info(`Auto-extraction results: stored=${stored}, superseded=${superseded}, skipped=${skipped}, failed=${failedFacts}`);
  }

  // If ANY batch failed, throw — even if some facts were stored earlier.
  // A failed/timed-out UserOp may still linger in the bundler mempool as a
  // "nonce zombie." If we return normally, the caller's next storeExtractedFacts
  // call will fetch the same on-chain nonce and hit AA25 ("invalid account nonce").
  // Throwing forces all callers (import loops, chunk handlers) to stop submitting.
  if (batchError) {
    throw new Error(`Memory storage failed (${stored} stored before failure): ${batchError}`);
  }
  if (stored === 0 && failedFacts > 0) {
    throw new Error(`Memory storage failed: ${failedFacts} fact(s) failed to store`);
  }

  return stored;
}

// ---------------------------------------------------------------------------
// Import handler (for totalreclaw_import_from tool)
// ---------------------------------------------------------------------------

/**
 * Handle import_from tool calls in the plugin context.
 *
 * Two paths:
 * 1. Pre-structured sources (Mem0, MCP Memory) — adapter returns facts directly,
 *    stored via storeExtractedFacts().
 * 2. Conversation-based sources (ChatGPT, Claude) — adapter returns conversation
 *    chunks, each chunk is passed through extractFacts() (the same LLM extraction
 *    pipeline used for auto-extraction), then stored via storeExtractedFacts().
 */
async function handlePluginImportFrom(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  _importInProgress = true;
  const startTime = Date.now();

  const source = params.source as string;
  const validSources = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini', 'memoclaw', 'generic-json', 'generic-csv'];

  if (!source || !validSources.includes(source)) {
    return { success: false, error: `Invalid source. Must be one of: ${validSources.join(', ')}` };
  }

  try {
    const { getAdapter } = await import('./import-adapters/index.js');
    const adapter = getAdapter(source as import('./import-adapters/types.js').ImportSource);

    const parseResult = await adapter.parse({
      content: params.content as string | undefined,
      api_key: params.api_key as string | undefined,
      source_user_id: params.source_user_id as string | undefined,
      api_url: params.api_url as string | undefined,
      file_path: params.file_path as string | undefined,
    });

    const hasChunks = parseResult.chunks && parseResult.chunks.length > 0;
    const hasFacts = parseResult.facts && parseResult.facts.length > 0;

    if (parseResult.errors.length > 0 && !hasFacts && !hasChunks) {
      return {
        success: false,
        error: `Failed to parse ${adapter.displayName} data`,
        details: parseResult.errors,
      };
    }

    // Dry run: report what was parsed (chunks or facts)
    if (params.dry_run) {
      if (hasChunks) {
        const totalChunks = parseResult.chunks.length;
        const EXTRACTION_RATIO = 2.5; // avg facts per chunk, from empirical data
        const BATCH_SIZE = 25;
        const SECONDS_PER_BATCH = 45; // ~30s extraction + ~15s embed+store
        const estimatedFacts = Math.round(totalChunks * EXTRACTION_RATIO);
        const estimatedBatches = Math.ceil(totalChunks / BATCH_SIZE);
        const estimatedMinutes = Math.ceil(estimatedBatches * SECONDS_PER_BATCH / 60);

        return {
          success: true,
          dry_run: true,
          source,
          total_chunks: totalChunks,
          total_messages: parseResult.totalMessages,
          estimated_facts: estimatedFacts,
          estimated_batches: estimatedBatches,
          estimated_minutes: estimatedMinutes,
          batch_size: BATCH_SIZE,
          use_background: totalChunks > 50,
          preview: parseResult.chunks.slice(0, 5).map((c) => ({
            title: c.title,
            messages: c.messages.length,
            first_message: c.messages[0]?.text.slice(0, 100),
          })),
          note: `Estimated ${estimatedFacts} facts from ${totalChunks} chunks (~${estimatedMinutes} min).${totalChunks > 50 ? ' Recommended: background import via sessions_spawn.' : ''}`,
          warnings: parseResult.warnings,
        };
      }
      return {
        success: true,
        dry_run: true,
        source,
        total_found: parseResult.facts.length,
        preview: parseResult.facts.slice(0, 10).map((f) => ({
          type: f.type,
          text: f.text.slice(0, 100),
          importance: f.importance,
        })),
        warnings: parseResult.warnings,
      };
    }

    // ── Path 1: Conversation chunks (ChatGPT, Claude) — LLM extraction ──
    if (hasChunks) {
      return handleChunkImport(parseResult.chunks, parseResult.totalMessages, source, logger, startTime, parseResult.warnings);
    }

    // ── Path 2: Pre-structured facts (Mem0, MCP Memory) — direct store ──
    const extractedFacts: ExtractedFact[] = parseResult.facts.map((f) => ({
      text: f.text,
      type: f.type,
      importance: f.importance,
      action: 'ADD' as const,
    }));

    // Store in batches of 50. Stop on any batch failure to prevent
    // nonce zombies from blocking subsequent UserOps (AA25).
    let totalStored = 0;
    let storeError: string | undefined;
    const batchSize = 50;

    for (let i = 0; i < extractedFacts.length; i += batchSize) {
      const batch = extractedFacts.slice(i, i + batchSize);
      try {
        const stored = await storeExtractedFacts(batch, logger);
        totalStored += stored;

        logger.info(
          `Import progress: ${Math.min(i + batchSize, extractedFacts.length)}/${extractedFacts.length} processed, ${totalStored} stored`,
        );
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import stopped at batch ${Math.floor(i / batchSize) + 1}: ${storeError}`);
        break; // Stop processing further batches
      }
    }

    const importWarnings = [...parseResult.warnings];
    if (storeError) {
      importWarnings.push(`Import stopped early: ${storeError}`);
    }

    return {
      success: totalStored > 0,
      source,
      import_id: crypto.randomUUID(),
      total_found: parseResult.facts.length,
      imported: totalStored,
      skipped: parseResult.facts.length - totalStored,
      stopped_early: !!storeError,
      warnings: importWarnings,
      duration_ms: Date.now() - startTime,
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Unknown error';
    logger.error(`Import failed: ${msg}`);
    return { success: false, error: `Import failed: ${msg}` };
  }
}

// ---------------------------------------------------------------------------
// Smart Import — Two-Pass Pipeline (Profile + Triage)
// ---------------------------------------------------------------------------

// Lazy-load WASM for smart import functions (same pattern as crypto.ts / subgraph-store.ts).
let _smartImportWasm: typeof import('@totalreclaw/core') | null = null;
function getSmartImportWasm() {
  if (!_smartImportWasm) _smartImportWasm = require('@totalreclaw/core');
  return _smartImportWasm;
}

/**
 * Check whether the @totalreclaw/core WASM module exposes smart import functions.
 * Returns false if the module is an older version without smart import support.
 */
function hasSmartImportSupport(): boolean {
  try {
    const wasm = getSmartImportWasm();
    return typeof wasm.chunksToSummaries === 'function' &&
      typeof wasm.buildProfileBatchPrompt === 'function' &&
      typeof wasm.parseProfileBatchResponse === 'function' &&
      typeof wasm.buildTriagePrompt === 'function' &&
      typeof wasm.parseTriageResponse === 'function' &&
      typeof wasm.enrichExtractionPrompt === 'function';
  } catch {
    return false;
  }
}

/** Smart import result containing profile, triage decisions, and enriched system prompt. */
interface SmartImportContext {
  /** JSON-serialized UserProfile (for WASM calls that require profile_json) */
  profileJson: string;
  /** Triage decisions indexed by chunk_index */
  decisions: Array<{ chunk_index: number; decision: string; reason: string }>;
  /** Enriched system prompt for extraction (profile context injected) */
  enrichedSystemPrompt: string;
  /** Number of chunks marked for extraction */
  extractCount: number;
  /** Number of chunks marked for skipping */
  skipCount: number;
  /** Duration of the profiling + triage pipeline in ms */
  durationMs: number;
}

/**
 * Run the smart import two-pass pipeline: profile the user from conversation
 * summaries, then triage chunks as EXTRACT or SKIP.
 *
 * All prompt construction and response parsing happens in @totalreclaw/core WASM.
 * LLM calls use the plugin's existing chatCompletion() function.
 *
 * Returns null if smart import is unavailable (old WASM, no LLM config, etc.)
 * so the caller can fall back to blind extraction.
 */
async function runSmartImportPipeline(
  chunks: import('./import-adapters/types.js').ConversationChunk[],
  logger: { info: (msg: string) => void; warn: (msg: string) => void },
): Promise<SmartImportContext | null> {
  // Guard: WASM must have smart import functions
  if (!hasSmartImportSupport()) {
    logger.info('Smart import: WASM module does not support smart import, falling back to blind extraction');
    return null;
  }

  // Guard: LLM must be available
  const llmConfig = resolveLLMConfig();
  if (!llmConfig) {
    logger.info('Smart import: no LLM available, falling back to blind extraction');
    return null;
  }

  const pipelineStart = Date.now();
  const wasm = getSmartImportWasm();

  try {
    // Step 0: Convert chunks to compact summaries (first + last message)
    const wasmChunks = chunks.map((c, i) => ({
      index: i,
      title: c.title || 'Untitled',
      messages: c.messages.map((m) => ({ role: m.role, content: m.text })),
      timestamp: c.timestamp || null,
    }));
    const summaries = wasm.chunksToSummaries(JSON.stringify(wasmChunks));
    const summariesJson = JSON.stringify(summaries);

    // Step 1: Build user profile (batch summarize -> merge)
    const PROFILE_BATCH_SIZE = 50;
    const profileStart = Date.now();
    const partials: unknown[] = [];

    for (let i = 0; i < summaries.length; i += PROFILE_BATCH_SIZE) {
      const batch = summaries.slice(i, i + PROFILE_BATCH_SIZE);
      const prompt = wasm.buildProfileBatchPrompt(JSON.stringify(batch));
      const response = await chatCompletion(llmConfig, [
        { role: 'user', content: prompt },
      ], { maxTokens: 2048, temperature: 0 });

      if (!response) {
        logger.warn(`Smart import: LLM returned empty response for profile batch ${Math.floor(i / PROFILE_BATCH_SIZE) + 1}`);
        continue;
      }

      const partial = wasm.parseProfileBatchResponse(response);
      partials.push(partial);
    }

    if (partials.length === 0) {
      logger.warn('Smart import: no profile batches produced, falling back to blind extraction');
      return null;
    }

    let profile: unknown;
    if (partials.length === 1) {
      // Single batch — skip merge, promote partial to full profile
      // parseProfileBatchResponse returns a PartialProfile; convert to UserProfile shape
      const p = partials[0] as Record<string, unknown>;
      profile = {
        identity: p.identity ?? null,
        themes: p.themes ?? [],
        projects: p.projects ?? [],
        stack: p.stack ?? [],
        decisions: p.decisions ?? [],
        interests: p.interests ?? [],
        skip_patterns: p.skip_patterns ?? [],
      };
    } else {
      const mergePrompt = wasm.buildProfileMergePrompt(JSON.stringify(partials));
      const mergeResponse = await chatCompletion(llmConfig, [
        { role: 'user', content: mergePrompt },
      ], { maxTokens: 2048, temperature: 0 });

      if (!mergeResponse) {
        logger.warn('Smart import: LLM returned empty response for profile merge, falling back to blind extraction');
        return null;
      }

      profile = wasm.parseProfileResponse(mergeResponse);
    }

    const profileJson = JSON.stringify(profile);
    const profileDuration = Date.now() - profileStart;

    const p = profile as Record<string, unknown>;
    const themeCount = Array.isArray(p.themes) ? p.themes.length : 0;
    const skipPatternCount = Array.isArray(p.skip_patterns) ? p.skip_patterns.length : 0;
    logger.info(
      `Smart import: profile built in ${profileDuration}ms (themes=${themeCount}, skip_patterns=${skipPatternCount})`,
    );

    // Step 1.5: Chunk triage (EXTRACT or SKIP)
    const triageStart = Date.now();
    const allDecisions: Array<{ chunk_index: number; decision: string; reason: string }> = [];
    const TRIAGE_BATCH_SIZE = 50;

    for (let i = 0; i < summaries.length; i += TRIAGE_BATCH_SIZE) {
      const batch = summaries.slice(i, i + TRIAGE_BATCH_SIZE);
      const triagePrompt = wasm.buildTriagePrompt(profileJson, JSON.stringify(batch));
      const triageResponse = await chatCompletion(llmConfig, [
        { role: 'user', content: triagePrompt },
      ], { maxTokens: 4096, temperature: 0 });

      if (!triageResponse) {
        logger.warn(`Smart import: LLM returned empty response for triage batch ${Math.floor(i / TRIAGE_BATCH_SIZE) + 1}, defaulting to EXTRACT`);
        // Default all chunks in this batch to EXTRACT
        for (let j = i; j < Math.min(i + TRIAGE_BATCH_SIZE, summaries.length); j++) {
          allDecisions.push({ chunk_index: j, decision: 'EXTRACT', reason: 'triage LLM unavailable' });
        }
        continue;
      }

      const batchDecisions = wasm.parseTriageResponse(triageResponse) as Array<{
        chunk_index: number;
        decision: string;
        reason: string;
      }>;
      allDecisions.push(...batchDecisions);
    }

    const triageDuration = Date.now() - triageStart;

    const extractCount = allDecisions.filter((d) => d.decision !== 'SKIP').length;
    const skipCount = allDecisions.filter((d) => d.decision === 'SKIP').length;
    logger.info(
      `Smart import: triage complete in ${triageDuration}ms (extract=${extractCount}, skip=${skipCount}, total=${chunks.length})`,
    );

    // Step 2: Build enriched system prompt for extraction
    const enrichedSystemPrompt = wasm.enrichExtractionPrompt(profileJson, EXTRACTION_SYSTEM_PROMPT);

    const totalDuration = Date.now() - pipelineStart;
    logger.info(`Smart import: pipeline complete in ${totalDuration}ms`);

    return {
      profileJson,
      decisions: allDecisions,
      enrichedSystemPrompt,
      extractCount,
      skipCount,
      durationMs: totalDuration,
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Smart import: pipeline failed (${msg}), falling back to blind extraction`);
    return null;
  }
}

/**
 * Check if a chunk should be skipped based on triage decisions.
 * If no decision exists for the chunk index, defaults to EXTRACT (safe default).
 */
function isChunkSkipped(
  chunkIndex: number,
  decisions: Array<{ chunk_index: number; decision: string }>,
): { skipped: boolean; reason: string } {
  const decision = decisions.find((d) => d.chunk_index === chunkIndex);
  if (decision && decision.decision === 'SKIP') {
    return { skipped: true, reason: (decision as { reason?: string }).reason || 'triage: skip' };
  }
  return { skipped: false, reason: '' };
}

/**
 * Process a batch (slice) of conversation chunks from a file.
 * Called repeatedly by the agent for large imports.
 */
async function handleBatchImport(
  params: Record<string, unknown>,
  logger: OpenClawPluginApi['logger'],
): Promise<Record<string, unknown>> {
  _importInProgress = true;
  const source = params.source as string;
  const filePath = params.file_path as string | undefined;
  const content = params.content as string | undefined;
  const offset = (params.offset as number) ?? 0;
  const batchSize = (params.batch_size as number) ?? 25;

  const validSources = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini', 'memoclaw', 'generic-json', 'generic-csv'];
  if (!source || !validSources.includes(source)) {
    return { success: false, error: `Invalid source. Must be one of: ${validSources.join(', ')}` };
  }

  const startTime = Date.now();

  const { getAdapter } = await import('./import-adapters/index.js');
  const adapter = getAdapter(source as import('./import-adapters/types.js').ImportSource);

  const parseResult = await adapter.parse({ content, file_path: filePath });

  if (parseResult.errors.length > 0 && parseResult.chunks.length === 0) {
    return { success: false, error: parseResult.errors.join('; ') };
  }

  const totalChunks = parseResult.chunks.length;
  const slice = parseResult.chunks.slice(offset, offset + batchSize);
  const remaining = Math.max(0, totalChunks - offset - slice.length);

  // --- Smart Import: Profile + Triage ---
  // Build profile from ALL chunks (not just the slice) for full context,
  // then triage only the current slice. For simplicity, we rebuild on every
  // batch call — optimization (caching) can come later.
  const smartCtx = await runSmartImportPipeline(parseResult.chunks, logger);
  let chunksSkipped = 0;

  // Process the slice through the normal extraction + storage pipeline.
  // If a batch fails (nonce zombie, quota exceeded, etc.), stop immediately
  // to prevent subsequent UserOps from hitting AA25 nonce conflicts.
  let factsExtracted = 0;
  let factsStored = 0;
  let chunksProcessed = 0;
  let storeError: string | undefined;

  for (let i = 0; i < slice.length; i++) {
    const chunk = slice[i];
    const globalIndex = offset + i; // Index in the full chunks array

    // Smart import: skip chunks triaged as SKIP
    if (smartCtx) {
      const { skipped, reason } = isChunkSkipped(globalIndex, smartCtx.decisions);
      if (skipped) {
        logger.info(`Import: skipping chunk ${globalIndex + 1}/${totalChunks}: "${chunk.title}" (${reason})`);
        chunksSkipped++;
        chunksProcessed++;
        continue;
      }
    }

    logger.info(`Import: extracting facts from chunk ${globalIndex + 1}/${totalChunks}: "${chunk.title}"`);

    const messages = chunk.messages.map((m) => ({ role: m.role, content: m.text }));
    const facts = await extractFacts(
      messages,
      'full',
      undefined, // no existing memories for dedup during import
      smartCtx?.enrichedSystemPrompt, // profile-enriched extraction prompt
    );
    chunksProcessed++;

    if (facts.length > 0) {
      factsExtracted += facts.length;
      try {
        const stored = await storeExtractedFacts(facts, logger);
        factsStored += stored;
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import batch stopped at chunk ${globalIndex + 1}/${totalChunks}: ${storeError}`);
        break; // Stop processing further chunks — a zombie UserOp may block writes
      }
    }
  }

  return {
    success: factsStored > 0 || (!storeError && factsExtracted === 0),
    batch_offset: offset,
    batch_size: chunksProcessed,
    total_chunks: totalChunks,
    facts_extracted: factsExtracted,
    facts_stored: factsStored,
    chunks_skipped: chunksSkipped,
    remaining_chunks: remaining,
    is_complete: remaining === 0 && !storeError,
    stopped_early: !!storeError,
    error: storeError,
    smart_import: smartCtx ? {
      profile_duration_ms: smartCtx.durationMs,
      extract_count: smartCtx.extractCount,
      skip_count: smartCtx.skipCount,
    } : null,
    // Estimation for the full import
    estimated_total_facts: Math.round(totalChunks * 2.5),
    estimated_total_userops: Math.ceil(totalChunks * 2.5 / 15),
    estimated_minutes: Math.ceil(Math.ceil(totalChunks / batchSize) * 45 / 60),
    duration_ms: Date.now() - startTime,
  };
}

/**
 * Process conversation chunks through LLM extraction and store results.
 *
 * Each chunk is passed to extractFacts() — the same extraction pipeline used
 * for auto-extraction during live conversations. This ensures import quality
 * matches conversation extraction quality.
 */
async function handleChunkImport(
  chunks: import('./import-adapters/types.js').ConversationChunk[],
  totalMessages: number,
  source: string,
  logger: OpenClawPluginApi['logger'],
  startTime: number,
  warnings: string[],
): Promise<Record<string, unknown>> {
  let totalExtracted = 0;
  let totalStored = 0;
  let chunksProcessed = 0;
  let chunksSkipped = 0;

  let storeError: string | undefined;

  // --- Smart Import: Profile + Triage ---
  const smartCtx = await runSmartImportPipeline(chunks, logger);

  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    chunksProcessed++;

    // Smart import: skip chunks triaged as SKIP
    if (smartCtx) {
      const { skipped, reason } = isChunkSkipped(i, smartCtx.decisions);
      if (skipped) {
        logger.info(
          `Import: skipping chunk ${chunksProcessed}/${chunks.length}: "${chunk.title}" (${reason})`,
        );
        chunksSkipped++;
        continue;
      }
    }

    logger.info(
      `Import: extracting facts from chunk ${chunksProcessed}/${chunks.length}: "${chunk.title}"`,
    );

    // Convert chunk messages to the format extractFacts() expects.
    // extractFacts() takes an array of message-like objects with { role, content }.
    const messages = chunk.messages.map((m) => ({
      role: m.role,
      content: m.text,
    }));

    // Use 'full' mode to extract ALL valuable memories from the chunk
    // (not just the last few messages like 'turn' mode does).
    // Smart import: pass enriched system prompt with user profile context.
    const facts = await extractFacts(
      messages,
      'full',
      undefined, // no existing memories for dedup during import
      smartCtx?.enrichedSystemPrompt, // profile-enriched extraction prompt
    );

    if (facts.length > 0) {
      totalExtracted += facts.length;

      try {
        // Store through the normal pipeline (dedup, encrypt, store).
        // storeExtractedFacts throws on batch failure to prevent nonce zombies.
        const stored = await storeExtractedFacts(facts, logger);
        totalStored += stored;

        logger.info(
          `Import chunk ${chunksProcessed}/${chunks.length}: extracted ${facts.length} facts, stored ${stored}`,
        );
      } catch (err: unknown) {
        storeError = err instanceof Error ? err.message : String(err);
        logger.warn(`Import stopped at chunk ${chunksProcessed}/${chunks.length}: ${storeError}`);
        break; // Stop processing further chunks — a zombie UserOp may block writes
      }
    }
  }

  if (totalExtracted === 0 && chunks.length > 0 && !storeError && chunksSkipped < chunks.length) {
    warnings.push(
      `Processed ${chunks.length} conversation chunks (${totalMessages} messages) but the LLM ` +
      `did not extract any facts worth storing. This can happen if the conversations are mostly ` +
      `generic/ephemeral content without personal facts, preferences, or decisions.`,
    );
  }

  if (storeError) {
    warnings.push(`Import stopped early: ${storeError}. ${chunks.length - chunksProcessed} chunk(s) not processed.`);
  }

  return {
    success: totalStored > 0 || totalExtracted > 0,
    source,
    import_id: crypto.randomUUID(),
    total_chunks: chunks.length,
    chunks_processed: chunksProcessed,
    chunks_skipped: chunksSkipped,
    total_messages: totalMessages,
    facts_extracted: totalExtracted,
    imported: totalStored,
    skipped: totalExtracted - totalStored,
    stopped_early: !!storeError,
    smart_import: smartCtx ? {
      profile_duration_ms: smartCtx.durationMs,
      extract_count: smartCtx.extractCount,
      skip_count: smartCtx.skipCount,
    } : null,
    warnings,
    duration_ms: Date.now() - startTime,
  };
}

// ---------------------------------------------------------------------------
// Plugin definition
// ---------------------------------------------------------------------------

const plugin = {
  id: 'totalreclaw',
  name: 'TotalReclaw',
  description: 'End-to-end encrypted memory vault for AI agents',
  kind: 'memory' as const,
  configSchema: {
    type: 'object',
    additionalProperties: false,
    properties: {
      extraction: {
        type: 'object',
        properties: {
          model: { type: 'string', description: "Override the extraction model (e.g., 'glm-4.5-flash', 'gpt-4.1-mini')" },
          enabled: { type: 'boolean', description: 'Enable/disable auto-extraction (default: true)' },
        },
        additionalProperties: false,
      },
    },
  },

  register(api: OpenClawPluginApi) {
    // ---------------------------------------------------------------
    // LLM client initialization (auto-detect provider from OpenClaw config)
    // ---------------------------------------------------------------

    initLLMClient({
      primaryModel: api.config?.agents?.defaults?.model?.primary as string | undefined,
      pluginConfig: api.pluginConfig,
      openclawProviders: api.config?.models?.providers,
      logger: api.logger,
    });

    // ---------------------------------------------------------------
    // Service registration (lifecycle logging)
    // ---------------------------------------------------------------

    api.registerService({
      id: 'totalreclaw',
      start: () => {
        api.logger.info('TotalReclaw plugin loaded');
      },
      stop: () => {
        api.logger.info('TotalReclaw plugin stopped');
      },
    });

    // ---------------------------------------------------------------
    // Tool: totalreclaw_remember
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_remember',
        label: 'Remember',
        description:
          'Store a memory in the encrypted vault. Use this when the user shares important information worth remembering.',
        parameters: {
          type: 'object',
          properties: {
            text: {
              type: 'string',
              description: 'The memory text to store',
            },
            type: {
              type: 'string',
              enum: [...VALID_MEMORY_TYPES, ...LEGACY_V0_MEMORY_TYPES],
              description:
                'Memory Taxonomy v1 type: claim, preference, directive, commitment, episode, summary. ' +
                'Use "claim" for factual assertions and decisions (populate `reasoning` with the why clause). ' +
                'Use "directive" for imperative rules ("always X", "never Y"), "commitment" for future intent, ' +
                'and "episode" for notable events. Legacy v0 tokens (fact, decision, episodic, goal, context, ' +
                'rule) are silently coerced to their v1 equivalents. Default: claim.',
            },
            source: {
              type: 'string',
              enum: [...VALID_MEMORY_SOURCES],
              description:
                'v1 provenance tag. "user" = user explicitly stated it, "user-inferred" = inferred from user ' +
                'signals, "assistant" = assistant-authored (downgrade unless user affirmed), "external" / ' +
                '"derived" = rare. Explicit remembers default to "user".',
            },
            scope: {
              type: 'string',
              enum: [...VALID_MEMORY_SCOPES],
              description:
                'v1 life-domain scope: work, personal, health, family, creative, finance, misc, unspecified. ' +
                'Default: unspecified.',
            },
            reasoning: {
              type: 'string',
              description:
                'For type=claim expressing a decision, the WHY clause ("because Y"). Max 256 chars. ' +
                'Omit for non-decision claims.',
              maxLength: 256,
            },
            importance: {
              type: 'number',
              minimum: 1,
              maximum: 10,
              description: 'Importance score 1-10 (default: 8 for explicit remember)',
            },
            entities: {
              type: 'array',
              description:
                'Named entities this memory is about (people, projects, tools, companies, concepts, places). ' +
                'Supplying entities enables Phase 2 contradiction detection against existing facts about the same entity. ' +
                'Omit if unclear — a best-effort fallback will still store the memory.',
              items: {
                type: 'object',
                properties: {
                  name: { type: 'string' },
                  type: {
                    type: 'string',
                    enum: ['person', 'project', 'tool', 'company', 'concept', 'place'],
                  },
                  role: { type: 'string' },
                },
                required: ['name', 'type'],
                additionalProperties: false,
              },
            },
          },
          required: ['text'],
          additionalProperties: false,
        },
        async execute(
          _toolCallId: string,
          params: {
            text: string;
            type?: string;
            source?: string;
            scope?: string;
            reasoning?: string;
            importance?: number;
            entities?: Array<{ name: string; type: string; role?: string }>;
          },
        ) {
          try {
            await requireFullSetup(api.logger);

            // v1 taxonomy: route explicit remembers through the same canonical
            // store path that auto-extraction uses (`storeExtractedFacts`). This
            // emits a Memory Taxonomy v1 JSON blob, generates entity trapdoors,
            // and runs through the Phase 2 contradiction-resolution pipeline.
            //
            // Accept legacy v0 tokens on input and coerce to v1 via
            // `normalizeToV1Type` so agents that still emit the pre-v3
            // taxonomy keep working.
            const rawType = typeof params.type === 'string' ? params.type.toLowerCase() : 'claim';
            const memoryType: MemoryType = isValidMemoryType(rawType)
              ? rawType
              : normalizeToV1Type(rawType);

            // Source defaults to 'user' for explicit remembers (the user is
            // the author by definition). Ignored if the caller passes an
            // invalid value.
            const rawSource = typeof params.source === 'string' ? params.source.toLowerCase() : 'user';
            const memorySource: MemorySource =
              (VALID_MEMORY_SOURCES as readonly string[]).includes(rawSource)
                ? (rawSource as MemorySource)
                : 'user';

            const rawScope = typeof params.scope === 'string' ? params.scope.toLowerCase() : 'unspecified';
            const memoryScope: MemoryScope =
              (VALID_MEMORY_SCOPES as readonly string[]).includes(rawScope)
                ? (rawScope as MemoryScope)
                : 'unspecified';

            const reasoning =
              typeof params.reasoning === 'string' && params.reasoning.length > 0
                ? params.reasoning.slice(0, 256)
                : undefined;

            // Explicit remember defaults to importance 8 (above auto-extraction's
            // typical 6-7), so store-time dedup's shouldSupersede prefers the
            // explicit call when it collides with an auto-extracted claim.
            const importance = Math.max(1, Math.min(10, params.importance ?? 8));

            const validatedEntities: ExtractedEntity[] = Array.isArray(params.entities)
              ? params.entities
                  .map((e) => parseEntity(e))
                  .filter((e): e is ExtractedEntity => e !== null)
              : [];

            const fact: ExtractedFact = {
              text: params.text.slice(0, 512),
              type: memoryType,
              source: memorySource,
              scope: memoryScope,
              reasoning,
              importance,
              action: 'ADD',
              confidence: 1.0, // user explicitly asked to remember — highest confidence
            };
            if (validatedEntities.length > 0) fact.entities = validatedEntities;

            const stored = await storeExtractedFacts([fact], api.logger, 'explicit');
            api.logger.info(
              `totalreclaw_remember: routed to storeExtractedFacts (stored=${stored}, entities=${validatedEntities.length})`,
            );

            if (stored === 0) {
              // Dedup or supersession consumed the write. Treat as success from
              // the user's perspective — the memory's content is already in the
              // vault (possibly under a different ID).
              return {
                content: [
                  {
                    type: 'text',
                    text: 'Memory noted (matched existing content in vault).',
                  },
                ],
              };
            }

            return {
              content: [{ type: 'text', text: 'Memory encrypted and stored.' }],
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_remember failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to store memory: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_remember' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_recall
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_recall',
        label: 'Recall',
        description:
          'Search the encrypted memory vault. Returns the most relevant memories matching the query.',
        parameters: {
          type: 'object',
          properties: {
            query: {
              type: 'string',
              description: 'Search query text',
            },
            k: {
              type: 'number',
              minimum: 1,
              maximum: 20,
              description: 'Number of results to return (default: 8)',
            },
          },
          required: ['query'],
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: { query: string; k?: number }) {
          try {
            await requireFullSetup(api.logger);

            const k = Math.min(params.k ?? 8, 20);

            // 1. Generate word trapdoors (blind indices for the query).
            const wordTrapdoors = generateBlindIndices(params.query);

            // 2. Generate query embedding + LSH trapdoors (may fail gracefully).
            let queryEmbedding: number[] | null = null;
            let lshTrapdoors: string[] = [];
            try {
              queryEmbedding = await generateEmbedding(params.query, { isQuery: true });
              const hasher = getLSHHasher(api.logger);
              if (hasher && queryEmbedding) {
                lshTrapdoors = hasher.hash(queryEmbedding);
              }
            } catch (err) {
              const msg = err instanceof Error ? err.message : String(err);
              api.logger.warn(`Recall: embedding/LSH generation failed (using word-only trapdoors): ${msg}`);
            }

            // 3. Merge word trapdoors + LSH trapdoors.
            const allTrapdoors = [...wordTrapdoors, ...lshTrapdoors];

            if (allTrapdoors.length === 0) {
              return {
                content: [{ type: 'text', text: 'No searchable terms in query.' }],
                details: { count: 0, memories: [] },
              };
            }

            // 4. Request more candidates than needed so we can re-rank client-side.
            // 5. Decrypt candidates (text + embeddings) and build reranker input.
            const rerankerCandidates: RerankerCandidate[] = [];
            const metaMap = new Map<string, { metadata: Record<string, unknown>; timestamp: number }>();

            if (isSubgraphMode()) {
              // --- Subgraph search path ---
              const factCount = await getSubgraphFactCount(subgraphOwner || userId!, authKeyHex!);
              const pool = computeCandidatePool(factCount);
              let subgraphResults = await searchSubgraph(subgraphOwner || userId!, allTrapdoors, pool, authKeyHex!);

              // Always run broadened search and merge — ensures vocabulary mismatches
              // (e.g., "preferences" vs "prefer") don't cause recall failures.
              // The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
              try {
                const broadenedResults = await searchSubgraphBroadened(subgraphOwner || userId!, pool, authKeyHex!);
                // Merge broadened results with existing (deduplicate by ID)
                const existingIds = new Set(subgraphResults.map(r => r.id));
                for (const br of broadenedResults) {
                  if (!existingIds.has(br.id)) {
                    subgraphResults.push(br);
                  }
                }
              } catch { /* best-effort */ }

              for (const result of subgraphResults) {
                try {
                  const docJson = decryptFromHex(result.encryptedBlob, encryptionKey!);
                  if (isDigestBlob(docJson)) continue;
                  const doc = readClaimFromBlob(docJson);

                  let decryptedEmbedding: number[] | undefined;
                  if (result.encryptedEmbedding) {
                    try {
                      decryptedEmbedding = JSON.parse(
                        decryptFromHex(result.encryptedEmbedding, encryptionKey!),
                      );
                    } catch {
                      // Embedding decryption failed -- proceed without it.
                    }
                  }

                  if (decryptedEmbedding && decryptedEmbedding.length !== getEmbeddingDims()) {
                    try {
                      decryptedEmbedding = await generateEmbedding(doc.text);
                    } catch {
                      decryptedEmbedding = undefined;
                    }
                  }

                  rerankerCandidates.push({
                    id: result.id,
                    text: doc.text,
                    embedding: decryptedEmbedding,
                    importance: doc.importance / 10,
                    createdAt: result.timestamp ? parseInt(result.timestamp, 10) : undefined,
                    // Retrieval v2 Tier 1: surface v1 source so applySourceWeights
                    // can multiply the final RRF score by the source weight.
                    source: typeof doc.metadata?.source === 'string' ? doc.metadata.source : undefined,
                  });

                  metaMap.set(result.id, {
                    metadata: doc.metadata ?? {},
                    timestamp: Date.now(),
                    category: doc.category,
                  });
                } catch {
                  // Skip candidates we cannot decrypt.
                }
              }

              // Update hot cache with top results for instant auto-recall.
              try {
                if (!pluginHotCache && encryptionKey) {
                  const config = getSubgraphConfig();
                  pluginHotCache = new PluginHotCache(config.cachePath, encryptionKey.toString('hex'));
                  pluginHotCache.load();
                }
                if (pluginHotCache) {
                  const hotFacts: HotFact[] = rerankerCandidates.map((c) => {
                    const meta = metaMap.get(c.id);
                    const importance = meta?.metadata.importance
                      ? Math.round((meta.metadata.importance as number) * 10)
                      : 5;
                    return { id: c.id, text: c.text, importance };
                  });
                  pluginHotCache.setHotFacts(hotFacts);
                  pluginHotCache.setFactCount(rerankerCandidates.length);
                  pluginHotCache.flush();
                }
              } catch {
                // Hot cache update is best-effort -- don't fail the recall.
              }
            } else {
              // --- Server search path (existing behavior) ---
              const factCount = await getFactCount(api.logger);
              const pool = computeCandidatePool(factCount);
              const candidates = await apiClient!.search(
                userId!,
                allTrapdoors,
                pool,
                authKeyHex!,
              );

              for (const candidate of candidates) {
                try {
                  const docJson = decryptFromHex(candidate.encrypted_blob, encryptionKey!);
                  if (isDigestBlob(docJson)) continue;
                  const doc = readClaimFromBlob(docJson);

                  let decryptedEmbedding: number[] | undefined;
                  if (candidate.encrypted_embedding) {
                    try {
                      decryptedEmbedding = JSON.parse(
                        decryptFromHex(candidate.encrypted_embedding, encryptionKey!),
                      );
                    } catch {
                      // Embedding decryption failed -- proceed without it.
                    }
                  }

                  if (decryptedEmbedding && decryptedEmbedding.length !== getEmbeddingDims()) {
                    try {
                      decryptedEmbedding = await generateEmbedding(doc.text);
                    } catch {
                      decryptedEmbedding = undefined;
                    }
                  }

                  rerankerCandidates.push({
                    id: candidate.fact_id,
                    text: doc.text,
                    embedding: decryptedEmbedding,
                    importance: doc.importance / 10,
                    createdAt: typeof candidate.timestamp === 'number'
                      ? candidate.timestamp / 1000
                      : new Date(candidate.timestamp).getTime() / 1000,
                    source: typeof doc.metadata?.source === 'string' ? doc.metadata.source : undefined,
                  });

                  metaMap.set(candidate.fact_id, {
                    metadata: doc.metadata ?? {},
                    timestamp: candidate.timestamp,
                    category: doc.category,
                  });
                } catch {
                  // Skip candidates we cannot decrypt (e.g. corrupted data).
                }
              }
            }

            // 6. Re-rank with BM25 + cosine + intent-weighted RRF fusion.
            const queryIntent = detectQueryIntent(params.query);
            const reranked = rerank(
              params.query,
              queryEmbedding ?? [],
              rerankerCandidates,
              k,
              INTENT_WEIGHTS[queryIntent],
              /* applySourceWeights (Retrieval v2 Tier 1) */ true,
            );

            if (reranked.length === 0) {
              return {
                content: [{ type: 'text', text: 'No memories found matching your query.' }],
                details: { count: 0, memories: [] },
              };
            }

            // 6b. Cosine similarity threshold gate — skip results when the
            //     best match is below the minimum relevance threshold.
            const maxCosine = Math.max(
              ...reranked.map((r) => r.cosineSimilarity ?? 0),
            );
            if (maxCosine < COSINE_THRESHOLD) {
              api.logger.info(
                `Recall: cosine threshold gate filtered results (max=${maxCosine.toFixed(3)}, threshold=${COSINE_THRESHOLD})`,
              );
              return {
                content: [{ type: 'text', text: 'No relevant memories found for this query.' }],
                details: { count: 0, memories: [] },
              };
            }

            // 7. Format results.
            const lines = reranked.map((m, i) => {
              const meta = metaMap.get(m.id);
              const imp = meta?.metadata.importance
                ? ` (importance: ${Math.round((meta.metadata.importance as number) * 10)}/10)`
                : '';
              const age = meta ? relativeTime(meta.timestamp) : '';
              const typeTag = meta?.category ? `[${meta.category}] ` : '';
              return `${i + 1}. ${typeTag}${m.text}${imp} -- ${age} [ID: ${m.id}]`;
            });

            const formatted = lines.join('\n');

            return {
              content: [{ type: 'text', text: formatted }],
              details: {
                count: reranked.length,
                memories: reranked.map((m) => ({
                  factId: m.id,
                  text: m.text,
                })),
              },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_recall failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to search memories: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_recall' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_forget
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_forget',
        label: 'Forget',
        description: 'Delete a specific memory by its ID.',
        parameters: {
          type: 'object',
          properties: {
            factId: {
              type: 'string',
              description: 'The UUID of the memory to delete',
            },
          },
          required: ['factId'],
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: { factId: string }) {
          try {
            await requireFullSetup(api.logger);

            if (isSubgraphMode()) {
              // On-chain tombstone: write a minimal protobuf with decayScore=0
              // The subgraph will overwrite the fact and set isActive=false
              const config = { ...getSubgraphConfig(), authKeyHex: authKeyHex!, walletAddress: subgraphOwner ?? undefined };
              const tombstone: FactPayload = {
                id: params.factId,
                timestamp: new Date().toISOString(),
                owner: subgraphOwner || userId!,
                encryptedBlob: '00', // minimal 1-byte placeholder
                blindIndices: [],
                decayScore: 0,
                source: 'tombstone',
                contentFp: '',
                agentId: 'openclaw-plugin',
                version: PROTOBUF_VERSION_V4,
              };
              const protobuf = encodeFactProtobuf(tombstone);
              const result = await submitFactOnChain(protobuf, config);
              if (!result.success) {
                throw new Error(`On-chain tombstone failed (tx=${result.txHash?.slice(0, 10) || 'none'}…)`);
              }
              api.logger.info(`Tombstone written for ${params.factId}: tx=${result.txHash}`);
              return {
                content: [{ type: 'text', text: `Memory ${params.factId} deleted (on-chain tombstone, tx: ${result.txHash})` }],
                details: { deleted: true, txHash: result.txHash },
              };
            } else {
              await apiClient!.deleteFact(params.factId, authKeyHex!);
              return {
                content: [{ type: 'text', text: `Memory ${params.factId} deleted` }],
                details: { deleted: true },
              };
            }
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_forget failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to delete memory: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_forget' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_export
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_export',
        label: 'Export',
        description:
          'Export all stored memories. Decrypts every memory and returns them as JSON or Markdown.',
        parameters: {
          type: 'object',
          properties: {
            format: {
              type: 'string',
              enum: ['json', 'markdown'],
              description: 'Output format (default: json)',
            },
          },
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: { format?: string }) {
          try {
            await requireFullSetup(api.logger);

            const format = params.format ?? 'json';

            // Paginate through all facts.
            const allFacts: Array<{
              id: string;
              text: string;
              metadata: Record<string, unknown>;
              created_at: string;
            }> = [];

            if (isSubgraphMode()) {
              // Query subgraph for all active facts (cursor-based pagination via id_gt)
              const config = getSubgraphConfig();
              const relayUrl = config.relayUrl;
              const PAGE_SIZE = 1000;
              let lastId = '';
              const owner = subgraphOwner || userId || '';
              console.error(`[TotalReclaw Export] owner=${owner} subgraphOwner=${subgraphOwner} userId=${userId} relayUrl=${relayUrl} authKey=${authKeyHex ? authKeyHex.slice(0, 8) + '...' : 'MISSING'} isSubgraph=${isSubgraphMode()}`);

              while (true) {
                const hasLastId = lastId !== '';
                const query = hasLastId
                  ? `query($owner:Bytes!,$first:Int!,$lastId:String!){facts(where:{owner:$owner,isActive:true,id_gt:$lastId},first:$first,orderBy:id,orderDirection:asc){id encryptedBlob timestamp sequenceId}}`
                  : `query($owner:Bytes!,$first:Int!){facts(where:{owner:$owner,isActive:true},first:$first,orderBy:id,orderDirection:asc){id encryptedBlob timestamp sequenceId}}`;
                const variables: Record<string, unknown> = hasLastId
                  ? { owner, first: PAGE_SIZE, lastId }
                  : { owner, first: PAGE_SIZE };

                const res = await fetch(`${relayUrl}/v1/subgraph`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    'X-TotalReclaw-Client': 'openclaw-plugin',
                    ...(authKeyHex ? { Authorization: `Bearer ${authKeyHex}` } : {}),
                  },
                  body: JSON.stringify({ query, variables }),
                });

                const json = (await res.json()) as {
                  data?: { facts?: Array<{ id: string; encryptedBlob: string; source: string; agentId: string; timestamp: string; sequenceId: string }> };
                  error?: string;
                  errors?: Array<{ message: string }>;
                };
                // Surface relay/subgraph errors instead of silently returning empty
                if (json.error || json.errors) {
                  const errMsg = json.error || json.errors?.map(e => e.message).join('; ') || 'Unknown error';
                  api.logger.error(`Export subgraph query failed: ${errMsg} (owner=${owner}, status=${res.status})`);
                  return {
                    content: [{ type: 'text', text: `Export failed: ${errMsg}` }],
                  };
                }
                const facts = json?.data?.facts || [];
                if (facts.length === 0) break;

                for (const fact of facts) {
                  try {
                    let hexBlob = fact.encryptedBlob;
                    if (hexBlob.startsWith('0x')) hexBlob = hexBlob.slice(2);
                    const docJson = decryptFromHex(hexBlob, encryptionKey!);
                    if (isDigestBlob(docJson)) continue;
                    const doc = readClaimFromBlob(docJson);
                    allFacts.push({
                      id: fact.id,
                      text: doc.text,
                      metadata: doc.metadata,
                      created_at: new Date(parseInt(fact.timestamp) * 1000).toISOString(),
                    });
                  } catch {
                    // Skip facts we cannot decrypt
                  }
                }

                if (facts.length < PAGE_SIZE) break;
                lastId = facts[facts.length - 1].id;
              }
            } else {
              // HTTP server mode — paginate through PostgreSQL facts
              let cursor: string | undefined;
              let hasMore = true;

              while (hasMore) {
                const page = await apiClient!.exportFacts(authKeyHex!, 1000, cursor);

                for (const fact of page.facts) {
                  try {
                    const docJson = decryptFromHex(fact.encrypted_blob, encryptionKey!);
                    if (isDigestBlob(docJson)) continue;
                    const doc = readClaimFromBlob(docJson);
                    allFacts.push({
                      id: fact.id,
                      text: doc.text,
                      metadata: doc.metadata,
                      created_at: fact.created_at,
                    });
                  } catch {
                    // Skip facts we cannot decrypt.
                  }
                }

                cursor = page.cursor ?? undefined;
                hasMore = page.has_more;
              }
            }

            // Format output.
            let formatted: string;

            if (format === 'markdown') {
              if (allFacts.length === 0) {
                formatted = '*No memories stored.*';
              } else {
                const lines = allFacts.map((f, i) => {
                  const meta = f.metadata;
                  const type = (meta.type as string) ?? 'fact';
                  const imp = meta.importance
                    ? ` (importance: ${Math.round((meta.importance as number) * 10)}/10)`
                    : '';
                  return `${i + 1}. **[${type}]** ${f.text}${imp}  \n   _ID: ${f.id} | Created: ${f.created_at}_`;
                });
                formatted = `# Exported Memories (${allFacts.length})\n\n${lines.join('\n')}`;
              }
            } else {
              formatted = JSON.stringify(allFacts, null, 2);
            }

            return {
              content: [{ type: 'text', text: formatted }],
              details: { count: allFacts.length },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_export failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to export memories: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_export' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_status
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_status',
        label: 'Status',
        description:
          'Check TotalReclaw billing and subscription status — tier, writes used, reset date.',
        parameters: {
          type: 'object',
          properties: {},
          additionalProperties: false,
        },
        async execute() {
          try {
            await requireFullSetup(api.logger);

            if (!authKeyHex) {
              return {
                content: [{ type: 'text', text: 'Auth credentials are not available. Please initialize first.' }],
              };
            }

            const serverUrl = CONFIG.serverUrl;
            const walletAddr = subgraphOwner || userId || '';
            const response = await fetch(`${serverUrl}/v1/billing/status?wallet_address=${encodeURIComponent(walletAddr)}`, {
              method: 'GET',
              headers: {
                'Authorization': `Bearer ${authKeyHex}`,
                'Accept': 'application/json',
                'X-TotalReclaw-Client': 'openclaw-plugin',
              },
            });

            if (!response.ok) {
              const body = await response.text().catch(() => '');
              return {
                content: [{ type: 'text', text: `Failed to fetch billing status (HTTP ${response.status}): ${body || response.statusText}` }],
              };
            }

            const data = await response.json() as Record<string, unknown>;
            const tier = (data.tier as string) || 'free';
            const freeWritesUsed = (data.free_writes_used as number) ?? 0;
            const freeWritesLimit = (data.free_writes_limit as number) ?? 0;
            const freeWritesResetAt = data.free_writes_reset_at as string | undefined;

            // Update billing cache on success.
            writeBillingCache({
              tier,
              free_writes_used: freeWritesUsed,
              free_writes_limit: freeWritesLimit,
              features: data.features as BillingCache['features'] | undefined,
              checked_at: Date.now(),
            });

            const tierLabel = tier === 'pro' ? 'Pro' : 'Free';
            const lines: string[] = [
              `Tier: ${tierLabel}`,
              `Writes: ${freeWritesUsed}/${freeWritesLimit} used this month`,
            ];
            if (freeWritesResetAt) {
              lines.push(`Resets: ${new Date(freeWritesResetAt).toLocaleDateString()}`);
            }
            if (tier !== 'pro') {
              lines.push(`Pricing: https://totalreclaw.xyz/pricing`);
            }

            return {
              content: [{ type: 'text', text: lines.join('\n') }],
              details: { tier, free_writes_used: freeWritesUsed, free_writes_limit: freeWritesLimit },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_status failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to check status: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_status' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_consolidate
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_consolidate',
        label: 'Consolidate',
        description:
          'Deduplicate and merge related memories. Self-hosted mode only.',
        parameters: {
          type: 'object',
          properties: {
            dry_run: {
              type: 'boolean',
              description: 'Preview only (default: false)',
            },
          },
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: { dry_run?: boolean }) {
          try {
            await requireFullSetup(api.logger);

            const dryRun = params.dry_run ?? false;

            // Consolidation is only available in centralized (HTTP server) mode.
            if (isSubgraphMode()) {
              return {
                content: [{ type: 'text', text: 'Consolidation is currently only available in centralized mode.' }],
              };
            }

            if (!apiClient || !authKeyHex || !encryptionKey) {
              return {
                content: [{ type: 'text', text: 'Plugin not fully initialized. Cannot consolidate.' }],
              };
            }

            // 1. Export all facts (paginated, max 10 pages of 1000).
            const allDecrypted: DecryptedCandidate[] = [];
            let cursor: string | undefined;
            let hasMore = true;
            let pageCount = 0;
            const MAX_PAGES = 10;

            while (hasMore && pageCount < MAX_PAGES) {
              const page = await apiClient.exportFacts(authKeyHex, 1000, cursor);

              for (const fact of page.facts) {
                try {
                  const docJson = decryptFromHex(fact.encrypted_blob, encryptionKey);
                  if (isDigestBlob(docJson)) continue;
                  const doc = readClaimFromBlob(docJson);

                  let embedding: number[] | null = null;
                  try {
                    embedding = await generateEmbedding(doc.text);
                  } catch { /* skip — fact will not be clustered */ }

                  allDecrypted.push({
                    id: fact.id,
                    text: doc.text,
                    embedding,
                    importance: doc.importance,
                    decayScore: fact.decay_score,
                    createdAt: new Date(fact.created_at).getTime(),
                    version: fact.version,
                  });
                } catch {
                  // Skip undecryptable facts.
                }
              }

              cursor = page.cursor ?? undefined;
              hasMore = page.has_more;
              pageCount++;
            }

            if (allDecrypted.length === 0) {
              return {
                content: [{ type: 'text', text: 'No memories found to consolidate.' }],
              };
            }

            // 2. Cluster by cosine similarity.
            const clusters = clusterFacts(allDecrypted, getConsolidationThreshold());

            if (clusters.length === 0) {
              return {
                content: [{ type: 'text', text: `Scanned ${allDecrypted.length} memories — no near-duplicates found.` }],
              };
            }

            // 3. Build report.
            const totalDuplicates = clusters.reduce((sum, c) => sum + c.duplicates.length, 0);
            const reportLines: string[] = [
              `Scanned ${allDecrypted.length} memories.`,
              `Found ${clusters.length} cluster(s) with ${totalDuplicates} duplicate(s).`,
              '',
            ];

            const displayClusters = clusters.slice(0, 10);
            for (let i = 0; i < displayClusters.length; i++) {
              const cluster = displayClusters[i];
              reportLines.push(`Cluster ${i + 1}: KEEP "${cluster.representative.text.slice(0, 80)}…"`);
              for (const dup of cluster.duplicates) {
                reportLines.push(`  - REMOVE "${dup.text.slice(0, 80)}…" (ID: ${dup.id})`);
              }
            }
            if (clusters.length > 10) {
              reportLines.push(`... and ${clusters.length - 10} more cluster(s).`);
            }

            // 4. If not dry_run, batch-delete duplicates.
            if (!dryRun) {
              const idsToDelete = clusters.flatMap((c) => c.duplicates.map((d) => d.id));
              const BATCH_SIZE = 500;
              let totalDeleted = 0;

              for (let i = 0; i < idsToDelete.length; i += BATCH_SIZE) {
                const batch = idsToDelete.slice(i, i + BATCH_SIZE);
                const deleted = await apiClient.batchDelete(batch, authKeyHex);
                totalDeleted += deleted;
              }

              reportLines.push('');
              reportLines.push(`Deleted ${totalDeleted} duplicate memories.`);
            } else {
              reportLines.push('');
              reportLines.push('DRY RUN — no memories were deleted. Run without dry_run to apply.');
            }

            return {
              content: [{ type: 'text', text: reportLines.join('\n') }],
              details: {
                scanned: allDecrypted.length,
                clusters: clusters.length,
                duplicates: totalDuplicates,
                dry_run: dryRun,
              },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_consolidate failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to consolidate memories: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_consolidate' },
    );

    // ---------------------------------------------------------------
    // Helper: build PinOpDeps bound to the live plugin state
    // ---------------------------------------------------------------
    // Wires the pure pin/unpin operation to the managed-service transport +
    // crypto layer. Mirrors MCP's buildPinDepsFromState and Python's
    // _change_claim_status argument plumbing.
    const buildPinDeps = (): PinOpDeps => {
      const owner = subgraphOwner || userId || '';
      const config = {
        ...getSubgraphConfig(),
        authKeyHex: authKeyHex!,
        walletAddress: subgraphOwner ?? undefined,
      };
      return {
        owner,
        sourceAgent: 'openclaw-plugin',
        fetchFactById: (factId: string) => fetchFactById(owner, factId, authKeyHex!),
        decryptBlob: (hex: string) => decryptFromHex(hex, encryptionKey!),
        encryptBlob: (plaintext: string) => encryptToHex(plaintext, encryptionKey!),
        submitBatch: async (payloads: Buffer[]) => {
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
            const hasher = getLSHHasher(api.logger);
            if (hasher) lshIndices = hasher.hash(embedding);
            encryptedEmbedding = encryptToHex(JSON.stringify(embedding), encryptionKey!);
          } catch {
            // Best-effort: word + entity trapdoors alone still surface the claim.
          }
          const entityTrapdoors = entityNames.map((n) => computeEntityTrapdoor(n));
          return {
            blindIndices: [...wordIndices, ...lshIndices, ...entityTrapdoors],
            encryptedEmbedding,
          };
        },
      };
    };

    // ---------------------------------------------------------------
    // Tool: totalreclaw_pin
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_pin',
        label: 'Pin',
        description:
          'Pin a memory so the auto-resolution engine will never override or supersede it. ' +
          "Use when the user explicitly confirms a claim is still valid after you or another agent " +
          "tried to retract/contradict it (e.g. 'wait, I still use Vim sometimes'). " +
          'Takes fact_id (from a prior recall result). Pinning is idempotent — pinning an already-pinned ' +
          'claim is a no-op. Cross-device: the pin propagates via the on-chain supersession chain.',
        parameters: {
          type: 'object',
          properties: {
            fact_id: {
              type: 'string',
              description: 'The ID of the fact to pin (from a totalreclaw_recall result).',
            },
            reason: {
              type: 'string',
              description: 'Optional human-readable reason for pinning (logged locally for tuning).',
            },
          },
          required: ['fact_id'],
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: Record<string, unknown>) {
          try {
            await requireFullSetup(api.logger);
            if (!isSubgraphMode()) {
              return {
                content: [{
                  type: 'text',
                  text: 'Pin/unpin is only supported with the managed service. Self-hosted mode does not yet implement the status-flip supersession flow.',
                }],
              };
            }
            const validation = validatePinArgs(params);
            if (!validation.ok) {
              return { content: [{ type: 'text', text: validation.error }] };
            }
            const deps = buildPinDeps();
            const result = await executePinOperation(validation.factId, 'pinned', deps, validation.reason);
            if (result.success && result.idempotent) {
              api.logger.info(`totalreclaw_pin: ${result.fact_id} already pinned (no-op)`);
              return {
                content: [{ type: 'text', text: `Memory ${result.fact_id} is already pinned.` }],
                details: result,
              };
            }
            if (result.success) {
              api.logger.info(`totalreclaw_pin: ${result.fact_id} → ${result.new_fact_id} (tx ${result.tx_hash?.slice(0, 10)})`);
              return {
                content: [{
                  type: 'text',
                  text: `Pinned memory ${result.fact_id}. New fact id: ${result.new_fact_id} (tx: ${result.tx_hash}).`,
                }],
                details: result,
              };
            }
            api.logger.error(`totalreclaw_pin failed: ${result.error}`);
            return {
              content: [{ type: 'text', text: `Failed to pin memory: ${humanizeError(result.error ?? 'unknown error')}` }],
              details: result,
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_pin failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to pin memory: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_pin' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_unpin
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_unpin',
        label: 'Unpin',
        description:
          'Remove the pin from a previously pinned memory, returning it to active status so the ' +
          'auto-resolution engine can supersede or retract it again. Takes fact_id. Idempotent — ' +
          'unpinning a non-pinned claim is a no-op.',
        parameters: {
          type: 'object',
          properties: {
            fact_id: {
              type: 'string',
              description: 'The ID of the fact to unpin (from a totalreclaw_recall result).',
            },
          },
          required: ['fact_id'],
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: Record<string, unknown>) {
          try {
            await requireFullSetup(api.logger);
            if (!isSubgraphMode()) {
              return {
                content: [{
                  type: 'text',
                  text: 'Pin/unpin is only supported with the managed service. Self-hosted mode does not yet implement the status-flip supersession flow.',
                }],
              };
            }
            const validation = validatePinArgs(params);
            if (!validation.ok) {
              return { content: [{ type: 'text', text: validation.error }] };
            }
            const deps = buildPinDeps();
            const result = await executePinOperation(validation.factId, 'active', deps);
            if (result.success && result.idempotent) {
              api.logger.info(`totalreclaw_unpin: ${result.fact_id} already active (no-op)`);
              return {
                content: [{ type: 'text', text: `Memory ${result.fact_id} is not pinned.` }],
                details: result,
              };
            }
            if (result.success) {
              api.logger.info(`totalreclaw_unpin: ${result.fact_id} → ${result.new_fact_id} (tx ${result.tx_hash?.slice(0, 10)})`);
              return {
                content: [{
                  type: 'text',
                  text: `Unpinned memory ${result.fact_id}. New fact id: ${result.new_fact_id} (tx: ${result.tx_hash}).`,
                }],
                details: result,
              };
            }
            api.logger.error(`totalreclaw_unpin failed: ${result.error}`);
            return {
              content: [{ type: 'text', text: `Failed to unpin memory: ${humanizeError(result.error ?? 'unknown error')}` }],
              details: result,
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_unpin failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to unpin memory: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_unpin' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_import_from
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_import_from',
        label: 'Import From',
        description:
          'Import memories from other AI memory tools (Mem0, MCP Memory Server, ChatGPT, Claude, Gemini, MemoClaw, or generic JSON/CSV). ' +
          'Provide the source name and either an API key, file content, or file path. ' +
          'Use dry_run=true to preview before importing. Idempotent — safe to run multiple times.',
        parameters: {
          type: 'object',
          properties: {
            source: {
              type: 'string',
              enum: ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini', 'memoclaw', 'generic-json', 'generic-csv'],
              description: 'The source system to import from (gemini: Google Takeout HTML; chatgpt: conversations.json or memory text; claude: memory text)',
            },
            api_key: {
              type: 'string',
              description: 'API key for the source system (used once, never stored)',
            },
            source_user_id: {
              type: 'string',
              description: 'User or agent ID in the source system',
            },
            content: {
              type: 'string',
              description: 'File content (JSON, JSONL, or CSV)',
            },
            file_path: {
              type: 'string',
              description: 'Path to the file on disk',
            },
            namespace: {
              type: 'string',
              description: 'Target namespace (default: "imported")',
            },
            dry_run: {
              type: 'boolean',
              description: 'Preview without importing',
            },
          },
          required: ['source'],
        },
        async execute(_toolCallId: string, params: Record<string, unknown>) {
          try {
            await requireFullSetup(api.logger);
            return handlePluginImportFrom(params, api.logger);
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            return { error: message };
          }
        },
      },
      { name: 'totalreclaw_import_from' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_import_batch
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_import_batch',
        label: 'Import Batch',
        description:
          'Process one batch of a large import. Call repeatedly with increasing offset until is_complete=true.',
        parameters: {
          type: 'object',
          properties: {
            source: {
              type: 'string',
              enum: ['gemini', 'chatgpt', 'claude'],
              description: 'Source format',
            },
            file_path: {
              type: 'string',
              description: 'Path to source file',
            },
            content: {
              type: 'string',
              description: 'File content (text sources)',
            },
            offset: {
              type: 'number',
              description: 'Starting chunk index (0-based)',
            },
            batch_size: {
              type: 'number',
              description: 'Chunks per call (default 25)',
            },
          },
          required: ['source'],
        },
        async execute(_toolCallId: string, params: Record<string, unknown>) {
          try {
            await requireFullSetup(api.logger);
            return handleBatchImport(params, api.logger);
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            return { error: message };
          }
        },
      },
      { name: 'totalreclaw_import_batch' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_upgrade
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_upgrade',
        label: 'Upgrade to Pro',
        description:
          'Upgrade to TotalReclaw Pro for unlimited encrypted memories. ' +
          'Returns a Stripe checkout URL for the user to complete payment via credit/debit card.',
        parameters: {
          type: 'object',
          properties: {},
          additionalProperties: false,
        },
        async execute() {
          try {
            await requireFullSetup(api.logger);

            if (!authKeyHex) {
              return {
                content: [{ type: 'text', text: 'Auth credentials are not available. Please initialize first.' }],
              };
            }

            const serverUrl = CONFIG.serverUrl;
            const walletAddr = subgraphOwner || userId || '';

            if (!walletAddr) {
              return {
                content: [{ type: 'text', text: 'Wallet address not available. Please ensure the plugin is fully initialized.' }],
              };
            }

            const response = await fetch(`${serverUrl}/v1/billing/checkout`, {
              method: 'POST',
              headers: {
                'Authorization': `Bearer ${authKeyHex}`,
                'Content-Type': 'application/json',
                'X-TotalReclaw-Client': 'openclaw-plugin',
              },
              body: JSON.stringify({
                wallet_address: walletAddr,
                tier: 'pro',
              }),
            });

            if (!response.ok) {
              const body = await response.text().catch(() => '');
              return {
                content: [{ type: 'text', text: `Failed to create checkout session (HTTP ${response.status}): ${body || response.statusText}` }],
              };
            }

            const data = await response.json() as { checkout_url?: string };

            if (!data.checkout_url) {
              return {
                content: [{ type: 'text', text: 'Failed to create checkout session: no checkout URL returned.' }],
              };
            }

            return {
              content: [{ type: 'text', text: `Open this URL to upgrade to Pro: ${data.checkout_url}` }],
              details: { checkout_url: data.checkout_url },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_upgrade failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Failed to create checkout session: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_upgrade' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_migrate
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_migrate',
        label: 'Migrate Testnet to Mainnet',
        description:
          'Migrate memories from testnet (Base Sepolia) to mainnet (Gnosis) after upgrading to Pro. ' +
          'Dry-run by default — set confirm=true to execute. Idempotent: re-running skips already-migrated facts.',
        parameters: {
          type: 'object',
          properties: {
            confirm: {
              type: 'boolean',
              description: 'Set to true to execute the migration. Without it, returns a dry-run preview.',
              default: false,
            },
          },
          additionalProperties: false,
        },
        async execute(_params: { confirm?: boolean }) {
          try {
            await requireFullSetup(api.logger);

            if (!authKeyHex || !subgraphOwner) {
              return {
                content: [{ type: 'text', text: 'Plugin not fully initialized. Ensure TOTALRECLAW_RECOVERY_PHRASE is set.' }],
              };
            }

            if (!isSubgraphMode()) {
              return {
                content: [{ type: 'text', text: 'Migration is only available with the managed service (subgraph mode).' }],
              };
            }

            const confirm = _params?.confirm === true;
            const serverUrl = CONFIG.serverUrl;

            // 1. Check billing tier
            const billingResp = await fetch(
              `${serverUrl}/v1/billing/status?wallet_address=${encodeURIComponent(subgraphOwner)}`,
              {
                method: 'GET',
                headers: {
                  'Authorization': `Bearer ${authKeyHex}`,
                  'Content-Type': 'application/json',
                  'X-TotalReclaw-Client': 'openclaw-plugin',
                },
              },
            );
            if (!billingResp.ok) {
              return { content: [{ type: 'text', text: `Failed to check billing tier (HTTP ${billingResp.status}).` }] };
            }
            const billingData = await billingResp.json() as { tier: string };
            if (billingData.tier !== 'pro') {
              return {
                content: [{ type: 'text', text: 'Migration requires Pro tier. Use totalreclaw_upgrade to upgrade first.' }],
              };
            }

            // 2. Fetch testnet facts via relay (chain=testnet query param)
            const testnetSubgraphUrl = `${serverUrl}/v1/subgraph?chain=testnet`;
            const mainnetSubgraphUrl = `${serverUrl}/v1/subgraph`;

            api.logger.info('Fetching testnet facts...');
            const testnetFacts = await fetchAllFactsByOwner(testnetSubgraphUrl, subgraphOwner, authKeyHex);

            if (testnetFacts.length === 0) {
              return {
                content: [{ type: 'text', text: 'No facts found on testnet. Nothing to migrate.' }],
              };
            }

            // 3. Check mainnet for existing facts (idempotency)
            api.logger.info('Checking mainnet for existing facts...');
            const mainnetFps = await fetchContentFingerprintsByOwner(mainnetSubgraphUrl, subgraphOwner, authKeyHex);
            const factsToMigrate = testnetFacts.filter(f => !f.contentFp || !mainnetFps.has(f.contentFp));
            const alreadyOnMainnet = testnetFacts.length - factsToMigrate.length;

            // 4. Dry-run
            if (!confirm) {
              const msg = factsToMigrate.length === 0
                ? `All ${testnetFacts.length} testnet facts already exist on mainnet. Nothing to migrate.`
                : `Found ${factsToMigrate.length} facts to migrate from testnet to Gnosis mainnet (${alreadyOnMainnet} already on mainnet). Call with confirm=true to proceed.`;
              return {
                content: [{ type: 'text', text: msg }],
                details: {
                  mode: 'dry_run',
                  testnet_facts: testnetFacts.length,
                  already_on_mainnet: alreadyOnMainnet,
                  to_migrate: factsToMigrate.length,
                },
              };
            }

            // 5. Execute migration
            if (factsToMigrate.length === 0) {
              return {
                content: [{ type: 'text', text: `All ${testnetFacts.length} testnet facts already exist on mainnet. Nothing to migrate.` }],
              };
            }

            // Fetch blind indices
            api.logger.info(`Fetching blind indices for ${factsToMigrate.length} facts...`);
            const factIds = factsToMigrate.map(f => f.id);
            const blindIndicesMap = await fetchBlindIndicesByFactIds(testnetSubgraphUrl, factIds, authKeyHex);

            // Build protobuf payloads
            const payloads: Buffer[] = [];
            for (const fact of factsToMigrate) {
              const blobHex = fact.encryptedBlob.startsWith('0x') ? fact.encryptedBlob.slice(2) : fact.encryptedBlob;
              const indices = blindIndicesMap.get(fact.id) || [];
              const factPayload: FactPayload = {
                id: fact.id,
                timestamp: new Date().toISOString(),
                owner: subgraphOwner,
                encryptedBlob: blobHex,
                blindIndices: indices,
                decayScore: parseFloat(fact.decayScore) || 0.5,
                source: fact.source || 'migration',
                contentFp: fact.contentFp || '',
                agentId: fact.agentId || 'openclaw-plugin',
                encryptedEmbedding: fact.encryptedEmbedding || undefined,
                version: PROTOBUF_VERSION_V4,
              };
              payloads.push(encodeFactProtobuf(factPayload));
            }

            // Batch submit (15 per UserOp)
            const BATCH_SIZE = 15;
            const batchConfig = { ...getSubgraphConfig(), authKeyHex: authKeyHex!, walletAddress: subgraphOwner ?? undefined };
            let migrated = 0;
            let failedBatches = 0;

            for (let i = 0; i < payloads.length; i += BATCH_SIZE) {
              const batch = payloads.slice(i, i + BATCH_SIZE);
              const batchNum = Math.floor(i / BATCH_SIZE) + 1;
              const totalBatches = Math.ceil(payloads.length / BATCH_SIZE);
              api.logger.info(`Migrating batch ${batchNum}/${totalBatches} (${batch.length} facts)...`);

              try {
                const result = await submitFactBatchOnChain(batch, batchConfig);
                if (result.success) {
                  migrated += batch.length;
                } else {
                  failedBatches++;
                }
              } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                api.logger.error(`Migration batch ${batchNum} failed: ${msg}`);
                failedBatches++;
              }
            }

            const resultMsg = failedBatches === 0
              ? `Successfully migrated ${migrated} memories from testnet to Gnosis mainnet.`
              : `Migrated ${migrated}/${factsToMigrate.length} memories. ${failedBatches} batch(es) failed — re-run to retry (idempotent).`;

            return {
              content: [{ type: 'text', text: resultMsg }],
              details: {
                mode: 'executed',
                testnet_facts: testnetFacts.length,
                already_on_mainnet: alreadyOnMainnet,
                to_migrate: factsToMigrate.length,
                migrated,
                failed_batches: failedBatches,
              },
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_migrate failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Migration failed: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_migrate' },
    );

    // ---------------------------------------------------------------
    // Tool: totalreclaw_setup
    // ---------------------------------------------------------------

    api.registerTool(
      {
        name: 'totalreclaw_setup',
        label: 'Setup TotalReclaw',
        description:
          'Initialize TotalReclaw with a recovery phrase. Derives encryption keys and registers with the server. ' +
          'Use this during first-time setup instead of setting environment variables — no gateway restart needed.',
        parameters: {
          type: 'object',
          properties: {
            recovery_phrase: {
              type: 'string',
              description: 'Optional 12-word BIP-39 recovery phrase. If not provided, generates a new one automatically. For returning users, pass their existing phrase.',
            },
          },
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: { recovery_phrase?: string }) {
          try {
            let mnemonic = params.recovery_phrase?.trim() || '';

            // Auto-generate if not provided
            if (!mnemonic) {
              const { generateMnemonic } = await import('@scure/bip39');
              const { wordlist } = await import('@scure/bip39/wordlists/english');
              mnemonic = generateMnemonic(wordlist, 128);
              api.logger.info('totalreclaw_setup: generated new BIP-39 mnemonic');
            }

            // Guard: refuse to overwrite existing credentials with a DIFFERENT phrase
            // (prevents data loss when background sessions_spawn workers call setup).
            // Allow re-init with the SAME phrase (handles agent exec → setup flow).
            try {
              const existing = fs.readFileSync(CREDENTIALS_PATH, 'utf-8');
              const creds = JSON.parse(existing);
              if (creds.mnemonic && creds.userId && creds.mnemonic !== mnemonic) {
                api.logger.info('totalreclaw_setup: credentials exist with different mnemonic, refusing to overwrite');
                return {
                  content: [{
                    type: 'text',
                    text: 'TotalReclaw is already set up with an existing recovery phrase. Your encrypted memories are tied to that phrase.\n\n' +
                          'If you intentionally want to start fresh with a NEW phrase (this will make existing memories inaccessible), ' +
                          'delete ~/.totalreclaw/credentials.json first, then call this tool again.',
                  }],
                };
              }
            } catch { /* credentials.json doesn't exist or is corrupted — proceed with setup */ }

            // Basic validation: must be 12 words
            const words = mnemonic.split(/\s+/);
            if (words.length !== 12) {
              return {
                content: [{
                  type: 'text',
                  text: `Error: Recovery phrase must be exactly 12 words (got ${words.length}). Use \`npx @totalreclaw/mcp-server setup\` to generate a valid BIP-39 mnemonic.`,
                }],
              };
            }

            api.logger.info('totalreclaw_setup: initializing with provided recovery phrase');

            // Force re-initialization with the new mnemonic.
            // This derives keys, registers with the server, saves credentials,
            // and sets up LSH/auth — all without a gateway restart.
            await forceReinitialization(mnemonic, api.logger);

            if (needsSetup) {
              return {
                content: [{
                  type: 'text',
                  text: 'Setup failed — could not initialize with the provided recovery phrase. Check the logs for details.',
                }],
              };
            }

            const wasGenerated = !params.recovery_phrase?.trim();
            return {
              content: [{
                type: 'text',
                text: 'TotalReclaw setup complete! Encryption keys derived, server registration confirmed. ' +
                      'You can now use totalreclaw_remember, totalreclaw_recall, and all other tools immediately — no restart needed.\n\n' +
                      (wasGenerated ? `Recovery phrase: ${mnemonic}\n\n` : '') +
                      'From now on, I will automatically remember important things from our conversations and recall relevant context at the start of each session.',
              }],
            };
          } catch (err: unknown) {
            const message = err instanceof Error ? err.message : String(err);
            api.logger.error(`totalreclaw_setup failed: ${message}`);
            return {
              content: [{ type: 'text', text: `Setup failed: ${humanizeError(message)}` }],
            };
          }
        },
      },
      { name: 'totalreclaw_setup' },
    );

    // ---------------------------------------------------------------
    // Hook: before_agent_start
    // ---------------------------------------------------------------

    api.on(
      'before_agent_start',
      async (event: unknown) => {
        try {
          // Prevent cleartext leakage from OpenClaw's native memory system.
          ensureMemoryHeader(api.logger);

          const evt = event as { prompt?: string } | undefined;

          // Skip trivial or missing prompts.
          if (!evt?.prompt || evt.prompt.length < 5) {
            return undefined;
          }

          await ensureInitialized(api.logger);

          // If setup is still needed after init, inject guided onboarding
          if (needsSetup) {
            return {
              prependContext:
                '## TotalReclaw — First-Time Setup\n\n' +
                'TotalReclaw is an end-to-end encrypted memory vault. It remembers things across conversations — preferences, decisions, project context — all encrypted so no one (not even the server) can read your memories.\n\n' +
                '**To get started, ask the user:** "Do you have an existing TotalReclaw recovery phrase you\'d like to restore, or should I generate a new one?"\n\n' +
                '**If new user:** Run `npx @totalreclaw/mcp-server setup` to generate a cryptographically valid recovery phrase. Do NOT generate the phrase yourself — LLMs cannot compute BIP-39 checksums. The command will output the phrase. Then call the `totalreclaw_setup` tool with the phrase. Display the phrase prominently and warn:\n' +
                '- "This is your ONLY way to access your memories. Write it down and store it securely."\n' +
                '- "NEVER use a recovery phrase from an existing crypto wallet — keep this separate from any wallet that holds funds."\n' +
                '- "If you lose this phrase, your memories are gone forever — there is no recovery."\n\n' +
                '**If returning user:** Ask them to provide their 12-word phrase, then call `totalreclaw_setup` with that phrase.\n\n' +
                '**IMPORTANT:** Use the `totalreclaw_setup` tool to complete setup. This initializes encryption keys immediately — no gateway restart needed. The user can continue chatting right away.\n\n' +
                '**After setup:** Explain that from now on, you will automatically remember important things from conversations and recall relevant memories at the start of each session. The user can also explicitly ask you to remember, recall, forget, or export memories.',
            };
          }

          // One-time welcome message (first conversation after setup or returning user)
          let welcomeBack = '';
          if (welcomeBackMessage) {
            welcomeBack = `\n\n${welcomeBackMessage}`;
            welcomeBackMessage = null; // Consume — only show once
          } else if (firstRunAfterInit) {
            // First conversation with a configured user — explain what's happening
            firstRunAfterInit = false;
            const cache = readBillingCache();
            const tier = cache?.tier || 'free';
            const tierInfo = tier === 'pro'
              ? 'You are on the **Pro** tier — unlimited memories, permanently stored on Gnosis mainnet.'
              : 'You are on the **Free** tier — memories stored on testnet. Use the totalreclaw_upgrade tool to upgrade to Pro for permanent on-chain storage.';
            welcomeBack = `\n\nTotalReclaw is active. I will automatically remember important things from our conversations and recall relevant context at the start of each session. ${tierInfo}`;
          }

          // Billing cache check — warn if quota is approaching limit.
          let billingWarning = '';
          try {
            let cache = readBillingCache();
            if (!cache && authKeyHex) {
              // Cache is stale or missing — fetch fresh billing status.
              const billingUrl = CONFIG.serverUrl;
              const walletParam = encodeURIComponent(subgraphOwner || userId || '');
              const billingResp = await fetch(`${billingUrl}/v1/billing/status?wallet_address=${walletParam}`, {
                method: 'GET',
                headers: { 'Authorization': `Bearer ${authKeyHex}`, 'Accept': 'application/json', 'X-TotalReclaw-Client': 'openclaw-plugin' },
              });
              if (billingResp.ok) {
                const billingData = await billingResp.json() as Record<string, unknown>;
                cache = {
                  tier: (billingData.tier as string) || 'free',
                  free_writes_used: (billingData.free_writes_used as number) ?? 0,
                  free_writes_limit: (billingData.free_writes_limit as number) ?? 0,
                  features: billingData.features as BillingCache['features'] | undefined,
                  checked_at: Date.now(),
                };
                writeBillingCache(cache);
              }
            }
            if (cache && cache.free_writes_limit > 0) {
              const usageRatio = cache.free_writes_used / cache.free_writes_limit;
              if (usageRatio >= QUOTA_WARNING_THRESHOLD) {
                billingWarning = `\n\nTotalReclaw quota warning: ${cache.free_writes_used}/${cache.free_writes_limit} writes used this month (${Math.round(usageRatio * 100)}%). Visit https://totalreclaw.xyz/pricing to upgrade.`;
              }
            }
          } catch {
            // Best-effort — don't block on billing check failure.
          }

          if (isSubgraphMode()) {
            // --- Subgraph mode: digest fast path → hot cache → background refresh ---

            // Digest fast path (Stage 3b). When a digest exists and the mode is
            // not 'off', inject its pre-compiled promptText instead of running
            // the per-query search. A stale digest triggers a background
            // recompile (non-blocking). Failures fall through to the legacy
            // path silently.
            const digestMode = resolveDigestMode();
            logDigestModeOnce(digestMode, api.logger);
            if (digestMode !== 'off' && encryptionKey && authKeyHex && (subgraphOwner || userId)) {
              try {
                const injectResult = await maybeInjectDigest({
                  owner: subgraphOwner || userId!,
                  authKeyHex: authKeyHex!,
                  encryptionKey: encryptionKey!,
                  mode: digestMode,
                  nowMs: Date.now(),
                  loadDeps: {
                    searchSubgraph: async (o, tds, n, a) => searchSubgraph(o, tds, n, a),
                    decryptFromHex: (hex, key) => decryptFromHex(hex, key),
                  },
                  probeDeps: {
                    searchSubgraphBroadened: async (o, n, a) => searchSubgraphBroadened(o, n, a),
                  },
                  recompileFn: (prev) => scheduleDigestRecompile(prev, api.logger),
                  logger: api.logger,
                });
                if (injectResult.promptText) {
                  api.logger.info(`Digest injection: state=${injectResult.state}`);
                  return {
                    prependContext:
                      `## Your Memory\n\n${injectResult.promptText}` + welcomeBack + billingWarning,
                  };
                }
              } catch (err) {
                // Never block session start on digest failure.
                const msg = err instanceof Error ? err.message : String(err);
                api.logger.warn(`Digest fast path failed: ${msg}`);
              }
            }

            // Initialize hot cache if needed.
            if (!pluginHotCache && encryptionKey) {
              const config = getSubgraphConfig();
              pluginHotCache = new PluginHotCache(config.cachePath, encryptionKey.toString('hex'));
              pluginHotCache.load();
            }

            // Try to return cached facts instantly.
            const cachedFacts = pluginHotCache?.getHotFacts() ?? [];

            // Query subgraph in parallel for fresh results.
            // 1. Generate word trapdoors from the user prompt.
            const wordTrapdoors = generateBlindIndices(evt.prompt);

            // 2. Generate query embedding + LSH trapdoors (may fail gracefully).
            let queryEmbedding: number[] | null = null;
            let lshTrapdoors: string[] = [];
            try {
              queryEmbedding = await generateEmbedding(evt.prompt, { isQuery: true });
              const hasher = getLSHHasher(api.logger);
              if (hasher && queryEmbedding) {
                lshTrapdoors = hasher.hash(queryEmbedding);
              }
            } catch {
              // Embedding/LSH failed -- proceed with word-only trapdoors.
            }

            // Two-tier search (C1): if cache is fresh AND query is semantically similar, return cached
            const now = Date.now();
            const cacheAge = now - lastSearchTimestamp;
            if (cacheAge < CACHE_TTL_MS && cachedFacts.length > 0 && queryEmbedding && lastQueryEmbedding) {
              const querySimilarity = cosineSimilarity(queryEmbedding, lastQueryEmbedding);
              if (querySimilarity > SEMANTIC_SKIP_THRESHOLD) {
                const lines = cachedFacts.slice(0, 8).map((f, i) =>
                  `${i + 1}. ${f.text} (importance: ${f.importance}/10, cached)`,
                );
                return { prependContext: `## Relevant Memories\n\n${lines.join('\n')}` + welcomeBack + billingWarning };
              }
            }

            // 3. Merge trapdoors — always include word trapdoors for small-dataset coverage.
            // LSH alone has low collision probability on <100 facts, causing 0 matches.
            const allTrapdoors = [...wordTrapdoors, ...lshTrapdoors];

            // If we have cached facts and no trapdoors, return cached facts.
            if (allTrapdoors.length === 0 && cachedFacts.length > 0) {
              const lines = cachedFacts.slice(0, 8).map((f, i) =>
                `${i + 1}. ${f.text} (importance: ${f.importance}/10, cached)`,
              );
              return { prependContext: `## Relevant Memories\n\n${lines.join('\n')}` + welcomeBack + billingWarning };
            }

            if (allTrapdoors.length === 0) return undefined;

            // 4. Query subgraph for fresh results.
            let subgraphResults: Awaited<ReturnType<typeof searchSubgraph>> = [];
            try {
              const factCount = await getSubgraphFactCount(subgraphOwner || userId!, authKeyHex!);
              const pool = computeCandidatePool(factCount);
              subgraphResults = await searchSubgraph(subgraphOwner || userId!, allTrapdoors, pool, authKeyHex!);
            } catch {
              // Subgraph query failed -- fall back to cached facts if available.
              if (cachedFacts.length > 0) {
                const lines = cachedFacts.slice(0, 8).map((f, i) =>
                  `${i + 1}. ${f.text} (importance: ${f.importance}/10, cached)`,
                );
                return { prependContext: `## Relevant Memories\n\n${lines.join('\n')}` + welcomeBack + billingWarning };
              }
              return undefined;
            }

            // Always run broadened search and merge — ensures vocabulary mismatches
            // (e.g., "preferences" vs "prefer") don't cause recall failures.
            // The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
            try {
              const broadPool = computeCandidatePool(0);
              const broadenedResults = await searchSubgraphBroadened(subgraphOwner || userId!, broadPool, authKeyHex!);
              // Merge broadened results with existing (deduplicate by ID)
              const existingIds = new Set(subgraphResults.map(r => r.id));
              for (const br of broadenedResults) {
                if (!existingIds.has(br.id)) {
                  subgraphResults.push(br);
                }
              }
            } catch { /* best-effort */ }

            if (subgraphResults.length === 0 && cachedFacts.length === 0) return undefined;

            // If subgraph returned no results but we have cache, use cache.
            if (subgraphResults.length === 0) {
              const lines = cachedFacts.slice(0, 8).map((f, i) =>
                `${i + 1}. ${f.text} (importance: ${f.importance}/10, cached)`,
              );
              return { prependContext: `## Relevant Memories\n\n${lines.join('\n')}` + welcomeBack + billingWarning };
            }

            // 5. Decrypt subgraph results and build reranker input.
            const rerankerCandidates: RerankerCandidate[] = [];
            const hookMetaMap = new Map<string, { importance: number; age: string }>();

            for (const result of subgraphResults) {
              try {
                const docJson = decryptFromHex(result.encryptedBlob, encryptionKey!);
                // Filter out digest infrastructure blobs — they have no user
                // text and should never surface in recall results.
                if (isDigestBlob(docJson)) continue;
                const doc = readClaimFromBlob(docJson);

                let decryptedEmbedding: number[] | undefined;
                if (result.encryptedEmbedding) {
                  try {
                    decryptedEmbedding = JSON.parse(
                      decryptFromHex(result.encryptedEmbedding, encryptionKey!),
                    );
                  } catch {
                    // Embedding decryption failed -- proceed without it.
                  }
                }

                const createdAtSec = result.timestamp ? parseInt(result.timestamp, 10) : undefined;
                rerankerCandidates.push({
                  id: result.id,
                  text: doc.text,
                  embedding: decryptedEmbedding,
                  importance: doc.importance / 10,
                  createdAt: createdAtSec,
                  source: typeof doc.metadata?.source === 'string' ? doc.metadata.source : undefined,
                });

                hookMetaMap.set(result.id, {
                  importance: doc.importance,
                  age: 'subgraph',
                  category: doc.category,
                });
              } catch {
                // Skip un-decryptable candidates.
              }
            }

            // 6. Re-rank with BM25 + cosine + intent-weighted RRF fusion.
            const hookQueryIntent = detectQueryIntent(evt.prompt);
            const reranked = rerank(
              evt.prompt,
              queryEmbedding ?? [],
              rerankerCandidates,
              8,
              INTENT_WEIGHTS[hookQueryIntent],
              /* applySourceWeights (Retrieval v2 Tier 1) */ true,
            );

            // Update hot cache with reranked results.
            try {
              if (pluginHotCache) {
                const hotFacts: HotFact[] = rerankerCandidates.map((c) => {
                  const meta = hookMetaMap.get(c.id);
                  return { id: c.id, text: c.text, importance: meta?.importance ?? 5 };
                });
                pluginHotCache.setHotFacts(hotFacts);
                pluginHotCache.setLastQueryEmbedding(queryEmbedding);
                pluginHotCache.flush();
              }
            } catch {
              // Hot cache update is best-effort.
            }

            // Record search state for two-tier cache (C1).
            lastSearchTimestamp = Date.now();
            lastQueryEmbedding = queryEmbedding;

            if (reranked.length === 0) return undefined;

            // 6b. Cosine similarity threshold gate — skip injection when the
            //     best match is below the minimum relevance threshold.
            const hookMaxCosine = Math.max(
              ...reranked.map((r) => r.cosineSimilarity ?? 0),
            );
            if (hookMaxCosine < COSINE_THRESHOLD) {
              api.logger.info(
                `Hook: cosine threshold gate filtered results (max=${hookMaxCosine.toFixed(3)}, threshold=${COSINE_THRESHOLD})`,
              );
              return undefined;
            }

            // 7. Build context string.
            const lines = reranked.map((m, i) => {
              const meta = hookMetaMap.get(m.id);
              const importance = meta?.importance ?? 5;
              const age = meta?.age ?? '';
              const typeTag = meta?.category ? `[${meta.category}] ` : '';
              return `${i + 1}. ${typeTag}${m.text} (importance: ${importance}/10, ${age})`;
            });
            const contextString = `## Relevant Memories\n\n${lines.join('\n')}`;

            return { prependContext: contextString + welcomeBack + billingWarning };
          }

          // --- Server mode (existing behavior) ---

          // 1. Generate word trapdoors from the user prompt.
          const wordTrapdoors = generateBlindIndices(evt.prompt);

          // 2. Generate query embedding + LSH trapdoors (may fail gracefully).
          let queryEmbedding: number[] | null = null;
          let lshTrapdoors: string[] = [];
          try {
            queryEmbedding = await generateEmbedding(evt.prompt, { isQuery: true });
            const hasher = getLSHHasher(api.logger);
            if (hasher && queryEmbedding) {
              lshTrapdoors = hasher.hash(queryEmbedding);
            }
          } catch {
            // Embedding/LSH failed -- proceed with word-only trapdoors.
          }

          // 3. Merge word + LSH trapdoors.
          const allTrapdoors = [...wordTrapdoors, ...lshTrapdoors];
          if (allTrapdoors.length === 0) return undefined;

          // 4. Fetch candidates from the server (dynamic pool sizing).
          const factCount = await getFactCount(api.logger);
          const pool = computeCandidatePool(factCount);
          const candidates = await apiClient!.search(
            userId!,
            allTrapdoors,
            pool,
            authKeyHex!,
          );

          if (candidates.length === 0) return undefined;

          // 5. Decrypt candidates (text + embeddings) and build reranker input.
          const rerankerCandidates: RerankerCandidate[] = [];
          const hookMetaMap = new Map<string, { importance: number; age: string }>();

          for (const candidate of candidates) {
            try {
              const docJson = decryptFromHex(candidate.encrypted_blob, encryptionKey!);
              // Skip digest infrastructure blobs.
              if (isDigestBlob(docJson)) continue;
              const doc = readClaimFromBlob(docJson);

              let decryptedEmbedding: number[] | undefined;
              if (candidate.encrypted_embedding) {
                try {
                  decryptedEmbedding = JSON.parse(
                    decryptFromHex(candidate.encrypted_embedding, encryptionKey!),
                  );
                } catch {
                  // Embedding decryption failed -- proceed without it.
                }
              }

              const createdAtSec = typeof candidate.timestamp === 'number'
                ? candidate.timestamp / 1000
                : new Date(candidate.timestamp).getTime() / 1000;
              rerankerCandidates.push({
                id: candidate.fact_id,
                text: doc.text,
                embedding: decryptedEmbedding,
                importance: doc.importance / 10,
                createdAt: createdAtSec,
                source: typeof doc.metadata?.source === 'string' ? doc.metadata.source : undefined,
              });

              hookMetaMap.set(candidate.fact_id, {
                importance: doc.importance,
                age: relativeTime(candidate.timestamp),
              });
            } catch {
              // Skip un-decryptable candidates.
            }
          }

          // 6. Re-rank with BM25 + cosine + RRF fusion (intent-weighted).
          const srvHookIntent = detectQueryIntent(evt.prompt);
          const reranked = rerank(
            evt.prompt,
            queryEmbedding ?? [],
            rerankerCandidates,
            8,
            INTENT_WEIGHTS[srvHookIntent],
            /* applySourceWeights (Retrieval v2 Tier 1) */ true,
            );

          if (reranked.length === 0) return undefined;

          // Cosine similarity threshold gate — skip injection when the
          // best match is below the minimum relevance threshold.
          const srvMaxCosine = Math.max(
            ...reranked.map((r) => r.cosineSimilarity ?? 0),
          );
          if (srvMaxCosine < COSINE_THRESHOLD) {
            api.logger.info(
              `Hook: cosine threshold gate filtered results (max=${srvMaxCosine.toFixed(3)}, threshold=${COSINE_THRESHOLD})`,
            );
            return undefined;
          }

          // 7. Build context string.
          const lines = reranked.map((m, i) => {
            const meta = hookMetaMap.get(m.id);
            const importance = meta?.importance ?? 5;
            const age = meta?.age ?? '';
            return `${i + 1}. ${m.text} (importance: ${importance}/10, ${age})`;
          });
          const contextString = `## Relevant Memories\n\n${lines.join('\n')}`;

          return { prependContext: contextString + welcomeBack + billingWarning };
        } catch (err: unknown) {
          // The hook must NEVER throw -- log and return undefined.
          const message = err instanceof Error ? err.message : String(err);
          api.logger.warn(`before_agent_start hook failed: ${message}`);
          return undefined;
        }
      },
      { priority: 10 },
    );

    // ---------------------------------------------------------------
    // Hook: agent_end — auto-extract facts after each conversation turn
    // ---------------------------------------------------------------

    api.on(
      'agent_end',
      async (event: unknown) => {
        // CRITICAL: Always return { memoryHandled: true } so OpenClaw's default
        // memory system does NOT fall back to writing plaintext MEMORY.md.
        // Losing facts on error is acceptable; leaking them in cleartext is not.
        try {
          // Defensive: ensure MEMORY.md header is present so OpenClaw's default
          // memory system doesn't write sensitive data in cleartext, even if
          // our extraction fails below.
          ensureMemoryHeader(api.logger);

          // BUG-2 fix: skip extraction if an import was in progress this turn.
          // Import failures were retriggering agent_end → extraction → import loops.
          if (_importInProgress) {
            _importInProgress = false; // auto-reset for next turn
            api.logger.info('agent_end: skipping extraction (import was in progress)');
            return { memoryHandled: true };
          }

          const evt = event as { messages?: unknown[]; success?: boolean } | undefined;
          if (!evt?.messages || evt.messages.length < 2) {
            api.logger.info('agent_end: skipping extraction (no messages)');
            return { memoryHandled: true };
          }
          // Proceed with extraction even when evt.success is false or undefined.
          // A single LLM timeout on one turn should not prevent extraction of
          // facts from the (potentially many) successful turns in the message
          // history. The extractor processes the full message array and can
          // extract valuable facts from content before the failure.
          if (evt.success === false) {
            api.logger.info('agent_end: turn reported failure, but proceeding with extraction from message history');
          }

          await ensureInitialized(api.logger);
          if (needsSetup) return { memoryHandled: true };

          // C3: Throttle auto-extraction to every N turns (configurable via env).
          // Phase 2.2.5: every branch of the extraction pipeline now logs its
          // outcome. Prior to 2.2.5, only the "stored N facts" happy path
          // produced a log line, so silent JSON parse failures / chatCompletion
          // timeouts / importance-filter-drops-everything scenarios left no
          // trace whatsoever in the gateway log. See the investigation report
          // in CHANGELOG for the full failure chain we uncovered.
          turnsSinceLastExtraction++;
          const extractInterval = getExtractInterval();
          api.logger.info(
            `agent_end: turn ${turnsSinceLastExtraction}/${extractInterval} (messages=${evt.messages.length})`,
          );
          if (turnsSinceLastExtraction >= extractInterval) {
            const existingMemories = isLlmDedupEnabled()
              ? await fetchExistingMemoriesForExtraction(api.logger, 20, evt.messages)
              : [];
            const rawFacts = await extractFacts(
              evt.messages,
              'turn',
              existingMemories,
              undefined,
              api.logger,
            );
            api.logger.info(
              `agent_end: extractFacts returned ${rawFacts.length} raw facts`,
            );
            const { kept: importanceFiltered, dropped } = filterByImportance(
              rawFacts,
              api.logger,
            );
            api.logger.info(
              `agent_end: after importance filter: kept=${importanceFiltered.length}, dropped=${dropped}`,
            );
            const maxFacts = getMaxFactsPerExtraction();
            if (importanceFiltered.length > maxFacts) {
              api.logger.info(
                `Capped extraction from ${importanceFiltered.length} to ${maxFacts} facts`,
              );
            }
            const facts = importanceFiltered.slice(0, maxFacts);
            if (facts.length > 0) {
              await storeExtractedFacts(facts, api.logger);
              api.logger.info(`agent_end: stored ${facts.length} facts to encrypted vault`);
            } else {
              // Phase 2.2.5: no longer silent when extraction produces nothing.
              api.logger.info(
                `agent_end: extraction produced 0 storable facts (raw=${rawFacts.length}, after-importance=${importanceFiltered.length})`,
              );
            }
            turnsSinceLastExtraction = 0;
          }
        } catch (err: unknown) {
          const message = err instanceof Error ? err.message : String(err);
          api.logger.error(`agent_end extraction failed: ${message}`);
          // Re-assert MEMORY.md header even on failure — last line of defense.
          ensureMemoryHeader(api.logger);
        }
        // Always signal that memory is handled — prevent plaintext fallback.
        return { memoryHandled: true };
      },
      { priority: 90 },
    );

    // ---------------------------------------------------------------
    // Hook: before_compaction — extract ALL facts before context is lost
    // ---------------------------------------------------------------

    api.on(
      'before_compaction',
      async (event: unknown) => {
        try {
          const evt = event as { messages?: unknown[]; messageCount?: number } | undefined;
          if (!evt?.messages || evt.messages.length < 2) return;

          await ensureInitialized(api.logger);
          if (needsSetup) return;

          api.logger.info(
            `pre_compaction: using compaction-aware extraction (importance >= 5), processing ${evt.messages.length} messages`,
          );

          const existingMemories = isLlmDedupEnabled()
            ? await fetchExistingMemoriesForExtraction(api.logger, 50, evt.messages)
            : [];
          const rawCompactFacts = await extractFactsForCompaction(evt.messages, existingMemories, api.logger);
          const { kept: compactImportanceFiltered } = filterByImportance(rawCompactFacts, api.logger);
          const maxFactsCompact = getMaxFactsPerExtraction();
          if (compactImportanceFiltered.length > maxFactsCompact) {
            api.logger.info(
              `Capped compaction extraction from ${compactImportanceFiltered.length} to ${maxFactsCompact} facts`,
            );
          }
          const facts = compactImportanceFiltered.slice(0, maxFactsCompact);
          if (facts.length > 0) {
            await storeExtractedFacts(facts, api.logger);
          }
          turnsSinceLastExtraction = 0; // Reset C3 counter on compaction.

          // Session debrief — after regular extraction.
          // v1 mapping: DebriefItem { type: 'summary'|'context' } →
          //   v1 type 'summary' (always, since context → claim would lose
          //   the "this is a session summary" signal) + source 'derived'
          //   (session debrief is a derived synthesis by definition).
          try {
            const storedTexts = facts.map((f) => f.text);
            const debriefItems = await extractDebrief(evt.messages, storedTexts);
            if (debriefItems.length > 0) {
              const debriefFacts: ExtractedFact[] = debriefItems.map((d) => ({
                text: d.text,
                type: 'summary' as MemoryType,
                source: 'derived' as MemorySource,
                importance: d.importance,
                action: 'ADD' as const,
              }));
              await storeExtractedFacts(debriefFacts, api.logger, 'openclaw_debrief');
              api.logger.info(`Session debrief: stored ${debriefItems.length} items`);
            }
          } catch (debriefErr: unknown) {
            api.logger.warn(`before_compaction debrief failed: ${debriefErr instanceof Error ? debriefErr.message : String(debriefErr)}`);
          }
        } catch (err: unknown) {
          const message = err instanceof Error ? err.message : String(err);
          api.logger.warn(`before_compaction extraction failed: ${message}`);
        }
      },
      { priority: 5 },
    );

    // ---------------------------------------------------------------
    // Hook: before_reset — final extraction before session is cleared
    // ---------------------------------------------------------------

    api.on(
      'before_reset',
      async (event: unknown) => {
        try {
          const evt = event as { messages?: unknown[]; reason?: string } | undefined;
          if (!evt?.messages || evt.messages.length < 2) return;

          await ensureInitialized(api.logger);
          if (needsSetup) return;

          api.logger.info(
            `Pre-reset extraction (${evt.reason ?? 'unknown'}): processing ${evt.messages.length} messages`,
          );

          const existingMemories = isLlmDedupEnabled()
            ? await fetchExistingMemoriesForExtraction(api.logger, 50, evt.messages)
            : [];
          const rawResetFacts = await extractFacts(evt.messages, 'full', existingMemories);
          const { kept: resetImportanceFiltered } = filterByImportance(rawResetFacts, api.logger);
          const maxFactsReset = getMaxFactsPerExtraction();
          if (resetImportanceFiltered.length > maxFactsReset) {
            api.logger.info(
              `Capped reset extraction from ${resetImportanceFiltered.length} to ${maxFactsReset} facts`,
            );
          }
          const facts = resetImportanceFiltered.slice(0, maxFactsReset);
          if (facts.length > 0) {
            await storeExtractedFacts(facts, api.logger);
          }
          turnsSinceLastExtraction = 0; // Reset C3 counter on reset.

          // Session debrief — after regular extraction.
          // v1 mapping: DebriefItem { type: 'summary'|'context' } →
          //   v1 type 'summary' (always, since context → claim would lose
          //   the "this is a session summary" signal) + source 'derived'
          //   (session debrief is a derived synthesis by definition).
          try {
            const storedTexts = facts.map((f) => f.text);
            const debriefItems = await extractDebrief(evt.messages, storedTexts);
            if (debriefItems.length > 0) {
              const debriefFacts: ExtractedFact[] = debriefItems.map((d) => ({
                text: d.text,
                type: 'summary' as MemoryType,
                source: 'derived' as MemorySource,
                importance: d.importance,
                action: 'ADD' as const,
              }));
              await storeExtractedFacts(debriefFacts, api.logger, 'openclaw_debrief');
              api.logger.info(`Session debrief: stored ${debriefItems.length} items`);
            }
          } catch (debriefErr: unknown) {
            api.logger.warn(`before_reset debrief failed: ${debriefErr instanceof Error ? debriefErr.message : String(debriefErr)}`);
          }
        } catch (err: unknown) {
          const message = err instanceof Error ? err.message : String(err);
          api.logger.warn(`before_reset extraction failed: ${message}`);
        }
      },
      { priority: 5 },
    );
  },
};

export default plugin;

/**
 * Reset all module-level state for test isolation.
 * ONLY call this from test code — never in production.
 */
export function __resetForTesting(): void {
  authKeyHex = null;
  encryptionKey = null;
  dedupKey = null;
  userId = null;
  subgraphOwner = null;
  apiClient = null;
  initPromise = null;
  lshHasher = null;
  lshInitFailed = false;
  masterPasswordCache = null;
  saltCache = null;
  cachedFactCount = null;
  lastFactCountFetch = 0;
  pluginHotCache = null;
  lastSearchTimestamp = 0;
  lastQueryEmbedding = null;
  turnsSinceLastExtraction = 0;
}
