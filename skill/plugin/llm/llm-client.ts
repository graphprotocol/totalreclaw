/**
 * TotalReclaw Plugin - LLM Client
 *
 * Auto-detects the user's LLM provider from OpenClaw's config and derives a
 * cheap extraction model. Supports OpenAI-compatible APIs and Anthropic's
 * Messages API. No external dependencies -- uses native fetch().
 *
 * Embedding generation has been moved to embedding.ts (local ONNX model via
 * @huggingface/transformers). No API key needed for embeddings.
 */

import { CONFIG } from '../config.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

interface ChatCompletionResponse {
  choices: Array<{
    message: {
      content: string | null;
    };
  }>;
}

/** Anthropic Messages API response shape. */
interface AnthropicMessagesResponse {
  content: Array<{
    type: string;
    text?: string;
  }>;
}

export interface LLMClientConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
  apiFormat: 'openai' | 'anthropic';
}

/** Shape of an OpenClaw model provider config entry. */
interface OpenClawProviderConfig {
  baseUrl: string;
  apiKey?: string;
  api?: string;
  models?: Array<{ id: string; [k: string]: unknown }>;
  [k: string]: unknown;
}

// ---------------------------------------------------------------------------
// Provider mappings
// ---------------------------------------------------------------------------

/** Maps provider name to CONFIG.llmApiKeys property names to check (in order). */
const PROVIDER_KEY_NAMES: Record<string, string[]> = {
  zai:        ['zai'],
  anthropic:  ['anthropic'],
  openai:     ['openai'],
  gemini:     ['gemini'],
  google:     ['gemini', 'google'],
  mistral:    ['mistral'],
  groq:       ['groq'],
  deepseek:   ['deepseek'],
  openrouter: ['openrouter'],
  xai:        ['xai'],
  together:   ['together'],
  cerebras:   ['cerebras'],
};

/**
 * zai has TWO public endpoints. The CODING endpoint is what GLM Coding Plan
 * subscription keys are provisioned against; the STANDARD (PAYG) endpoint
 * serves pay-as-you-go balances. A coding-plan key that hits the STANDARD
 * endpoint returns HTTP 429 with body `"Insufficient balance or no resource
 * package. Please recharge."` — misleading because the subscription is in
 * good standing. Vice-versa for PAYG keys that accidentally hit CODING.
 *
 * 3.3.1-rc.3: exported so the rc.3 auto-fallback (see `chatCompletion`)
 * can flip between them when the upstream error signature matches.
 */
export const ZAI_CODING_BASE_URL = 'https://api.z.ai/api/coding/paas/v4';
export const ZAI_STANDARD_BASE_URL = 'https://api.z.ai/api/paas/v4';

/**
 * Resolve the zai base URL.
 *
 * Precedence:
 *   1. `ZAI_BASE_URL` env var (explicit operator override — read by
 *      `CONFIG.zaiBaseUrl` via a getter so tests can mutate the env
 *      between calls)
 *   2. Default: coding endpoint (coding-plan-biased; the rc.3 auto-fallback
 *      hops to the standard endpoint on an "Insufficient balance" 429).
 *
 * Documented in plugin SKILL.md — Coding-Plan users can leave it unset (or
 * set it explicitly to `https://api.z.ai/api/coding/paas/v4`). PAYG users
 * MUST set it to `https://api.z.ai/api/paas/v4` to avoid the auto-fallback
 * tax on every first call.
 *
 * Scanner-isolation note: the env read lives in `config.ts` (which has no
 * network triggers). This module has network calls, so it cannot touch
 * env vars directly — both rules 1 (env-harvesting) and 2 (potential-
 * exfiltration) in check-scanner.mjs would fire.
 */
export function getZaiBaseUrl(): string {
  return CONFIG.zaiBaseUrl;
}

const PROVIDER_BASE_URLS: Record<string, string> = {
  // zai: resolved lazily at each init/call so `ZAI_BASE_URL` env changes
  // propagate without a module re-import. See `getZaiBaseUrl()`.
  zai:        getZaiBaseUrl(),
  anthropic:  'https://api.anthropic.com/v1',
  openai:     'https://api.openai.com/v1',
  gemini:     'https://generativelanguage.googleapis.com/v1beta/openai',
  google:     'https://generativelanguage.googleapis.com/v1beta/openai',
  mistral:    'https://api.mistral.ai/v1',
  groq:       'https://api.groq.com/openai/v1',
  deepseek:   'https://api.deepseek.com/v1',
  openrouter: 'https://openrouter.ai/api/v1',
  xai:        'https://api.x.ai/v1',
  together:   'https://api.together.xyz/v1',
  cerebras:   'https://api.cerebras.ai/v1',
};

// ---------------------------------------------------------------------------
// Cheap model derivation
// ---------------------------------------------------------------------------

const CHEAP_INDICATORS = ['flash', 'mini', 'nano', 'haiku', 'small', 'lite', 'fast'];

/**
 * Regex that tests whether a model id genuinely mentions a "cheap" tier.
 * Uses word-boundary + `-` separators so we do NOT match substrings like
 * "mini" inside "gemini" (real bug caught in 3.3.1 tests — deriveCheapModel
 * was passing gemini-2.5-pro through unchanged because `.includes('mini')`
 * matched the letters inside "gemini"). The canonical cheap-tier naming
 * conventions put the indicator at a hyphen boundary or end of string:
 *   gpt-4.1-mini, claude-haiku-4-5, gemini-flash-lite, glm-4.5-flash, o4-mini
 */
const CHEAP_INDICATOR_RE = new RegExp(
  `(?:^|[-_/.])(?:${CHEAP_INDICATORS.join('|')})(?:[-_/.]|$)`,
  'i',
);

/**
 * LAST-RESORT default cheap extraction model per provider.
 *
 * Pedro directive (2026-07-20, internal#502 follow-up): this table is NOT the
 * production default. The extraction model must be the cheapest of the USER'S
 * OWN configured models (`models.providers.<provider>.models[]`), resolved by
 * `selectCheapestConfiguredModel` — never a baked-in glm/claude/gpt constant.
 * This table is only consulted when no configured model list is available
 * (the harvested auth-profiles key path, the legacy env-var path, or a
 * provider whose `models[]` contains NO cheap-indicator match) — i.e. a
 * best-effort fallback so extraction still has a model id to send. Exported
 * so callers that resolve a provider WITHOUT a model list can still pick one.
 *
 * 3.3.1 update: haiku is now `claude-haiku-4-5-20251001` (latest cheap
 * Claude as of 2026-04). glm-4.5-flash stays the zai FALLBACK (not default).
 */
