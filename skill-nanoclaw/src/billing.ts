/**
 * TotalReclaw NanoClaw - Billing Utilities
 *
 * Provides billing status caching, quota warning detection, and 403 handling
 * for NanoClaw hooks. Mirrors the OpenClaw plugin's billing awareness:
 *
 *   - before-agent-start: fetch/cache billing status, inject quota warnings
 *   - agent-end: invalidate cache on 403/quota errors so next start re-fetches
 *
 * Key derivation delegates to @totalreclaw/core (Rust WASM) which performs:
 *   mnemonic -> BIP-39 seed (512 bits)
 *   salt = seed[0..32]
 *   authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)
 */

import * as fs from 'fs';
import * as path from 'path';
import * as wasm from '@totalreclaw/core';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BILLING_CACHE_DIR = path.join(process.env.HOME ?? '/home/node', '.totalreclaw');
const BILLING_CACHE_PATH = path.join(BILLING_CACHE_DIR, 'billing-cache.json');
const BILLING_CACHE_TTL = 2 * 60 * 60 * 1000; // 2 hours
const QUOTA_WARNING_THRESHOLD = 0.8; // 80%

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface BillingCache {
  tier: string;
  free_writes_used: number;
  free_writes_limit: number;
  features?: {
    llm_dedup?: boolean;
    extraction_interval?: number;
    max_facts_per_extraction?: number;
    max_candidate_pool?: number;
  };
  checked_at: number;
}

export interface BillingContext {
  serverUrl: string;
  authKeyHex: string;
  walletAddress: string;
}

// ---------------------------------------------------------------------------
// Key Derivation (BIP-39 seed path -- synchronous, no RPC needed)
// ---------------------------------------------------------------------------

/**
 * Derive the auth key hex from a BIP-39 mnemonic.
 *
 * Delegates to the Rust WASM core which performs:
 *   mnemonic -> BIP-39 seed (512 bits)
 *   salt = seed[0..32]
 *   authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)
 *
 * This is synchronous and does not require RPC connectivity.
 */
export function deriveAuthKeyHex(mnemonic: string): string {
  const result = wasm.deriveKeysFromMnemonic(mnemonic.trim());
  return result.auth_key;
}

// ---------------------------------------------------------------------------
// Smart Account Address Derivation
// ---------------------------------------------------------------------------

/**
 * Derive the Smart Account address from a BIP-39 mnemonic.
 *
 * This requires an RPC call to compute the counterfactual CREATE2 address,
 * so results are cached to disk after the first derivation.
 *
 * Falls back to an empty string if derivation fails (e.g. no network).
 * Can also be set explicitly via TOTALRECLAW_WALLET_ADDRESS env var.
 */
let cachedWalletAddress: string | null = null;
const WALLET_CACHE_PATH = path.join(BILLING_CACHE_DIR, 'wallet-address-cache.json');

export async function getWalletAddress(mnemonic: string, chainId: number = 84532): Promise<string> {
  // Check env var override first
  if (process.env.TOTALRECLAW_WALLET_ADDRESS) {
    return process.env.TOTALRECLAW_WALLET_ADDRESS.toLowerCase();
  }

  if (cachedWalletAddress) return cachedWalletAddress;

  // Try reading from disk cache first
  try {
    if (fs.existsSync(WALLET_CACHE_PATH)) {
      const cached = JSON.parse(fs.readFileSync(WALLET_CACHE_PATH, 'utf-8'));
      if (cached.address && cached.chainId === chainId) {
        cachedWalletAddress = cached.address;
        return cachedWalletAddress;
      }
    }
  } catch {
    // Cache read failed -- derive fresh
  }

  // Derive via RPC using the client library's seed module.
  // Dynamic import from the specific subpath since the barrel export
  // doesn't always re-export the seed module.
  try {
    let mnemonicToSmartAccountAddress: (mnemonic: string, chainId?: number) => Promise<string>;
    try {
      // Try the barrel export first (preferred)
      const clientLib = await import('@totalreclaw/client');
      mnemonicToSmartAccountAddress = (clientLib as any).mnemonicToSmartAccountAddress;
    } catch {
      // Not available via barrel -- this means the export chain is broken
      mnemonicToSmartAccountAddress = undefined as any;
    }

    if (!mnemonicToSmartAccountAddress) {
      console.warn('[billing] mnemonicToSmartAccountAddress not available -- skipping wallet derivation');
      return '';
    }

    const address = await mnemonicToSmartAccountAddress(mnemonic.trim(), chainId);
    cachedWalletAddress = address.toLowerCase();

    // Persist to disk cache
    try {
      if (!fs.existsSync(BILLING_CACHE_DIR)) {
        fs.mkdirSync(BILLING_CACHE_DIR, { recursive: true });
      }
      fs.writeFileSync(
        WALLET_CACHE_PATH,
        JSON.stringify({ address: cachedWalletAddress, chainId }),
      );
    } catch {
      // Best-effort cache write
    }

    return cachedWalletAddress;
  } catch (err) {
    console.error(
      `[billing] Failed to derive Smart Account address: ${err instanceof Error ? err.message : String(err)}`,
    );
    return '';
  }
}

