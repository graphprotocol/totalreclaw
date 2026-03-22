/**
 * Session Manager
 *
 * Caches derived cryptographic keys in memory with a configurable timeout.
 * This avoids the expensive Argon2id derivation on every operation while
 * still enforcing a session lifetime for security.
 *
 * Security notes:
 * - Cached keys are held in plain Buffers (Node.js does not support
 *   memory-locked pages from userland). They are zeroed on invalidation.
 * - Session entries are automatically evicted after the configured timeout.
 * - The manager never persists keys to disk.
 */

import { deriveKeys } from '../crypto/kdf';
import type { KeyDerivationParams } from '../crypto/kdf';

/**
 * Derived key pair for a user session.
 */
export interface DerivedKeys {
  /** HKDF-derived authentication key (32 bytes) */
  authKey: Buffer;
  /** HKDF-derived encryption key (32 bytes) */
  encryptionKey: Buffer;
}

/**
 * Internal cache entry.
 */
interface SessionEntry {
  keys: DerivedKeys;
  /** Timestamp (ms) when this entry was cached */
  cachedAt: number;
  /** Handle for the expiry timer so we can cancel it */
  timer: ReturnType<typeof setTimeout>;
}

/**
 * Configuration for the SessionManager.
 */
export interface SessionManagerConfig {
  /** Session timeout in milliseconds (default: 30 minutes) */
  timeoutMs?: number;
  /** KDF parameters forwarded to Argon2id (optional overrides) */
  kdfParams?: KeyDerivationParams;
}

/** Default session timeout: 30 minutes */
const DEFAULT_TIMEOUT_MS = 30 * 60 * 1000;

/**
 * SessionManager caches derived keys per userId with automatic expiry.
 *
 * @example
 * ```typescript
 * const session = new SessionManager({ timeoutMs: 15 * 60 * 1000 });
 *
 * // First call derives keys (slow — Argon2id)
 * const keys = await session.getOrDeriveKeys(userId, salt, masterPassword);
 *
 * // Subsequent calls return cached keys (fast)
 * const cached = await session.getOrDeriveKeys(userId, salt);
 * ```
 */
export class SessionManager {
  private cache: Map<string, SessionEntry> = new Map();
  private timeoutMs: number;
  private kdfParams: KeyDerivationParams;

  constructor(config: SessionManagerConfig = {}) {
    this.timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.kdfParams = config.kdfParams ?? {};

    if (this.timeoutMs <= 0) {
      throw new Error('Session timeout must be a positive number of milliseconds');
    }
  }

  /**
   * Return cached keys for the given user, or derive new ones.
   *
   * If keys exist in the cache and have not expired, they are returned
   * immediately. Otherwise, `masterPassword` and `salt` are required to
   * perform a fresh Argon2id derivation.
   *
   * @param userId         - The user identifier
   * @param salt           - The salt used during key derivation
   * @param masterPassword - Required on first call or after expiry
   * @returns The derived auth + encryption key pair
   */
  async getOrDeriveKeys(
    userId: string,
    salt: Buffer,
    masterPassword?: string
  ): Promise<DerivedKeys> {
    // Check cache
    const existing = this.cache.get(userId);
    if (existing && !this.isExpired(existing)) {
      return existing.keys;
    }

    // Cache miss or expired — need the recovery phrase
    if (!masterPassword) {
      throw new Error(
        'Session expired or not yet established. Recovery phrase is required to derive keys.'
      );
    }

    // Derive fresh keys
    const keys = await deriveKeys(masterPassword, salt, this.kdfParams);

    // Store in cache (replaces any stale entry)
    this.cacheKeys(userId, keys);

    return keys;
  }

  /**
   * Invalidate (clear) the cached session for a user.
   *
   * Zeroes the key buffers before removing the entry.
   *
   * @param userId - The user identifier
   */
  invalidateSession(userId: string): void {
    const entry = this.cache.get(userId);
    if (entry) {
      clearTimeout(entry.timer);
      this.zeroKeys(entry.keys);
      this.cache.delete(userId);
    }
  }

  /**
   * Invalidate all cached sessions.
   */
  invalidateAll(): void {
    for (const [userId] of this.cache) {
      this.invalidateSession(userId);
    }
  }

  /**
   * Check whether a valid (non-expired) session exists for a user.
   *
   * @param userId - The user identifier
   */
  hasSession(userId: string): boolean {
    const entry = this.cache.get(userId);
    if (!entry) return false;
    if (this.isExpired(entry)) {
      // Evict stale entry eagerly
      this.invalidateSession(userId);
      return false;
    }
    return true;
  }

  /**
   * Return the number of active (non-expired) sessions.
   */
  get activeSessionCount(): number {
    let count = 0;
    for (const [, entry] of this.cache) {
      if (!this.isExpired(entry)) {
        count++;
      }
    }
    return count;
  }

  /**
   * Return the configured timeout in milliseconds.
   */
  get sessionTimeoutMs(): number {
    return this.timeoutMs;
  }

  // ---------------------------------------------------------------------------
  // Private helpers
  // ---------------------------------------------------------------------------

  private isExpired(entry: SessionEntry): boolean {
    return Date.now() - entry.cachedAt >= this.timeoutMs;
  }

  private cacheKeys(userId: string, keys: DerivedKeys): void {
    // Clean up any existing entry first
    this.invalidateSession(userId);

    const timer = setTimeout(() => {
      this.invalidateSession(userId);
    }, this.timeoutMs);

    // Allow the Node.js process to exit even if timers are pending
    if (timer && typeof timer === 'object' && 'unref' in timer) {
      timer.unref();
    }

    this.cache.set(userId, {
      keys,
      cachedAt: Date.now(),
      timer,
    });
  }

  /**
   * Zero out key buffers to minimize exposure in memory.
   */
  private zeroKeys(keys: DerivedKeys): void {
    if (Buffer.isBuffer(keys.authKey)) {
      keys.authKey.fill(0);
    }
    if (Buffer.isBuffer(keys.encryptionKey)) {
      keys.encryptionKey.fill(0);
    }
  }
}