export const CHEAP_MODEL_BY_PROVIDER: Record<string, string> = {
  zai: 'glm-4.5-flash',
  anthropic: 'claude-haiku-4-5-20251001',
  openai: 'gpt-4.1-mini',
  gemini: 'gemini-flash-lite',
  google: 'gemini-flash-lite',
  mistral: 'mistral-small-latest',
  groq: 'llama-3.3-70b-versatile',
  deepseek: 'deepseek-chat',
  openrouter: 'anthropic/claude-haiku-4-5-20251001',
  xai: 'grok-2',
  together: 'meta-llama/Llama-3.3-70B-Instruct-Turbo',
  cerebras: 'llama3.3-70b',
};

/**
 * Derive a cheap/fast model suitable for fact extraction, given the user's
 * provider and primary (potentially expensive) model.
 *
 * LEGACY (internal#502): no longer on the live extraction path — the config
 * builder now calls `resolveExtractionModel()`, which prefers the cheapest of
 * the user's OWN configured OpenClaw models and only falls back to the
 * hardcoded `CHEAP_MODEL_BY_PROVIDER` table as a last resort. Kept exported +
 * unit-tested because it still exercises the shared `CHEAP_INDICATOR_RE`
 * word-boundary logic and may be imported externally; prefer
 * `resolveExtractionModel` for new callers.
 */
export function deriveCheapModel(provider: string, primaryModel: string): string {
  // If already on a cheap model, use it as-is.
  // Word-boundary match to avoid false positives (see CHEAP_INDICATOR_RE).
  if (CHEAP_INDICATOR_RE.test(primaryModel)) {
    return primaryModel;
  }

  // Derive based on provider naming conventions
  const fromTable = CHEAP_MODEL_BY_PROVIDER[provider];
  if (fromTable) return fromTable;

  // Fallback: use the primary model (best-effort — caller may still work)
  return primaryModel;
}

// ---------------------------------------------------------------------------
// Configured-model selection (Pedro directive 2026-07-20: no baked-in model)
// ---------------------------------------------------------------------------

/**
 * A model entry from the user's OpenClaw provider config
 * (`models.providers.<provider>.models[]`). Real entries carry
 * `{ id, name, contextWindow, maxTokens }` — NO price field, so cheapness is
 * proxied by the cheap-tier indicator on the id plus the smallest `maxTokens`.
 */
export interface ConfiguredModel {
  id: string;
  maxTokens?: unknown;
  contextWindow?: unknown;
}

/**
 * Pick the cheapest model id from the USER'S OWN configured models list.
 *
 * Selection rule (Pedro directive):
 *   a. keep only ids that carry a genuine cheap-tier indicator
 *      (`CHEAP_INDICATOR_RE`: flash / mini / nano / lite / small / fast /
 *      haiku at a word boundary — the same regex that rejects the "mini"
 *      inside "gemini");
 *   b. among the matches, rank by smallest `maxTokens` (proxy for the
 *      cheapest/faster tier; `contextWindow` is a secondary proxy when
 *      `maxTokens` is absent; both absent → largest so a known-small value
 *      wins), then lexical id for determinism;
 *   c. if NOTHING matches a cheap indicator, return `null` — do NOT invent a
 *      model. The caller then falls to the hardcoded `CHEAP_MODEL_BY_PROVIDER`
 *      table (tier 4, last resort).
 *
 * So on the QA box (zai models [glm-5-turbo, glm-4.7, glm-4.5-flash]) this
 * returns `glm-4.5-flash` *from the user's config*, and for an OpenAI user on
 * [gpt-5, gpt-5-mini] it returns `gpt-5-mini` — no baked-in model names.
 */