// ---------------------------------------------------------------------------
// Billing Cache Operations
// ---------------------------------------------------------------------------

export function readBillingCache(): BillingCache | null {
  try {
    if (!fs.existsSync(BILLING_CACHE_PATH)) return null;
    const raw = JSON.parse(fs.readFileSync(BILLING_CACHE_PATH, 'utf-8')) as BillingCache;
    if (!raw.checked_at || Date.now() - raw.checked_at > BILLING_CACHE_TTL) return null;
    return raw;
  } catch {
    return null;
  }
}

export function writeBillingCache(cache: BillingCache): void {
  try {
    if (!fs.existsSync(BILLING_CACHE_DIR)) {
      fs.mkdirSync(BILLING_CACHE_DIR, { recursive: true });
    }
    fs.writeFileSync(BILLING_CACHE_PATH, JSON.stringify(cache));
  } catch {
    // Best-effort -- don't block on cache write failure.
  }
}

export function deleteBillingCache(): void {
  try {
    fs.unlinkSync(BILLING_CACHE_PATH);
  } catch {
    // Ignore -- file may not exist.
  }
}

// ---------------------------------------------------------------------------
// Billing Status Fetch
// ---------------------------------------------------------------------------

/**
 * Fetch billing status from the relay and update the cache.
 *
 * @returns The billing cache (fresh or existing), or null on failure.
 */
export async function fetchBillingStatus(ctx: BillingContext): Promise<BillingCache | null> {
  // Return cached if valid
  const existing = readBillingCache();
  if (existing) return existing;

  if (!ctx.authKeyHex || !ctx.walletAddress) return null;

  try {
    const url = `${ctx.serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(ctx.walletAddress)}`;
    const resp = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${ctx.authKeyHex}`,
        'Accept': 'application/json',
        'X-TotalReclaw-Client': 'nanoclaw-skill',
      },
    });

    if (!resp.ok) return null;

    const data = await resp.json() as Record<string, unknown>;
    const cache: BillingCache = {
      tier: (data.tier as string) || 'free',
      free_writes_used: (data.free_writes_used as number) ?? 0,
      free_writes_limit: (data.free_writes_limit as number) ?? 0,
      features: data.features as BillingCache['features'] | undefined,
      checked_at: Date.now(),
    };

    writeBillingCache(cache);
    return cache;
  } catch {
    // Best-effort -- don't block on billing check failure.
    return null;
  }
}

// ---------------------------------------------------------------------------
// Quota Warning Generation
// ---------------------------------------------------------------------------

/**
 * Generate a quota warning string if usage exceeds the threshold.
 *
 * @returns Warning text to inject into agent context, or empty string.
 */
export function getQuotaWarning(cache: BillingCache | null): string {
  if (!cache || cache.free_writes_limit <= 0) return '';

  const usageRatio = cache.free_writes_used / cache.free_writes_limit;
  if (usageRatio >= QUOTA_WARNING_THRESHOLD) {
    return `\n\nTotalReclaw quota warning: ${cache.free_writes_used}/${cache.free_writes_limit} writes used this month (${Math.round(usageRatio * 100)}%). Use the totalreclaw_upgrade tool to upgrade to Pro.`;
  }

  return '';
}

/**
 * Check if the user has an active Pro subscription (for welcome-back message).
 */
export async function checkWelcomeBack(ctx: BillingContext): Promise<string> {
  try {
    const url = `${ctx.serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(ctx.walletAddress)}`;
    const resp = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${ctx.authKeyHex}`,
        'Accept': 'application/json',
        'X-TotalReclaw-Client': 'nanoclaw-skill',
      },
    });

    if (!resp.ok) return '';

    const data = await resp.json() as Record<string, unknown>;
    const tier = data.tier as string;
    const expiresAt = data.expires_at as string | undefined;

    // Populate billing cache as a side-effect.
    writeBillingCache({
      tier: tier || 'free',
      free_writes_used: (data.free_writes_used as number) ?? 0,
      free_writes_limit: (data.free_writes_limit as number) ?? 0,
      features: data.features as BillingCache['features'] | undefined,
      checked_at: Date.now(),
    });

    if (tier === 'pro' && expiresAt) {
      return `\n\nWelcome back! Your TotalReclaw Pro subscription is active (expires ${expiresAt}). All memories are stored on Gnosis mainnet.`;
    }

    return '';
  } catch {
    return '';
  }
}

