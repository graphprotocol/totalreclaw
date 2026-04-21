/**
 * llm-profile-reader — read OpenClaw's `auth-profiles.json` to harvest
 * provider API keys when the plugin has no other source.
 *
 * Background
 * ----------
 * In 3.3.0-rc.6 and earlier, the plugin's `initLLMClient` only looked in:
 *   1. `api.config.providers` / `openclawProviders` passed by the SDK
 *   2. Env vars (`ZAI_API_KEY`, `OPENAI_API_KEY`, ...)
 *   3. Plugin-config override `extraction.llm`
 *
 * Real-world OpenClaw installs store user API keys in
 * `~/.openclaw/agents/<agent>/agent/auth-profiles.json`. None of the three
 * paths above reach that file, so the plugin silently logged
 * `No LLM available for auto-extraction` on every turn — auto-extraction
 * was a no-op for virtually every real user. See user-findings 3.3.0-rc.6.
 *
 * 3.3.1 adds this file as the fourth resolution tier, sitting between
 * "openclawProviders (SDK-passed)" and "env vars".
 *
 * Scope and scanner surface
 * -------------------------
 * This file does disk I/O. It MUST NOT contain the trigger substrings
 * used by OpenClaw's scanner (see `skill/scripts/check-scanner.mjs`) —
 * namely the outbound-request markers. All network work stays in
 * `llm-client.ts` and friends; this file only reads local files.
 *
 * File format (auth-profiles.json, OpenClaw canonical shape)
 * ----------------------------------------------------------
 *   {
 *     "profiles": {
 *       "openai:default": { "key": "sk-..." },
 *       "anthropic:default": { "key": "sk-ant-..." },
 *       "zai:default": { "key": "..." },
 *       ...
 *     }
 *   }
 *
 * We map the `<provider>:default` profile id to the canonical provider
 * name the plugin uses elsewhere (openai, anthropic, zai, gemini, etc.).
 * Non-default profile ids are ignored — a deliberate choice so users who
 * have multiple profiles (`openai:work`, `openai:personal`) see the one
 * they've explicitly flagged as `default`.
 */

import fs from 'node:fs';
import path from 'node:path';

// ---------------------------------------------------------------------------
// Provider-name normalization
// ---------------------------------------------------------------------------

/**
 * Map an auth-profile namespace (the part before `:` in a profile id like
 * `openai:default`) to the plugin's canonical provider name.
 */
const PROFILE_NS_TO_PROVIDER: Record<string, string> = {
  openai: 'openai',
  anthropic: 'anthropic',
  zai: 'zai',
  'z.ai': 'zai',
  google: 'gemini',
  gemini: 'gemini',
  mistral: 'mistral',
  groq: 'groq',
  deepseek: 'deepseek',
  openrouter: 'openrouter',
  xai: 'xai',
  'x.ai': 'xai',
  together: 'together',
  cerebras: 'cerebras',
};

// ---------------------------------------------------------------------------
// File discovery
// ---------------------------------------------------------------------------

/**
 * Default search root — `$HOME/.openclaw/agents`. Returns an empty
 * string when HOME is unset (avoids path.join crash on bare envs).
 */
export function defaultAuthProfilesRoot(homeDir: string | undefined): string {
  if (!homeDir) return '';
  return path.join(homeDir, '.openclaw', 'agents');
}

/**
 * Walk `$HOME/.openclaw/agents/*` (one level deep), each subdirectory
 * being an agent with potentially an `agent/auth-profiles.json`. Returns
 * every auth-profiles.json path that exists on disk, in alphabetical
 * order of the agent dir name (stable, so tests don't flake).
 *
 * Silently tolerates a missing root — returns [] instead of throwing.
 */
export function findAuthProfilesFiles(root: string): string[] {
  if (!root) return [];
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: string[] = [];
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    if (e.name.startsWith('.')) continue;
    const candidate = path.join(root, e.name, 'agent', 'auth-profiles.json');
    try {
      if (fs.statSync(candidate).isFile()) out.push(candidate);
    } catch {
      // missing or inaccessible — skip.
    }
  }
  out.sort();
  return out;
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/**
 * One provider-key entry harvested from auth-profiles.json.
 */
export interface AuthProfileKey {
  /** Canonical provider name (e.g. "openai", "anthropic", "zai"). */
  provider: string;
  /** The API key string (unmodified from disk — validated non-empty). */
  apiKey: string;
  /** Absolute path of the file the key was read from — used only for diagnostics. */
  sourcePath: string;
  /** Profile id the key came from (e.g. "openai:default"). */
  profileId: string;
}

/**
 * Parse one auth-profiles.json file into a list of (provider, apiKey)
 * entries, keeping only `<ns>:default` profiles and only those whose `key`
 * is a non-empty string. Unknown namespaces are skipped silently.
 */
export function parseAuthProfilesFile(filePath: string): AuthProfileKey[] {
  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch {
    return [];
  }
  let json: unknown;
  try {
    json = JSON.parse(raw) as unknown;
  } catch {
    return [];
  }
  if (typeof json !== 'object' || json === null) return [];
  const profilesField = (json as { profiles?: unknown }).profiles;
  if (typeof profilesField !== 'object' || profilesField === null) return [];
  const profiles = profilesField as Record<string, unknown>;

  const out: AuthProfileKey[] = [];
  for (const [profileId, entryRaw] of Object.entries(profiles)) {
    const parts = profileId.split(':');
    if (parts.length !== 2) continue;
    const [ns, suffix] = parts;
    if (suffix !== 'default') continue;
    const nsKey = ns.toLowerCase();
    const provider = PROFILE_NS_TO_PROVIDER[nsKey];
    if (!provider) continue;
    if (typeof entryRaw !== 'object' || entryRaw === null) continue;
    const keyField = (entryRaw as { key?: unknown }).key;
    if (typeof keyField !== 'string') continue;
    const trimmed = keyField.trim();
    if (!trimmed) continue;
    out.push({
      provider,
      apiKey: trimmed,
      sourcePath: filePath,
      profileId,
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Public aggregate
// ---------------------------------------------------------------------------

/**
 * Harvest every non-empty provider key from every
 * `~/.openclaw/agents/<agent>/agent/auth-profiles.json` on disk. Later files
 * (alphabetical) win for duplicate provider names — intentional so a
 * newly-added agent's keys shadow older ones. Callers that want the
 * single "first match per provider" list should run through
 * `dedupeByProvider` below.
 */
export function readAllAuthProfileKeys(options: {
  root: string;
}): AuthProfileKey[] {
  const files = findAuthProfilesFiles(options.root);
  const out: AuthProfileKey[] = [];
  for (const file of files) {
    const keys = parseAuthProfilesFile(file);
    out.push(...keys);
  }
  return out;
}

/**
 * Reduce a list of AuthProfileKey entries to one-per-provider, picking
 * the LAST one in list order (so later agent files override earlier ones
 * for the same provider).
 */
export function dedupeByProvider(entries: AuthProfileKey[]): Record<string, AuthProfileKey> {
  const map: Record<string, AuthProfileKey> = {};
  for (const e of entries) {
    map[e.provider] = e;
  }
  return map;
}

// ---------------------------------------------------------------------------
// Test hook
// ---------------------------------------------------------------------------

/** Internal — exposed for tests. */
export const __internal = {
  PROFILE_NS_TO_PROVIDER,
};