export function selectCheapestConfiguredModel(
  configuredModels: ReadonlyArray<ConfiguredModel> | undefined,
): string | null {
  if (!configuredModels || configuredModels.length === 0) return null;
  const cheap = configuredModels.filter(
    (m) => m && typeof m.id === 'string' && CHEAP_INDICATOR_RE.test(m.id),
  );
  if (cheap.length === 0) return null;

  // Cheapness proxy: smaller output budget = cheaper/faster tier. Missing
  // maxTokens falls back to contextWindow, then to MAX_SAFE_INTEGER so a model
  // with a known small budget ranks above one with an unknown budget.
  const tokenRank = (m: ConfiguredModel): number => {
    if (typeof m.maxTokens === 'number' && Number.isFinite(m.maxTokens)) return m.maxTokens;
    if (typeof m.contextWindow === 'number' && Number.isFinite(m.contextWindow)) return m.contextWindow;
    return Number.MAX_SAFE_INTEGER;
  };

  cheap.sort((a, b) => {
    const da = tokenRank(a);
    const db = tokenRank(b);
    if (da !== db) return da - db;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
  return cheap[0].id;
}

/**
 * Full extraction-model precedence (Pedro directive, 2026-07-20). Returns the
 * model id AND a short reason so the caller can log which tier fired.
 *
 *   1. `modelOverride` — explicit `extraction.model` / `extraction.llm.model`.
 *   2. the primary model, IF it already carries a cheap-tier indicator.
 *   3. the cheapest of the user's OWN configured models (`models[]`).
 *   4. the hardcoded `CHEAP_MODEL_BY_PROVIDER` table — LAST RESORT, only when
 *      no configured list is available or it has no cheap-indicator match.
 *   5. the primary model (best-effort).
 */
export function resolveExtractionModel(
  provider: string,
  opts: {
    modelOverride?: string;
    primaryModelHint?: string;
    configuredModels?: ReadonlyArray<ConfiguredModel>;
  },
): { model: string | null; reason: string } {
  if (opts.modelOverride) {
    return { model: opts.modelOverride, reason: 'explicit override' };
  }
  if (opts.primaryModelHint && CHEAP_INDICATOR_RE.test(opts.primaryModelHint)) {
    return { model: opts.primaryModelHint, reason: 'primary already cheap' };
  }
  const fromConfig = selectCheapestConfiguredModel(opts.configuredModels);
  if (fromConfig) {
    return { model: fromConfig, reason: 'cheapest of configured models' };
  }
  const fromTable = CHEAP_MODEL_BY_PROVIDER[provider];
  if (fromTable) {
    return { model: fromTable, reason: 'hardcoded table (fallback — no cheap model in config)' };
  }
  return { model: opts.primaryModelHint ?? null, reason: 'primary model (best-effort)' };
}

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

let _cachedConfig: LLMClientConfig | null = null;
let _initialized = false;
let _logger: { warn: (msg: string) => void; info?: (msg: string) => void } | null = null;

/** Harvested auth-profile key entry — same shape as llm-profile-reader. */
export interface AuthProfileKeyInput {
  provider: string;
  apiKey: string;
  sourcePath?: string;
  profileId?: string;
}

/**
 * Plugin-level extraction override block. Read from
 * `plugins.entries.totalreclaw.config.extraction.llm` via the plugin
 * config surface.
 */
interface ExtractionLlmOverride {
  provider?: string;
  model?: string;
  apiKey?: string;
  baseUrl?: string;
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Build an LLMClientConfig for a known provider + apiKey, picking the
 * extraction model via the 5-tier `resolveExtractionModel` precedence.
 * Returns `{ config, reason }` (reason = which model tier fired, for
 * logging), or null if the provider is unknown / no model resolves.
 */
function buildConfigForProvider(
  provider: string,
  apiKey: string,
  opts: {
    baseUrlOverride?: string;
    modelOverride?: string;
    primaryModelHint?: string;
    configuredModels?: ReadonlyArray<ConfiguredModel>;
    apiFormatOverride?: 'openai' | 'anthropic';
  } = {},
): { config: LLMClientConfig; reason: string } | null {
  // zai's base URL is resolved via `getZaiBaseUrl()` (reads CONFIG) so
  // the `ZAI_BASE_URL` env override takes effect even when this helper is
  // called with no `baseUrlOverride` (i.e. the env-var fallback tier in
  // initLLMClient).
  const defaultForProvider =
    provider === 'zai' ? getZaiBaseUrl() : PROVIDER_BASE_URLS[provider] ?? '';
  const baseUrl = (opts.baseUrlOverride ?? defaultForProvider).replace(/\/+$/, '');
  if (!baseUrl) return null;
  const { model, reason } = resolveExtractionModel(provider, {
    modelOverride: opts.modelOverride,
    primaryModelHint: opts.primaryModelHint,
    configuredModels: opts.configuredModels,
  });
  if (!model) return null;
  const apiFormat: 'openai' | 'anthropic' =
    opts.apiFormatOverride ?? (provider === 'anthropic' ? 'anthropic' : 'openai');
  return { config: { apiKey, baseUrl, model, apiFormat }, reason };
}

/**
 * Initialize the LLM client by detecting the provider from OpenClaw's config.
 * Called once from the plugin's `register()` function.
 *
 * 3.3.1 resolution cascade (highest priority first):
 *   1. Plugin config `extraction.llm` override block (provider/apiKey/baseUrl/model)
 *   2. `api.config.providers` / `openclawProviders` — SDK-passed
 *   3. `~/.openclaw/agents/*\/agent/auth-profiles.json` (harvested by caller)
 *   4. Env var fallback (`ZAI_API_KEY`, `OPENAI_API_KEY`, ...)
 *   5. No source → disable extraction cleanly (single log at startup, never
 *      per-turn).
 *
 * The `TOTALRECLAW_LLM_MODEL` user-facing override was removed in v1 —
 * `deriveCheapModel(provider)` covers the 99% case and a model-level knob
 * was adding config surface for no tangible win.
 */
export function initLLMClient(options: {
  primaryModel?: string;
  pluginConfig?: Record<string, unknown>;
  openclawProviders?: Record<string, OpenClawProviderConfig>;
  /**
   * Auth-profile entries harvested by llm-profile-reader. Caller supplies
   * this list so llm-client.ts never touches disk (scanner isolation —
   * this file has `fetch`/`POST` in it).
   */
  authProfileKeys?: AuthProfileKeyInput[];
  logger?: { warn: (msg: string) => void; info?: (msg: string) => void };
}): void {
  _logger = options.logger ?? null;
  _initialized = true;
  _cachedConfig = null;

  const { primaryModel, pluginConfig, openclawProviders, authProfileKeys } = options;

  // Check if extraction is explicitly disabled
  const extraction = pluginConfig?.extraction as Record<string, unknown> | undefined;
  if (extraction?.enabled === false) {
    _logger?.info?.(
      'TotalReclaw extraction LLM: disabled via plugin config (extraction.enabled=false).',
    );
    return;
  }

  const modelOverride =
    typeof extraction?.model === 'string' ? (extraction.model as string) : undefined;
  const llmOverrideRaw = extraction?.llm as ExtractionLlmOverride | undefined;
  const llmOverride: ExtractionLlmOverride | undefined =
    typeof llmOverrideRaw === 'object' && llmOverrideRaw !== null ? llmOverrideRaw : undefined;

  // Derive provider name from primary-model ("anthropic/claude-sonnet-4-5" etc)
  let providerFromPrimary = '';
  let modelFromPrimary: string | undefined;
  if (primaryModel) {
    const parts = primaryModel.split('/');
    if (parts.length >= 2) {
      providerFromPrimary = parts[0].toLowerCase();
      modelFromPrimary = parts.slice(1).join('/');
    } else {
      modelFromPrimary = primaryModel;
    }
  }

  // ---------------------------------------------------------------------
  // Tier 1 — explicit plugin-config override (highest priority)
  // Accepts any subset of { provider, model, apiKey, baseUrl }. A bare
  // `model` override without a provider+apiKey falls through to lower
  // tiers — matches pre-3.3.1 behaviour.
  // ---------------------------------------------------------------------
  if (llmOverride && typeof llmOverride === 'object') {
    const provider = (llmOverride.provider ?? providerFromPrimary).toLowerCase();
    const apiKey =
      typeof llmOverride.apiKey === 'string' && llmOverride.apiKey.trim()
        ? llmOverride.apiKey.trim()
        : undefined;
    if (provider && apiKey) {
      const resolved = buildConfigForProvider(provider, apiKey, {
        baseUrlOverride: llmOverride.baseUrl,
        modelOverride: llmOverride.model ?? modelOverride,
        primaryModelHint: modelFromPrimary,
        // #502: even for an explicit provider+key override, honor the user's
        // configured models[] when the override omits a model — keeps
        // cheapest-configured selection consistent across every tier (moot when
        // the override sets a model, since that wins outright).
        configuredModels: openclawProviders?.[provider]?.models,
      });
      if (resolved) {
        _cachedConfig = resolved.config;
        _logger?.info?.(
          `TotalReclaw extraction LLM: resolved ${provider}/${resolved.config.model} (plugin config override; model: ${resolved.reason})`,
        );
        return;
      }
    }
  }

  // ---------------------------------------------------------------------
  // Tier 2 — SDK-passed openclawProviders. Try the primary-model's provider
  // first, then any other provider that has an apiKey.
  // ---------------------------------------------------------------------
  if (openclawProviders) {
    if (providerFromPrimary) {
      const ocProvider = openclawProviders[providerFromPrimary];
      if (ocProvider?.apiKey) {
        const resolved = buildConfigForProvider(providerFromPrimary, ocProvider.apiKey, {
          baseUrlOverride: ocProvider.baseUrl,
          modelOverride,
          primaryModelHint: modelFromPrimary,
          configuredModels: ocProvider.models,
          apiFormatOverride:
            ocProvider.api === 'anthropic-messages' || providerFromPrimary === 'anthropic'
              ? 'anthropic'
              : 'openai',
        });
        if (resolved) {
          _cachedConfig = resolved.config;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${resolved.config.model} (OpenClaw provider config; model: ${resolved.reason})`,
          );
          return;
        }
      }
    }
    for (const [providerName, providerConfig] of Object.entries(openclawProviders)) {
      if (!providerConfig?.apiKey) continue;
      const provider = providerName.toLowerCase();
      const firstModelId = providerConfig.models?.[0]?.id;
      const resolved = buildConfigForProvider(provider, providerConfig.apiKey, {
        baseUrlOverride: providerConfig.baseUrl,
        modelOverride,
        primaryModelHint: firstModelId,
        configuredModels: providerConfig.models,
        apiFormatOverride:
          providerConfig.api === 'anthropic-messages' || provider === 'anthropic'
            ? 'anthropic'
            : 'openai',
      });
      if (resolved) {
        _cachedConfig = resolved.config;
        _logger?.info?.(
          `TotalReclaw extraction LLM: resolved ${provider}/${resolved.config.model} (OpenClaw provider config; model: ${resolved.reason})`,
        );
        return;
      }
    }
  }

  // ---------------------------------------------------------------------
  // Tier 3 — auth-profiles.json keys harvested by llm-profile-reader.
  // 3.3.1: new tier. Prefer the primary-model's provider, then any other.
  // ---------------------------------------------------------------------
  if (authProfileKeys && authProfileKeys.length > 0) {
    if (providerFromPrimary) {
      const hit = authProfileKeys.find((k) => k.provider === providerFromPrimary);
      if (hit) {
        const resolved = buildConfigForProvider(providerFromPrimary, hit.apiKey, {
          modelOverride,
          primaryModelHint: modelFromPrimary,
          // #502: the key came from auth-profiles, but the user's configured
          // models[] still lives in the OpenClaw provider config — thread it so
          // cheapest-configured selection fires even when the key isn't co-located.
          configuredModels: openclawProviders?.[providerFromPrimary]?.models,
        });
        if (resolved) {
          _cachedConfig = resolved.config;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${resolved.config.model} (auth-profiles.json; model: ${resolved.reason})`,
          );
          return;
        }
      }
    }
    // Try zai / openai / anthropic first (cheapest+most available), then anything else.
    const priority = ['zai', 'openai', 'anthropic', 'gemini', 'groq', 'deepseek', 'mistral', 'openrouter', 'xai', 'together', 'cerebras'];
    const ordered = [
      ...priority.flatMap((p) => authProfileKeys.filter((k) => k.provider === p)),
      ...authProfileKeys.filter((k) => !priority.includes(k.provider)),
    ];
    for (const entry of ordered) {
      const resolved = buildConfigForProvider(entry.provider, entry.apiKey, {
        modelOverride,
        // #502: thread the OpenClaw-configured models[] for this provider (key
        // source and models[] source are independent).
        configuredModels: openclawProviders?.[entry.provider]?.models,
      });
      if (resolved) {
        _cachedConfig = resolved.config;
        _logger?.info?.(
          `TotalReclaw extraction LLM: resolved ${entry.provider}/${resolved.config.model} (auth-profiles.json; model: ${resolved.reason})`,
        );
        return;
      }
    }
  }

  // ---------------------------------------------------------------------
  // Tier 4 — env var fallback (for dev/test without OpenClaw config)
  // ---------------------------------------------------------------------
  const envFallback: Array<[string, string]> = [
    ['zai', 'zai'],
    ['openai', 'openai'],
    ['anthropic', 'anthropic'],
    ['gemini', 'gemini'],
    ['groq', 'groq'],
    ['deepseek', 'deepseek'],
    ['mistral', 'mistral'],
    ['openrouter', 'openrouter'],
    ['xai', 'xai'],
  ];
  // If primary model hints a specific provider, try it first.
  if (providerFromPrimary) {
    const keyNames = PROVIDER_KEY_NAMES[providerFromPrimary];
    if (keyNames) {
      const apiKey = keyNames.map((n) => CONFIG.llmApiKeys[n]).find(Boolean);
      if (apiKey) {
        const resolved = buildConfigForProvider(providerFromPrimary, apiKey, {
          modelOverride,
          primaryModelHint: modelFromPrimary,
          // #502: the key comes from an env var (e.g. ZAI_API_KEY), but the
          // user's models[] still lives in the OpenClaw provider config — thread
          // it so cheapest-configured selection fires instead of silently
          // dropping to the hardcoded CHEAP_MODEL_BY_PROVIDER table. This is the
          // common production setup (provider key via env, models[] in config).
          configuredModels: openclawProviders?.[providerFromPrimary]?.models,
        });
        if (resolved) {
          _cachedConfig = resolved.config;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${resolved.config.model} (env var; model: ${resolved.reason})`,
          );
          return;
        }
      }
    }
  }
  for (const [provider, keyName] of envFallback) {
    const apiKey = CONFIG.llmApiKeys[keyName];
    if (!apiKey) continue;
    const resolved = buildConfigForProvider(provider, apiKey, {
      modelOverride,
      // #502: env-var key + OpenClaw-configured models[] (independent sources).
      configuredModels: openclawProviders?.[provider]?.models,
    });
    if (resolved) {
      _cachedConfig = resolved.config;
      _logger?.info?.(
        `TotalReclaw extraction LLM: resolved ${provider}/${resolved.config.model} (env var; model: ${resolved.reason})`,
      );
      return;
    }
  }

  // ---------------------------------------------------------------------
  // No source — extraction disabled. Single startup log, INFO-level.
  // NOT a warn: this is the default state for users who have not set up a
  // provider. Warning per turn is what 3.3.0 did and it was misleading.
  // ---------------------------------------------------------------------
  _logger?.info?.(
    'TotalReclaw extraction LLM: not configured — auto-extraction disabled. ' +
      'To enable, configure a provider in ~/.openclaw/agents/*\/agent/auth-profiles.json ' +
      'or set an API key env var (ZAI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, ...).',
  );
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Resolve LLM configuration. Returns the cached config set by `initLLMClient()`,
 * or falls back to the legacy env-var detection if `initLLMClient()` was never called.
 */
export function resolveLLMConfig(): LLMClientConfig | null {
  if (_initialized) {
    return _cachedConfig;
  }

  // Legacy fallback: if initLLMClient() was never called (e.g. running outside
  // the plugin context), try the config-based approach for backwards compat.
  const zaiKey = CONFIG.llmApiKeys.zai;
  const openaiKey = CONFIG.llmApiKeys.openai;

  const model = zaiKey ? 'glm-4.5-flash' : 'gpt-4.1-mini';

  if (zaiKey) {
    return {
      apiKey: zaiKey,
      baseUrl: getZaiBaseUrl(),
      model,
      apiFormat: 'openai',
    };
  }

  if (openaiKey) {
    return {
      apiKey: openaiKey,
      baseUrl: 'https://api.openai.com/v1',
      model,
      apiFormat: 'openai',
    };
  }

  return null;
}

/**
 * Options for chatCompletion. `retry` controls the 429 + timeout backoff
 * loop. Defaults to 5 attempts, so 4 waits between them, with a per-attempt
 * ceiling of 2s → 4s → 8s → 16s (the 5th attempt is the final try and does
 * not wait) under FULL jitter (AWS "full jitter": wait =
 * random(0, min(cap, base*2^(n-1)))) — worst case ~30s of backoff, capped by
 * a 60s total budget — plus `Retry-After` honoring up to a 60s ceiling
 * (internal#502). rc.1/rc.2 QA showed multi-minute upstream outages that blew
 * through the rc.2 7s budget. Cumulative budget configurable via
 * `TOTALRECLAW_LLM_RETRY_BUDGET_MS` env; on exhaustion (budget, or a
 * Retry-After past the ceiling) chatCompletion throws LLMUpstreamOutageError.
 */
export interface ChatCompletionOptions {
  maxTokens?: number;
  temperature?: number;
  /**
   * Retry behaviour. Defaults mirror the rc.3/#502 budget: 5 attempts, 2s
   * base delay, exponential with FULL jitter, `Retry-After` honored. Set
   * `attempts: 0` (or `1`) to disable retry. Pass a `logger` for visibility;
   * without one, retries are silent.
   *
   * `budgetMs` caps the cumulative retry-delay time — after an attempt
   * fails, we compute the next wait and skip it (falling through to the
   * give-up path) if adding it would exceed the budget. Defaults to the
   * value read from `TOTALRECLAW_LLM_RETRY_BUDGET_MS` at module load,
   * which itself defaults to 60_000ms.
   *
   * `capMs` (default 60_000) caps any single per-attempt wait, including the
   * `Retry-After` floor. A `Retry-After` demanding MORE than `capMs` is
   * treated as "this-cycle-exhausted" — we surface `LLMUpstreamOutageError`
   * immediately rather than burning retries against a long upstream pause
   * (the extractor then re-queues the batch for the background poller).
   *
   * `random` / `now` are dependency-injected for deterministic tests (the
   * default `Math.random` / `Date.now` are used in production).
   */
  retry?: {
    attempts?: number;
    baseDelayMs?: number;
    budgetMs?: number;
    capMs?: number;
    /** Injectable RNG for the full-jitter wait (default Math.random). */
    random?: () => number;
    /** Injectable clock for Retry-After HTTP-date parsing (default Date.now). */
    now?: () => number;
  };
  logger?: {
    info?: (msg: string) => void;
    warn?: (msg: string) => void;
    debug?: (msg: string) => void;
  };
  /** Timeout per attempt in ms (default 30_000). */
  timeoutMs?: number;
}

/**
 * Default per-attempt retry-delay cap (ms). Also the `Retry-After` ceiling:
 * a server-demanded wait above this is treated as this-cycle-exhausted
 * (re-queue for the poller) rather than slept through. Matches the Python
 * `_RETRY_AFTER_CEILING_S = 60`.
 */
export const DEFAULT_RETRY_CAP_MS = 60_000;

/**
 * Default retry budget in ms. Configurable via
 * `TOTALRECLAW_LLM_RETRY_BUDGET_MS` env var — read by `config.ts`. Callers
 * can override per-call via `retry.budgetMs`. 60_000ms comfortably covers the
 * default 5-attempt schedule (worst case ~30s of exponential backoff) plus a
 * single Retry-After wait up to the 60s ceiling.
 *
 * Scanner-isolation note: the env read lives in `config.ts` so this file
 * stays clean of env-harvesting triggers.
 */
export const DEFAULT_RETRY_BUDGET_MS: number = CONFIG.llmRetryBudgetMs;

/**
 * Structured error thrown when the extraction LLM upstream is unreachable
 * after the full retry budget is exhausted. The extraction pipeline
 * recognizes this via `err instanceof LLMUpstreamOutageError` and can
 * choose to:
 *   - queue the message batch for retry next turn,
 *   - surface a one-time notification to the user, or
 *   - simply skip this extraction window silently.
 */
export class LLMUpstreamOutageError extends Error {
  readonly attempts: number;
  readonly lastStatus?: number;
  constructor(message: string, attempts: number, lastStatus?: number) {
    super(message);
    this.name = 'LLMUpstreamOutageError';
    this.attempts = attempts;
    this.lastStatus = lastStatus;
  }
}

/**
 * Detect the "Insufficient balance" error shape from zai. Matches both
 * the exact production wording ("Insufficient balance or no resource
 * package. Please recharge.") and the short "no resource package" variant
 * we've seen in some historical responses.
 */
export function isZaiBalanceError(errorMessage: string): boolean {
  const m = errorMessage.toLowerCase();
  return m.includes('insufficient balance') || m.includes('no resource package');
}

// ---------------------------------------------------------------------------
// Retry-After (Part 1.3) — pure parser + HTTP-error metadata plumbing
// ---------------------------------------------------------------------------

/**
 * A thrown error carrying the HTTP status + raw `Retry-After` header value
 * from the upstream response. The OpenAI/Anthropic fetch paths attach these
 * so the retry loop can honor `Retry-After` without re-reading the response.
 */
interface HttpError extends Error {
  httpStatus?: number;
  retryAfterHeader?: string | null;
}

/**
 * Parse a `Retry-After` header (RFC 7231 §7.1.3) into a wait in MS.
 *
 * Accepts either:
 *   - delta-seconds: `"5"` → 5000
 *   - HTTP-date:     `"Wed, 21 Oct 2026 07:28:00 GMT"` → ms from `now`
 *
 * Returns the wait floored at 0 (a past HTTP-date → 0, i.e. retry now), or
 * `null` when the header is absent / unparseable. Deliberately does NOT cap —
 * the retry loop applies the 60s ceiling and the "above ceiling → exhausted"
 * rule, so this parser stays pure and unit-testable. Mirrors the Python
 * `_retry_after_seconds` helper.
 */
export function parseRetryAfter(
  header: string | null | undefined,
  opts: { now?: () => number } = {},
): number | null {
  if (header == null) return null;
  const raw = header.trim();
  if (raw === '') return null;

  // delta-seconds (integer or decimal, per RFC allows decimal).
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const secs = parseFloat(raw);
    if (!Number.isFinite(secs) || secs < 0) return null;
    return Math.max(0, Math.round(secs * 1000));
  }

  // HTTP-date (RFC 1123, e.g. "Wed, 21 Oct 2026 07:28:00 GMT").
  const parsed = Date.parse(raw);
  if (Number.isFinite(parsed)) {
    const now = (opts.now ?? Date.now)();
    return Math.max(0, parsed - now);
  }

  return null;
}

/**
 * Stamp an outgoing HTTP error with the response status + Retry-After header
 * so the retry loop can honor them. Used by the OpenAI/Anthropic fetch paths.
 */
function stampHttpError(
  err: Error,
  status: number | undefined,
  retryAfterHeader: string | null,
): HttpError {
  const e = err as HttpError;
  if (status !== undefined) e.httpStatus = status;
  e.retryAfterHeader = retryAfterHeader;
  return e;
}

/**
 * Wrap a low-level fetch error in the `LLM call failed: …` envelope while
 * PRESERVING any HTTP status / Retry-After metadata attached by
 * `stampHttpError` (so the retry loop still sees them after the wrap).
 */
function wrapLlmError(err: unknown): Error {
  const msg = err instanceof Error ? err.message : String(err);
  const wrapped: HttpError = new Error(`LLM call failed: ${msg}`);
  if (err && typeof err === 'object') {
    const src = err as HttpError;
    if (src.httpStatus !== undefined) wrapped.httpStatus = src.httpStatus;
    if (src.retryAfterHeader !== undefined) wrapped.retryAfterHeader = src.retryAfterHeader;
  }
  return wrapped;
}

/** Read the `retryAfterHeader` a wrapped error may be carrying (or undefined). */
function retryAfterFromError(err: unknown): string | null | undefined {
  if (err && typeof err === 'object' && 'retryAfterHeader' in err) {
    return (err as HttpError).retryAfterHeader;
  }
  return undefined;
}

/**
 * Identify the "other" zai endpoint when the current one returns a balance
 * error — CODING ↔ STANDARD. Returns `null` when the URL is neither of
 * the two zai endpoints we know about (e.g. a self-hosted proxy), which
 * means the fallback logic stays put.
 */
export function zaiFallbackBaseUrl(currentBaseUrl: string): string | null {
  const normalized = currentBaseUrl.replace(/\/+$/, '');
  if (normalized === ZAI_CODING_BASE_URL) return ZAI_STANDARD_BASE_URL;
  if (normalized === ZAI_STANDARD_BASE_URL) return ZAI_CODING_BASE_URL;
  return null;
}

/**
 * Call the LLM chat completion endpoint.
 *
 * Supports both OpenAI-compatible format and Anthropic Messages API,
 * determined by `config.apiFormat`.
 *
 * 3.3.1-rc.3 — lifts the retry budget to 5 attempts (4 waits, per-attempt
 * ceiling 2s/4s/8s/16s → ~30s worst-case backoff) under a 60s total budget.
 * Configurable via `TOTALRECLAW_LLM_RETRY_BUDGET_MS`. Adds zai
 * "Insufficient balance" auto-fallback: when a zai 429 carries the balance
 * error body AND we're on one of the two known zai endpoints, we flip to
 * the OTHER endpoint and retry ONCE (accounted for separately from the
 * normal retry loop). On exhaustion, throws `LLMUpstreamOutageError`.
 *
 * Non-retryable errors (4xx other than 429, network refused, JSON parse)
 * fail fast on the first attempt.
 *
 * @returns The assistant's response content, or null on failure.
 */
export async function chatCompletion(
  config: LLMClientConfig,
  messages: ChatMessage[],
  options?: ChatCompletionOptions,
): Promise<string | null> {
  const maxTokens = options?.maxTokens ?? 2048;
  const temperature = options?.temperature ?? 0; // Deterministic output for dedup (same input → same text → same content fingerprint)
  const attempts = Math.max(1, options?.retry?.attempts ?? 5);
  const baseDelayMs = Math.max(100, options?.retry?.baseDelayMs ?? 2000);
  const budgetMs = Math.max(100, options?.retry?.budgetMs ?? DEFAULT_RETRY_BUDGET_MS);
  const capMs = Math.max(1000, options?.retry?.capMs ?? DEFAULT_RETRY_CAP_MS);
  const randomFn = options?.retry?.random ?? Math.random;
  const nowFn = options?.retry?.now ?? Date.now;
  const timeoutMs = options?.timeoutMs ?? 30_000;
  const logger = options?.logger;

  // We mutate `activeConfig.baseUrl` in the zai fallback branch so the
  // retried call hits the other endpoint. Shallow-clone so the caller's
  // config object stays untouched.
  const activeConfig: LLMClientConfig = { ...config };

  // One-shot flag: we only auto-fallback zai once per chatCompletion call
  // to prevent ping-pong between the two endpoints if both reject.
  let zaiFallbackAttempted = false;

  const callOnce = (): Promise<string | null> =>
    activeConfig.apiFormat === 'anthropic'
      ? chatCompletionAnthropic(activeConfig, messages, maxTokens, temperature, timeoutMs)
      : chatCompletionOpenAI(activeConfig, messages, maxTokens, temperature, timeoutMs);

  let lastErr: unknown;
  let cumulativeDelayMs = 0;
  let lastStatus: number | undefined;

  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await callOnce();
    } catch (err) {
      lastErr = err;
      const msg = err instanceof Error ? err.message : String(err);
      lastStatus = parseHttpStatus(msg) ?? lastStatus;

      // ── zai "Insufficient balance" auto-fallback ──
      // Fires BEFORE the normal retry accounting. If the error is a zai
      // balance-shaped 429, flip the baseUrl once and immediately retry —
      // no backoff, no decrement of the attempt count. Keeps the total
      // attempt budget reserved for genuine outages.
      if (!zaiFallbackAttempted && /\b429\b/.test(msg) && isZaiBalanceError(msg)) {
        const fallback = zaiFallbackBaseUrl(activeConfig.baseUrl);
        if (fallback) {
          zaiFallbackAttempted = true;
          const oldUrl = activeConfig.baseUrl;
          activeConfig.baseUrl = fallback;
          logger?.info?.(
            `chatCompletion: zai endpoint auto-fallback: ${oldUrl} → ${fallback} due to "Insufficient balance" response`,
          );
          // Retry immediately — do NOT decrement attempts counter further;
          // this "extra" attempt is the fallback freebie.
          attempt--;
          continue;
        }
      }

      const retryable = isRetryable(msg);
      const isFinalAttempt = attempt >= attempts;
      if (!retryable || isFinalAttempt) {
        // Fail-fast OR last attempt — rethrow.
        if (attempt > 1 || !retryable) {
          if (retryable) {
            logger?.warn?.(`chatCompletion: giving up after ${attempt} attempts: ${msg.slice(0, 200)}`);
          }
          // Structured outage error when the retryable error budget is
          // fully exhausted — lets downstream recognize vs bail silently.
          if (retryable) {
            throw new LLMUpstreamOutageError(
              `LLM upstream outage after ${attempt} attempts: ${msg.slice(0, 200)}`,
              attempt,
              lastStatus,
            );
          }
        }
        throw err;
      }

      // ── Compute the next wait (Part 1.2 + 1.3) ──
      // Exponential backoff capped per-attempt at `capMs`, then FULL jitter
      // (AWS "full jitter": wait = random(0, min(cap, base*2^(n-1)))) to
      // de-synchronize concurrent extraction. Then honor `Retry-After` as a
      // floor: wait = max(jittered, retry_after).
      const expDelayMs = Math.min(capMs, baseDelayMs * Math.pow(2, attempt - 1));
      const jitteredMs = Math.min(expDelayMs, Math.floor(randomFn() * (expDelayMs + 1)));

      const retryAfterHeader = retryAfterFromError(err);
      const retryAfterMs = parseRetryAfter(retryAfterHeader, { now: nowFn });

      // Retry-After ABOVE the ceiling → the server is signalling a long pause;
      // don't burn retries against it. Treat as this-cycle-exhausted so the
      // extractor re-queues the batch for the background poller (Part 1.4).
      if (retryAfterMs !== null && retryAfterMs > capMs) {
        logger?.warn?.(
          `chatCompletion: Retry-After ${retryAfterMs}ms exceeds ${capMs}ms ceiling; surfacing outage after ${attempt} attempts (will re-queue): ${msg.slice(0, 160)}`,
        );
        throw new LLMUpstreamOutageError(
          `LLM upstream outage (Retry-After ${retryAfterMs}ms > ${capMs}ms ceiling after ${attempt} attempts): ${msg.slice(0, 200)}`,
          attempt,
          lastStatus,
        );
      }

      const delayMs = retryAfterMs !== null ? Math.max(jitteredMs, retryAfterMs) : jitteredMs;

      // Respect the cumulative retry-budget cap.
      if (cumulativeDelayMs + delayMs > budgetMs) {
        logger?.warn?.(
          `chatCompletion: retry budget exhausted (${cumulativeDelayMs}ms used + ${delayMs}ms next > ${budgetMs}ms budget); surfacing outage after ${attempt} attempts: ${msg.slice(0, 160)}`,
        );
        throw new LLMUpstreamOutageError(
          `LLM upstream outage (budget ${budgetMs}ms exhausted after ${attempt} attempts): ${msg.slice(0, 200)}`,
          attempt,
          lastStatus,
        );
      }
      cumulativeDelayMs += delayMs;

      // Log only the FIRST retry at INFO to avoid spamming during long
      // outages; subsequent retries are DEBUG (debounced per outage).
      const waitLabel = retryAfterMs !== null ? `${delayMs}ms (Retry-After honored)` : `${delayMs}ms`;
      if (attempt === 1) {
        logger?.info?.(
          `chatCompletion: retrying after transient failure (attempt ${attempt}/${attempts}, wait ${waitLabel}): ${msg.slice(0, 160)}`,
        );
      } else {
        logger?.debug?.(
          `chatCompletion: retry attempt ${attempt}/${attempts} (wait ${waitLabel}): ${msg.slice(0, 160)}`,
        );
      }
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  // Defensive — should never reach here since the loop always throws on the
  // final attempt when it fails. Keeps TS happy.
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

/**
 * Parse the HTTP status code from an error message of the form
 * `"LLM API 429: rate limit"` or `"Anthropic API 503: ..."`. Returns
 * `undefined` when the message doesn't follow that shape (e.g. network
 * refused). Used by `LLMUpstreamOutageError.lastStatus` for downstream
 * classification.
 */
function parseHttpStatus(errorMessage: string): number | undefined {
  const m = errorMessage.match(/\b(\d{3})\b/);
  if (!m) return undefined;
  const code = parseInt(m[1], 10);
  return code >= 100 && code < 600 ? code : undefined;
}

/**
 * Which LLM-call errors are worth retrying. Exported for testability.
 *
 * Retryable:
 *   - HTTP 429 (rate limit)
 *   - HTTP 503 / 502 / 504 (gateway transients)
 *   - AbortError / "aborted due to timeout" / "TimeoutError"
 *
 * NOT retryable:
 *   - HTTP 400 / 401 / 403 / 404 (auth / request errors — no point retrying)
 *   - JSON parse errors
 *   - DNS / connection refused (usually misconfig, not transient)
 */
export function isRetryable(errorMessage: string): boolean {
  const m = errorMessage.toLowerCase();
  // Rate limit
  if (/\b429\b/.test(errorMessage) || m.includes('rate limit')) return true;
  // Transient gateway errors
  if (/\b50(2|3|4)\b/.test(errorMessage)) return true;
  // Timeouts
  if (
    m.includes('timeout') ||
    m.includes('aborterror') ||
    m.includes('was aborted') ||
    m.includes('operation was aborted')
  ) {
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// OpenAI-compatible chat completion
// ---------------------------------------------------------------------------

/**
 * Provider base-URL hints for OpenAI-compatible endpoints that honour the
 * `response_format: {"type": "json_object"}` body field. Sending the field
 * to a provider that supports it makes JSON output deterministic; sending
 * it to a provider that does NOT recognise the field is a 400.
 *
 * Why this exists (3.3.12-rc.6, 2026-05-09):
 *   z.ai's GLM family (4.5-flash, 5-turbo, 5.1) silently returns EMPTY
 *   `message.content` for the merged-extraction prompt unless this hint
 *   is set. No error, no warning — the LLM just emits "" instead of the
 *   expected `{"topics": [], "facts": []}` JSON. Plugin's parse step
 *   then logs `0 raw facts` from a successful-but-empty branch.
 *
 *   This bug was found and fixed on the Python (Hermes) side in
 *   2.3.1-rc.23 (see `python/src/totalreclaw/agent/llm_client.py`
 *   `_supports_json_object_response_format`) but the plugin TS port did
 *   not carry the fix — observed in plugin 3.3.12-rc.5 auto-QA on
 *   2026-05-09: hook + poller both fired correctly but extraction
 *   returned 0 facts on every batch despite trajectories containing
 *   explicit "I prefer X" / "I work at Y" statements.
 *
 * Mirror of Python's `_supports_json_object_response_format`. Match by
 * substring on a lowercased baseUrl so cosmetic prefix differences
 * (https://, /v1, etc.) don't matter.
 */
const JSON_OBJECT_PROVIDER_HINTS = [
  'z.ai',
  'api.openai.com',
  'groq.com',
  'openrouter.ai',
  'deepseek.com',
  'mistral.ai',
  'x.ai',
  'together.xyz',
] as const;

export function supportsJsonObjectResponseFormat(baseUrl: string | undefined): boolean {
  if (!baseUrl) return false;
  const lower = baseUrl.toLowerCase();
  return JSON_OBJECT_PROVIDER_HINTS.some((h) => lower.includes(h));
}

async function chatCompletionOpenAI(
  config: LLMClientConfig,
  messages: ChatMessage[],
  maxTokens: number,
  temperature: number,
  timeoutMs: number,
): Promise<string | null> {
  const url = `${config.baseUrl}/chat/completions`;

  const body: Record<string, unknown> = {
    model: config.model,
    messages,
    temperature,
    max_completion_tokens: maxTokens,
  };

  // 3.3.12-rc.6: hint the provider to return strict JSON. Critical for
  // z.ai/GLM (silent-empty without it). See JSON_OBJECT_PROVIDER_HINTS
  // doc above.
  if (supportsJsonObjectResponseFormat(config.baseUrl)) {
    body.response_format = { type: 'json_object' };
  }

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      // Part 1.3: carry the status + Retry-After header so the retry loop can
      // honor the server's backoff demand (delta-seconds or HTTP-date).
      throw stampHttpError(
        new Error(`LLM API ${res.status}: ${text.slice(0, 200)}`),
        res.status,
        res.headers.get('retry-after'),
      );
    }

    const json = (await res.json()) as ChatCompletionResponse;
    const content = json.choices?.[0]?.message?.content ?? null;

    // 3.3.12-rc.6: loud-on-empty. If the provider returned a 200 with
    // empty content, this almost always means a missing response_format
    // hint or a content-filter. Without this log the silent-empty
    // failure mode (Python rc.23 / plugin rc.5) is invisible to ops.
    if (content === '' || content === null) {
      // Lazy import to avoid circular dep with the registered logger.
      // Fall back to console.warn if logger unavailable.
      const warn = (msg: string) => {
        try {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (globalThis as any)?.console?.warn?.(msg);
        } catch {
          /* noop */
        }
      };
      warn(
        `[totalreclaw][llm-client] provider=${config.baseUrl} model=${config.model} ` +
          `returned empty content (status=200). ` +
          `If using z.ai/GLM/OpenAI-compat, check response_format hint is being sent.`,
      );
    }

    return content;
  } catch (err) {
    // Wrap preserving any httpStatus / retryAfterHeader stamped above.
    throw wrapLlmError(err);
  }
}

// ---------------------------------------------------------------------------
// Anthropic Messages API chat completion
// ---------------------------------------------------------------------------

async function chatCompletionAnthropic(
  config: LLMClientConfig,
  messages: ChatMessage[],
  maxTokens: number,
  temperature: number,
  timeoutMs: number,
): Promise<string | null> {
  const url = `${config.baseUrl}/messages`;

  // Anthropic requires system prompt to be a top-level param, not in messages
  let system: string | undefined;
  const apiMessages: Array<{ role: string; content: string }> = [];

  for (const msg of messages) {
    if (msg.role === 'system') {
      system = msg.content;
    } else {
      apiMessages.push({ role: msg.role, content: msg.content });
    }
  }

  const body: Record<string, unknown> = {
    model: config.model,
    max_tokens: maxTokens,
    temperature,
    messages: apiMessages,
  };

  if (system) {
    body.system = system;
  }

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': config.apiKey,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      // Part 1.3: carry the status + Retry-After header (Anthropic also returns
      // Retry-After on 429) so the retry loop can honor it.
      throw stampHttpError(
        new Error(`Anthropic API ${res.status}: ${text.slice(0, 200)}`),
        res.status,
        res.headers.get('retry-after'),
      );
    }

    const json = (await res.json()) as AnthropicMessagesResponse;
    const textBlock = json.content?.find((block) => block.type === 'text');
    return textBlock?.text ?? null;
  } catch (err) {
    // Wrap preserving any httpStatus / retryAfterHeader stamped above.
    throw wrapLlmError(err);
  }
}

// ---------------------------------------------------------------------------
// Embedding (re-exported from local ONNX module)
// ---------------------------------------------------------------------------

// Embeddings are now generated locally via @huggingface/transformers
// (Harrier-OSS-v1-270M ONNX model). No API key needed. The native deps +
// model are lazy-fetched from a pinned GitHub Release on first call —
// see embedding.ts + embedder-loader.ts.
export {
  generateEmbedding,
  getEmbeddingDims,
  getEmbeddingModelId,
  configureEmbedder,
  // 3.3.3-rc.1 (#187): pre-pair bundle prefetch
  prefetchEmbedderBundle,
} from '../embedding/embedding.js';
