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

import { CONFIG } from './config.js';

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

const PROVIDER_BASE_URLS: Record<string, string> = {
  zai:        'https://api.z.ai/api/coding/paas/v4',
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
 * Default cheap extraction model per provider. Exported so callers that
 * resolve a provider WITHOUT knowing the user's primary model (e.g. the
 * auth-profiles.json path) can still pick a sensible model.
 *
 * 3.3.1 update: haiku is now `claude-haiku-4-5-20251001` (latest cheap
 * Claude as of 2026-04). glm-4.5-flash stays the zai extraction default.
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
 * Build an LLMClientConfig for a known provider + apiKey, picking a
 * cheap default model if none is specified. Returns null if the
 * provider is unknown and no baseUrl is available.
 */
function buildConfigForProvider(
  provider: string,
  apiKey: string,
  opts: {
    baseUrlOverride?: string;
    modelOverride?: string;
    primaryModelHint?: string;
    apiFormatOverride?: 'openai' | 'anthropic';
  } = {},
): LLMClientConfig | null {
  const baseUrl = (opts.baseUrlOverride ?? PROVIDER_BASE_URLS[provider] ?? '').replace(/\/+$/, '');
  if (!baseUrl) return null;
  const model =
    opts.modelOverride ??
    (opts.primaryModelHint ? deriveCheapModel(provider, opts.primaryModelHint) : null) ??
    CHEAP_MODEL_BY_PROVIDER[provider];
  if (!model) return null;
  const apiFormat: 'openai' | 'anthropic' =
    opts.apiFormatOverride ?? (provider === 'anthropic' ? 'anthropic' : 'openai');
  return { apiKey, baseUrl, model, apiFormat };
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
      const cfg = buildConfigForProvider(provider, apiKey, {
        baseUrlOverride: llmOverride.baseUrl,
        modelOverride: llmOverride.model ?? modelOverride,
        primaryModelHint: modelFromPrimary,
      });
      if (cfg) {
        _cachedConfig = cfg;
        _logger?.info?.(`TotalReclaw extraction LLM: resolved ${provider}/${cfg.model} (plugin config override)`);
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
        const cfg = buildConfigForProvider(providerFromPrimary, ocProvider.apiKey, {
          baseUrlOverride: ocProvider.baseUrl,
          modelOverride,
          primaryModelHint: modelFromPrimary,
          apiFormatOverride:
            ocProvider.api === 'anthropic-messages' || providerFromPrimary === 'anthropic'
              ? 'anthropic'
              : 'openai',
        });
        if (cfg) {
          _cachedConfig = cfg;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${cfg.model} (OpenClaw provider config)`,
          );
          return;
        }
      }
    }
    for (const [providerName, providerConfig] of Object.entries(openclawProviders)) {
      if (!providerConfig?.apiKey) continue;
      const provider = providerName.toLowerCase();
      const firstModelId = providerConfig.models?.[0]?.id;
      const cfg = buildConfigForProvider(provider, providerConfig.apiKey, {
        baseUrlOverride: providerConfig.baseUrl,
        modelOverride,
        primaryModelHint: firstModelId,
        apiFormatOverride:
          providerConfig.api === 'anthropic-messages' || provider === 'anthropic'
            ? 'anthropic'
            : 'openai',
      });
      if (cfg) {
        _cachedConfig = cfg;
        _logger?.info?.(
          `TotalReclaw extraction LLM: resolved ${provider}/${cfg.model} (OpenClaw provider config)`,
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
        const cfg = buildConfigForProvider(providerFromPrimary, hit.apiKey, {
          modelOverride,
          primaryModelHint: modelFromPrimary,
        });
        if (cfg) {
          _cachedConfig = cfg;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${cfg.model} (auth-profiles.json)`,
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
      const cfg = buildConfigForProvider(entry.provider, entry.apiKey, {
        modelOverride,
      });
      if (cfg) {
        _cachedConfig = cfg;
        _logger?.info?.(
          `TotalReclaw extraction LLM: resolved ${entry.provider}/${cfg.model} (auth-profiles.json)`,
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
        const cfg = buildConfigForProvider(providerFromPrimary, apiKey, {
          modelOverride,
          primaryModelHint: modelFromPrimary,
        });
        if (cfg) {
          _cachedConfig = cfg;
          _logger?.info?.(
            `TotalReclaw extraction LLM: resolved ${providerFromPrimary}/${cfg.model} (env var)`,
          );
          return;
        }
      }
    }
  }
  for (const [provider, keyName] of envFallback) {
    const apiKey = CONFIG.llmApiKeys[keyName];
    if (!apiKey) continue;
    const cfg = buildConfigForProvider(provider, apiKey, { modelOverride });
    if (cfg) {
      _cachedConfig = cfg;
      _logger?.info?.(`TotalReclaw extraction LLM: resolved ${provider}/${cfg.model} (env var)`);
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
      baseUrl: 'https://api.z.ai/api/coding/paas/v4',
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
 * Call the LLM chat completion endpoint.
 *
 * Supports both OpenAI-compatible format and Anthropic Messages API,
 * determined by `config.apiFormat`.
 *
 * @returns The assistant's response content, or null on failure.
 */
export async function chatCompletion(
  config: LLMClientConfig,
  messages: ChatMessage[],
  options?: { maxTokens?: number; temperature?: number },
): Promise<string | null> {
  const maxTokens = options?.maxTokens ?? 2048;
  const temperature = options?.temperature ?? 0; // Deterministic output for dedup (same input → same text → same content fingerprint)

  if (config.apiFormat === 'anthropic') {
    return chatCompletionAnthropic(config, messages, maxTokens, temperature);
  }

  return chatCompletionOpenAI(config, messages, maxTokens, temperature);
}

// ---------------------------------------------------------------------------
// OpenAI-compatible chat completion
// ---------------------------------------------------------------------------

async function chatCompletionOpenAI(
  config: LLMClientConfig,
  messages: ChatMessage[],
  maxTokens: number,
  temperature: number,
): Promise<string | null> {
  const url = `${config.baseUrl}/chat/completions`;

  const body: Record<string, unknown> = {
    model: config.model,
    messages,
    temperature,
    max_completion_tokens: maxTokens,
  };

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000), // 30 second timeout
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`LLM API ${res.status}: ${text.slice(0, 200)}`);
    }

    const json = (await res.json()) as ChatCompletionResponse;
    return json.choices?.[0]?.message?.content ?? null;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`LLM call failed: ${msg}`);
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
      signal: AbortSignal.timeout(30_000),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`Anthropic API ${res.status}: ${text.slice(0, 200)}`);
    }

    const json = (await res.json()) as AnthropicMessagesResponse;
    const textBlock = json.content?.find((block) => block.type === 'text');
    return textBlock?.text ?? null;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`LLM call failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Embedding (re-exported from local ONNX module)
// ---------------------------------------------------------------------------

// Embeddings are now generated locally via @huggingface/transformers
// (Harrier-OSS-v1-270M ONNX model). No API key needed.
// See embedding.ts for implementation details.
export { generateEmbedding, getEmbeddingDims } from './embedding.js';
