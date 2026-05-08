/**
 * extraction/config.ts — env-var reads for the extraction LLM client.
 *
 * Mirrors the relevant subset of `skill/plugin/config.ts` (the plugin's
 * CONFIG object) so the ported `llm-client.ts` doesn't need to be edited
 * substantively. Scoped to ONLY the env reads the extraction pipeline
 * actually needs: provider API keys, zai base URL, retry budget.
 *
 * This file is a centralized env-read surface so future contributors can
 * find every TOTALRECLAW_/PROVIDER_API_KEY env touchpoint in one place
 * (matches the rationale of the plugin's config.ts).
 *
 * MCP-server-specific note: the plugin's config.ts has a scanner-isolation
 * rule that forbids mixing process.env reads with outbound-network triggers
 * in the same file. The MCP server is NOT scanner-gated (it's a regular
 * npm package run by node, not an OpenClaw plugin), so the rule doesn't
 * apply — but we keep the same structural separation anyway because it
 * makes the env surface auditable.
 */

export const EXTRACTION_CONFIG = {
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

  /**
   * zai base-URL override. Read via a getter so tests can mutate the env
   * between calls. Default is the coding endpoint (GLM Coding Plan); the
   * `chatCompletion` auto-fallback flips to the standard PAYG endpoint on
   * an "Insufficient balance" 429.
   */
  get zaiBaseUrl(): string {
    const override = process.env.ZAI_BASE_URL;
    if (override && override.trim()) return override.trim().replace(/\/+$/, '');
    return 'https://api.z.ai/api/coding/paas/v4';
  },

  /**
   * Retry budget for chatCompletion. Default 60s covers multi-minute
   * upstream outages. Read once at module load.
   */
  llmRetryBudgetMs: (() => {
    const raw = process.env.TOTALRECLAW_LLM_RETRY_BUDGET_MS;
    const parsed = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 60_000;
  })(),
};
