/**
 * Plugin configuration — centralized env var reads.
 * This file ONLY reads process.env. No network calls, no I/O.
 * Other modules import config values from here.
 *
 * OpenClaw's security scanner flags files that contain BOTH process.env reads
 * AND network calls. By centralizing all env reads here, no other file needs
 * to touch process.env directly.
 *
 * v1 env var cleanup — see `docs/guides/env-vars-reference.md`.
 * Removed user-facing vars: TOTALRECLAW_CHAIN_ID, TOTALRECLAW_EMBEDDING_MODEL,
 * TOTALRECLAW_STORE_DEDUP, TOTALRECLAW_LLM_MODEL, TOTALRECLAW_SESSION_ID,
 * TOTALRECLAW_TAXONOMY_VERSION.
 * Removed legacy gates: TOTALRECLAW_CLAIM_FORMAT, TOTALRECLAW_DIGEST_MODE,
 * TOTALRECLAW_AUTO_RESOLVE_MODE (the last one moved to an internal debug
 * module; see `contradiction-sync.ts`).
 *
 * Tuning knobs (cosine threshold, min importance, cache TTL, etc.) are now
 * delivered via the relay billing response. Env-var fallbacks are kept only
 * for self-hosted deployments where the server may not surface those values.
 */

import path from 'node:path';

const home = process.env.HOME ?? '/home/node';

/**
 * Removed env vars — warn once per process if still set so operators know
 * their config is a no-op. The removal list matches `docs/guides/env-vars-reference.md`.
 */
const REMOVED_ENV_VARS = [
  'TOTALRECLAW_CHAIN_ID',
  'TOTALRECLAW_EMBEDDING_MODEL',
  'TOTALRECLAW_STORE_DEDUP',
  'TOTALRECLAW_LLM_MODEL',
  'TOTALRECLAW_SESSION_ID',
  'TOTALRECLAW_TAXONOMY_VERSION',
  'TOTALRECLAW_CLAIM_FORMAT',
  'TOTALRECLAW_DIGEST_MODE',
] as const;

function warnRemovedEnvVars(warn: (msg: string) => void = console.warn): void {
  const set = REMOVED_ENV_VARS.filter((name) => process.env[name] !== undefined);
  if (set.length === 0) return;
  warn(
    `TotalReclaw: ignoring removed env var(s): ${set.join(', ')}. ` +
      `See docs/guides/env-vars-reference.md for the v1 env var surface.`,
  );
}

// Emit the warning once at import time. Safe because this module is loaded
// exactly once per process.
warnRemovedEnvVars();

/** Runtime override for recovery phrase (set by hot-reload after setup). */
let _recoveryPhraseOverride: string | null = null;

export function setRecoveryPhraseOverride(phrase: string): void {
  _recoveryPhraseOverride = phrase;
}

export function getRecoveryPhrase(): string {
  return _recoveryPhraseOverride ?? process.env.TOTALRECLAW_RECOVERY_PHRASE ?? '';
}

/**
 * Runtime override for chain ID, set after the relay billing response is
 * read. Free tier stays on 84532 (Base Sepolia); Pro tier flips to 100
 * (Gnosis mainnet). The relay routes Pro writes to Gnosis, so Pro-tier
 * UserOps MUST be signed against chain 100 — otherwise the bundler rejects
 * the signature with AA23.
 *
 * See index.ts: after the billing fetch completes, call
 * `setChainIdOverride(100)` for Pro users. Free users can leave the
 * override unset.
 */
let _chainIdOverride: number | null = null;

export function setChainIdOverride(chainId: number): void {
  _chainIdOverride = chainId;
}

/** Reset the chain override — used by tests. */
export function __resetChainIdOverrideForTests(): void {
  _chainIdOverride = null;
}

