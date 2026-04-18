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
 * Derive a cheap/fast model suitable for fact extraction, given the user's
 * provider and primary (potentially expensive) model.
 */
function deriveCheapModel(provider: string, primaryModel: string): string {
  // If already on a cheap model, use it as-is
  if (CHEAP_INDICATORS.some((s) => primaryModel.toLowerCase().includes(s))) {
    return primaryModel;
  }

  // Derive based on provider naming conventions
  switch (provider) {
    case 'zai': {
      // glm-5.1 -> glm-5-turbo (fast, available on coding endpoint)
      return 'glm-5-turbo';
    }
    case 'anthropic': {
      // claude-sonnet-4-5 -> claude-haiku-4-5-20251001
      return 'claude-haiku-4-5-20251001';
    }
    case 'openai': {
      // gpt-4.1 -> gpt-4.1-mini, gpt-4o -> gpt-4.1-mini
      return 'gpt-4.1-mini';
    }
    case 'gemini':
    case 'google': {
      return 'gemini-2.0-flash';
    }
    case 'mistral': {
      return 'mistral-small-latest';
    }
    case 'groq': {
      return 'llama-3.3-70b-versatile';
    }
    case 'deepseek': {
      return 'deepseek-chat';
    }
    case 'openrouter': {
      // Use Anthropic Haiku via OpenRouter (cheap and good at JSON)
      return 'anthropic/claude-haiku-4-5-20251001';
    }
    case 'xai': {
      return 'grok-2';
    }
    default: {
      // Fallback: try the primary model itself
      return primaryModel;
    }
  }
}

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

let _cachedConfig: LLMClientConfig | null = null;
let _initialized = false;
let _logger: { warn: (msg: string) => void } | null = null;

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the LLM client by detecting the provider from OpenClaw's config.
 * Called once from the plugin's `register()` function.
 *
 * Resolution order (highest priority first):
 *   1. Plugin config `extraction.model` (if provided)
 *   2. Auto-derived from provider heuristic using env var API keys
 *   3. OpenClaw's model provider config (api.config.models.providers)
 *   4. Fallback: try common env vars (ZAI_API_KEY, OPENAI_API_KEY) for dev/test
 *
 * The `TOTALRECLAW_LLM_MODEL` user-facing override was removed in v1 —
 * `deriveCheapModel(provider)` covers the 99% case and a model-level knob
 * was adding config surface for no tangible win.
 */
export function initLLMClient(options: {
  primaryModel?: string;
  pluginConfig?: Record<string, unknown>;
  openclawProviders?: Record<string, OpenClawProviderConfig>;
  logger?: { warn: (msg: string) => void };
}): void {
  _logger = options.logger ?? null;
  _initialized = true;
  _cachedConfig = null;

  const { primaryModel, pluginConfig, openclawProviders } = options;

  // Check if extraction is explicitly disabled
  const extraction = pluginConfig?.extraction as Record<string, unknown> | undefined;
  if (extraction?.enabled === false) {
    _logger?.warn('TotalReclaw: LLM extraction explicitly disabled via plugin config.');
    return;
  }

  // --- Try to resolve from primaryModel (auto-detect path) ---
  if (primaryModel) {
    const parts = primaryModel.split('/');
    const provider = parts.length >= 2 ? parts[0].toLowerCase() : '';
    const modelName = parts.length >= 2 ? parts.slice(1).join('/') : primaryModel;

    if (provider) {
      // Find the API key for this provider — first from env vars, then from
      // OpenClaw's provider config (api.config.models.providers)
      const keyNames = PROVIDER_KEY_NAMES[provider];
      let apiKey = keyNames
        ? keyNames.map((name) => CONFIG.llmApiKeys[name]).find(Boolean)
        : undefined;

      let baseUrl = PROVIDER_BASE_URLS[provider];

      // If no env var key found, check OpenClaw's provider config
      if (!apiKey && openclawProviders) {
        const ocProvider = openclawProviders[provider];
        if (ocProvider?.apiKey) {
          apiKey = ocProvider.apiKey;
          if (ocProvider.baseUrl) {
            baseUrl = ocProvider.baseUrl.replace(/\/+$/, '');
          }
        }
      }

      if (apiKey && baseUrl) {
        // Determine model: plugin config > auto-derived
        const model =
          (typeof extraction?.model === 'string' ? extraction.model : null) ||
          deriveCheapModel(provider, modelName);

        const apiFormat: 'openai' | 'anthropic' =
          provider === 'anthropic' ? 'anthropic' : 'openai';

        _cachedConfig = { apiKey, baseUrl, model, apiFormat };
        return;
      }
    }
  }

  // --- Fallback: try OpenClaw provider configs (any provider with an apiKey) ---
  if (openclawProviders) {
    for (const [providerName, providerConfig] of Object.entries(openclawProviders)) {
      if (!providerConfig?.apiKey) continue;

      const provider = providerName.toLowerCase();
      let baseUrl = providerConfig.baseUrl?.replace(/\/+$/, '') || PROVIDER_BASE_URLS[provider];
      if (!baseUrl) continue;

      // Pick a model from the provider's configured models, or use our default
      const firstModelId = providerConfig.models?.[0]?.id;
      const model =
        (typeof extraction?.model === 'string' ? extraction.model : null) ||
        (firstModelId ? deriveCheapModel(provider, firstModelId) : null);

      if (!model) continue;

      const apiFormat: 'openai' | 'anthropic' =
        providerConfig.api === 'anthropic-messages' || provider === 'anthropic'
          ? 'anthropic'
          : 'openai';

      _cachedConfig = { apiKey: providerConfig.apiKey, baseUrl, model, apiFormat };
      return;
    }
  }

  // --- Fallback: try common env var API keys (for dev/test without OpenClaw config) ---
  const fallbackProviders: Array<[string, string, string]> = [
    ['zai', 'zai', 'glm-4.5-flash'],
    ['openai', 'openai', 'gpt-4.1-mini'],
    ['anthropic', 'anthropic', 'claude-haiku-4-5-20251001'],
    ['gemini', 'gemini', 'gemini-2.0-flash'],
  ];

  for (const [provider, keyName, defaultModel] of fallbackProviders) {
    const apiKey = CONFIG.llmApiKeys[keyName];
    if (apiKey) {
      const model =
        (typeof extraction?.model === 'string' ? extraction.model : null) ||
        defaultModel;

      const apiFormat: 'openai' | 'anthropic' =
        provider === 'anthropic' ? 'anthropic' : 'openai';

      _cachedConfig = {
        apiKey,
        baseUrl: PROVIDER_BASE_URLS[provider],
        model,
        apiFormat,
      };
      return;
    }
  }

  // No LLM available
  _logger?.warn(
    'TotalReclaw: No LLM available for auto-extraction. ' +
    'Set an API key for your provider or configure extraction in plugin settings.',
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