// ---------------------------------------------------------------------------
// 403 / Quota Error Detection
// ---------------------------------------------------------------------------

/**
 * Check if an error indicates a quota/403 issue. If so, invalidate the
 * billing cache so the next before-agent-start re-fetches fresh status.
 *
 * @returns true if this was a quota error (caller should stop retrying).
 */
export function handleQuotaError(err: unknown): boolean {
  const errMsg = err instanceof Error ? err.message : String(err);
  if (errMsg.includes('403') || errMsg.toLowerCase().includes('quota')) {
    deleteBillingCache();
    console.warn(`[billing] Quota exceeded -- billing cache invalidated. ${errMsg}`);
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Feature Gating Helpers
// ---------------------------------------------------------------------------

/**
 * Get the effective extraction interval from billing cache or env fallback.
 */
export function getExtractInterval(): number {
  const cache = readBillingCache();
  if (cache?.features?.extraction_interval != null) return cache.features.extraction_interval;
  return parseInt(process.env.TOTALRECLAW_EXTRACT_INTERVAL || '3', 10);
}

/**
 * Get the max facts per extraction from billing cache or constant fallback.
 */
export function getMaxFactsPerExtraction(): number {
  const cache = readBillingCache();
  if (cache?.features?.max_facts_per_extraction != null) return cache.features.max_facts_per_extraction;
  return 15;
}

// ---------------------------------------------------------------------------
// Billing Context Construction
// ---------------------------------------------------------------------------

/**
 * Build a BillingContext from environment variables.
 *
 * Returns null if the recovery phrase is not configured (self-hosted mode
 * without mnemonic doesn't need billing).
 */
let cachedBillingContext: BillingContext | null | undefined;

export async function getBillingContext(): Promise<BillingContext | null> {
  if (cachedBillingContext !== undefined) return cachedBillingContext;

  const mnemonic = process.env.TOTALRECLAW_RECOVERY_PHRASE;
  const serverUrl = process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz';

  if (!mnemonic || mnemonic.trim().split(/\s+/).length < 12) {
    cachedBillingContext = null;
    return null;
  }

  try {
    const authKeyHex = deriveAuthKeyHex(mnemonic);
    // Chain ID is not user-configurable in v1 — Smart Account derivation is
    // deterministic via CREATE2 so the address is the same on every chain;
    // tier routing (free = Base Sepolia, Pro = Gnosis) happens at the relay.
    const chainId = 84532;
    const walletAddress = await getWalletAddress(mnemonic, chainId);

    // Register auth key with relay (idempotent — relay returns 200 for existing users).
    // Without this, all relay queries return 401 when using env var mnemonic path.
    try {
      const keys = wasm.deriveKeysFromMnemonic(mnemonic.trim());
      const authKeyHash = wasm.computeAuthKeyHash(keys.auth_key);
      const saltHex = keys.salt;
      const regUrl = `${serverUrl.replace(/\/+$/, '')}/v1/register`;
      await fetch(regUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-TotalReclaw-Client': 'nanoclaw-skill',
        },
        body: JSON.stringify({ auth_key_hash: authKeyHash, salt: saltHex }),
      });
    } catch {
      // Best-effort — relay may be unreachable; will retry on next startup.
    }

    cachedBillingContext = { serverUrl, authKeyHex, walletAddress };
    return cachedBillingContext;
  } catch (err) {
    console.error(
      `[billing] Failed to build billing context: ${err instanceof Error ? err.message : String(err)}`,
    );
    cachedBillingContext = null;
    return null;
  }
}