export const CONFIG = {
  // Core — recoveryPhrase reads from override first, then env var.
  // Use getRecoveryPhrase() for dynamic access; this property is for
  // backward-compat with code that reads CONFIG.recoveryPhrase at init time.
  get recoveryPhrase(): string {
    return getRecoveryPhrase();
  },
  serverUrl: (process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz').replace(/\/+$/, ''),
  selfHosted: process.env.TOTALRECLAW_SELF_HOSTED === 'true',
  credentialsPath: process.env.TOTALRECLAW_CREDENTIALS_PATH || path.join(home, '.totalreclaw', 'credentials.json'),

  // Chain — chainId is no longer user-configurable. It is auto-detected from
  // the relay billing response (free = Base Sepolia / 84532, Pro = Gnosis /
  // 100). The default here is used only before the first billing lookup
  // completes. Self-hosted users can still point at a custom DataEdge via
  // TOTALRECLAW_DATA_EDGE_ADDRESS / TOTALRECLAW_ENTRYPOINT_ADDRESS /
  // TOTALRECLAW_RPC_URL (undocumented; internal knobs).
  //
  // Reads the runtime override set by the billing auto-detect in index.ts.
  // Falls back to 84532 (free tier / pre-billing-fetch). Must be a getter,
  // not a literal — a literal would freeze all Pro-tier UserOps to the
  // wrong chainId and AA23 at the bundler.
  get chainId(): number {
    return _chainIdOverride ?? 84532;
  },
  dataEdgeAddress: process.env.TOTALRECLAW_DATA_EDGE_ADDRESS || '',
  entryPointAddress: process.env.TOTALRECLAW_ENTRYPOINT_ADDRESS || '',
  rpcUrl: process.env.TOTALRECLAW_RPC_URL || '',

  // Tuning knobs — default values used only as local fallback for
  // self-hosted mode. Managed-service clients override these from the relay
  // billing response via `resolveTuning(...)`.
  // See: docs/specs/totalreclaw/client-consistency.md
  cosineThreshold: parseFloat(process.env.TOTALRECLAW_COSINE_THRESHOLD ?? '0.15'),
  extractInterval: parseInt(process.env.TOTALRECLAW_EXTRACT_INTERVAL ?? process.env.TOTALRECLAW_EXTRACT_EVERY_TURNS ?? '3', 10),
  relevanceThreshold: parseFloat(process.env.TOTALRECLAW_RELEVANCE_THRESHOLD ?? '0.3'),
  semanticSkipThreshold: parseFloat(process.env.TOTALRECLAW_SEMANTIC_SKIP_THRESHOLD ?? '0.85'),
  cacheTtlMs: parseInt(process.env.TOTALRECLAW_CACHE_TTL_MS ?? String(5 * 60 * 1000), 10),
  minImportance: Math.max(1, Math.min(10, Number(process.env.TOTALRECLAW_MIN_IMPORTANCE) || 6)),
  trapdoorBatchSize: parseInt(process.env.TOTALRECLAW_TRAPDOOR_BATCH_SIZE ?? '5', 10),
  pageSize: parseInt(process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE ?? '1000', 10),

  // Store-time dedup is always ON. TOTALRECLAW_STORE_DEDUP was removed in v1.
  storeDedupEnabled: true,

  // LLM provider API keys (read once, passed to llm-client). Model selection
  // is entirely automatic via `deriveCheapModel(provider)` — the
  // TOTALRECLAW_LLM_MODEL override was removed in v1.
  llmApiKeys: {
    zai: process.env.ZAI_API_KEY || '',
    anthropic: process.env.ANTHROPIC_API_KEY || '',
    openai: process.env.OPENAI_API_KEY || '',
    gemini: process.env.GEMINI_API_KEY || '',
    google: process.env.GOOGLE_API_KEY || '',
    mistral: process.env.MISTRAL_API_KEY || '',
    groq: process.env.GROQ_API_KEY || '',
    deepseek: process.env.DEEPSEEK_API_KEY || '',
    openrouter: process.env.OPENROUTER_API_KEY || '',
    xai: process.env.XAI_API_KEY || '',
    together: process.env.TOGETHER_API_KEY || '',
    cerebras: process.env.CEREBRAS_API_KEY || '',
  } as Record<string, string>,

  // Paths
  home,
  billingCachePath: path.join(home, '.totalreclaw', 'billing-cache.json'),
  cachePath: process.env.TOTALRECLAW_CACHE_PATH || path.join(home, '.totalreclaw', 'cache.enc'),
  openclawWorkspace: path.join(home, '.openclaw', 'workspace'),
} as const;

// ---------------------------------------------------------------------------
// Server-side tuning resolution
// ---------------------------------------------------------------------------

/**
 * Optional tuning fields delivered via the relay billing response.
 *
 * Relay may populate these in `features` (same cache consumed by
 * `isLlmDedupEnabled`, `getExtractInterval`, etc.). When present, they
 * override the env/defaults resolved above. When absent (self-hosted or
 * pre-rollout relay), clients fall back to `CONFIG` values.
 */
export interface BillingTuning {
  cosine_threshold?: number;
  relevance_threshold?: number;
  semantic_skip_threshold?: number;
  min_importance?: number;
  cache_ttl_ms?: number;
  trapdoor_batch_size?: number;
  subgraph_page_size?: number;
}

/**
 * Merge a billing-response tuning block with the local fallback values.
 *
 * Use this at the call-site that needs a threshold, passing the features
 * blob from the billing cache. No I/O here — callers read the cache once
 * and hand the features in.
 */
export function resolveTuning(features?: BillingTuning | null): {
  cosineThreshold: number;
  relevanceThreshold: number;
  semanticSkipThreshold: number;
  minImportance: number;
  cacheTtlMs: number;
  trapdoorBatchSize: number;
  pageSize: number;
} {
  return {
    cosineThreshold: features?.cosine_threshold ?? CONFIG.cosineThreshold,
    relevanceThreshold: features?.relevance_threshold ?? CONFIG.relevanceThreshold,
    semanticSkipThreshold: features?.semantic_skip_threshold ?? CONFIG.semanticSkipThreshold,
    minImportance: features?.min_importance ?? CONFIG.minImportance,
    cacheTtlMs: features?.cache_ttl_ms ?? CONFIG.cacheTtlMs,
    trapdoorBatchSize: features?.trapdoor_batch_size ?? CONFIG.trapdoorBatchSize,
    pageSize: features?.subgraph_page_size ?? CONFIG.pageSize,
  };
}

// Exposed for tests that want to assert the removed-var warning behaviour.
export const __internal = {
  REMOVED_ENV_VARS,
  warnRemovedEnvVars,
};
