// ---------------------------------------------------------------------------
// Pure formatting / resolution helpers
// ---------------------------------------------------------------------------
//
// Extracted from index.ts. Every function here is a pure computation over its
// arguments (plus imported constants / stateless helpers) — none reads the
// plugin's mutable session state, so they carve cleanly out of the composing
// entry point. No environment-variable reads live here (they stay in config.ts /
// entry.ts per the OpenClaw env-harvesting scanner rule).

import { encrypt, decrypt } from '../crypto/crypto.js';
import { detectGatewayHost } from '../billing/gateway-url.js';
import { isRunningInDocker } from '../fs-helpers.js';
import { readBillingCache } from '../billing/billing-cache.js';
import type { GatewayMode } from '../pairing/first-run.js';
import type { OpenClawPluginApi } from './types.js';

// ---------------------------------------------------------------------------
// Human-friendly error messages
// ---------------------------------------------------------------------------

/**
 * Translate technical error messages from the on-chain submission pipeline
 * into user-friendly messages. The original technical details are still
 * logged via api.logger — this only affects what the agent sees.
 */
export function humanizeError(rawMessage: string): string {
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
// 3.3.0 — pairing URL resolution
// ---------------------------------------------------------------------------

/**
 * Build the full pairing URL (including `#pk=` fragment) for a fresh
 * pairing session. Pulls gateway config from `api.config.gateway`.
 *
 * Resolution order (3.3.1 — six-layer cascade):
 *   1. `plugins.entries.totalreclaw.config.publicUrl` — explicit override
 *   2. `gateway.remote.url` — OpenClaw's own remote-gateway URL
 *   3. `gateway.bind === 'custom'` + `gateway.customBindHost` + port
 *   4. Tailscale auto-detect — `tailscale status --json` → `https://<MagicDNS>`
 *      (assumes `tailscale serve` proxies to the gateway port on 443)
 *   5. LAN auto-detect — first non-loopback, non-virtual IPv4 interface.
 *      Emits a warning: "only works on the same network".
 *   6. Fallback `http://localhost:<port>` — warns with a pointer to
 *      configure `plugins.entries.totalreclaw.config.publicUrl`.
 *
 * Always returns a working URL string; never throws. The caller (CLI or
 * JSON output) prints whatever we give it.
 */
export function buildPairingUrl(
  api: Pick<OpenClawPluginApi, 'config' | 'pluginConfig' | 'logger'>,
  session: { sid: string; pkGatewayB64: string },
): string {
  const cfg = api.config as {
    gateway?: {
      port?: number;
      bind?: string;
      customBindHost?: string;
      tls?: { enabled?: boolean };
      remote?: { url?: string };
    };
  } | undefined;
  const pluginCfg = (api.pluginConfig ?? {}) as { publicUrl?: string };

  const tlsEnabled = cfg?.gateway?.tls?.enabled === true;
  const scheme = tlsEnabled ? 'https' : 'http';
  const port = cfg?.gateway?.port ?? 18789;

  let base: string;

  // Layer 1 — explicit user override
  if (typeof pluginCfg.publicUrl === 'string' && pluginCfg.publicUrl.trim()) {
    base = pluginCfg.publicUrl.replace(/\/+$/, '');
    base = base.replace(/^wss:\/\//i, 'https://').replace(/^ws:\/\//i, 'http://');
  }
  // Layer 2 — OpenClaw gateway remote URL
  else if (typeof cfg?.gateway?.remote?.url === 'string' && cfg.gateway.remote.url.trim()) {
    base = cfg.gateway.remote.url.trim().replace(/\/+$/, '');
    base = base.replace(/^wss:\/\//i, 'https://').replace(/^ws:\/\//i, 'http://');
  }
  // Layer 3 — gateway.bind = custom + explicit customBindHost
  else if (cfg?.gateway?.bind === 'custom' && cfg.gateway.customBindHost) {
    base = `${scheme}://${cfg.gateway.customBindHost}:${port}`;
  }
  // Layers 4 + 5 — auto-detect via gateway-url helper (Tailscale CGNAT, then LAN)
  else {
    let detected: ReturnType<typeof detectGatewayHost> = null;
    // issue #110 fix 4 — pass `isDocker` so LAN detection skips
    // 172.16/12 bridge IPs that no external browser can reach.
    let isDocker = false;
    try {
      isDocker = isRunningInDocker();
    } catch {
      // Defensive: never block URL building on Docker sniff errors.
      isDocker = false;
    }
    try {
      detected = detectGatewayHost({ isDocker });
    } catch (err) {
      api.logger.warn(
        `TotalReclaw: host autodetect crashed: ${err instanceof Error ? err.message : String(err)} — falling back to localhost`,
      );
    }
    if (detected?.kind === 'tailscale') {
      // 3.3.1-rc.2: we surface the raw Tailscale CGNAT IP because passive
      // NIC detection (no subprocess) cannot resolve the MagicDNS name.
      // Caller can override via `publicUrl` for a proper https://<magicdns>.
      // The IP + port URL still works inside the tailnet (peers can reach
      // each other by CGNAT IP directly). TLS defaults to the gateway's
      // own config because we no longer assume `tailscale serve`.
      base = `${scheme}://${detected.host}:${port}`;
      api.logger.warn(
        `TotalReclaw: pairing URL using Tailscale CGNAT IP ${detected.host}:${port} — ` +
          detected.note,
      );
    } else if (detected?.kind === 'lan') {
      base = `${scheme}://${detected.host}:${port}`;
      api.logger.warn(
        `TotalReclaw: pairing URL using LAN host ${detected.host}:${port} — ` +
          `this URL only works from the same network. ` +
          `Set plugins.entries.totalreclaw.config.publicUrl for remote access.`,
      );
    } else {
      // Layer 6 — localhost fallback (or Docker-aware relay-pointer warning)
      const bind = cfg?.gateway?.bind;
      if (isDocker) {
        // issue #110 fix 4: inside Docker the LAN IP is container-internal
        // and useless. Loopback localhost only works for `docker exec`
        // tests. The CORRECT pair URL for Docker is the relay-brokered
        // path served by `tr pair` / the `/plugin/totalreclaw/pair/*` HTTP
        // routes (CONFIG.pairMode === 'relay' since rc.11). The CLI-only
        // path here cannot mint a relay session synchronously (the relay
        // handshake needs a WS round-trip), so we emit the loopback URL
        // with a LOUD warning pointing the operator at the pair CLI /
        // publicUrl override.
        api.logger.warn(
          `TotalReclaw: Docker container detected — pairing URL falling back to ` +
            `http://localhost:${port}, which is unreachable from the host browser. ` +
            `Run \`tr pair --url-pin\` (or \`openclaw totalreclaw pair generate --url-pin-only\`) ` +
            `on the gateway host to mint a relay-brokered pair URL that reaches the host browser, ` +
            `OR set plugins.entries.totalreclaw.config.publicUrl ` +
            `to your gateway's host-reachable URL (e.g. http://<host-ip>:${port} when the ` +
            `Docker port is published). Setting TOTALRECLAW_PAIR_MODE=relay is the default; ` +
            `air-gapped operators on TOTALRECLAW_PAIR_MODE=local must publish a port + set publicUrl.`,
        );
      } else if (bind === 'lan' || bind === 'tailnet') {
        api.logger.warn(
          `TotalReclaw: pairing URL falling back to localhost because gateway.bind=${bind} could not be autodetected. ` +
            'Set plugins.entries.totalreclaw.config.publicUrl to override.',
        );
      } else {
        api.logger.warn(
          `TotalReclaw: pairing URL fell back to http://localhost:${port} — this URL only works on this machine. ` +
            `Configure plugins.entries.totalreclaw.config.publicUrl for remote access.`,
        );
      }
      base = `${scheme}://localhost:${port}`;
    }
  }

  return `${base}/plugin/totalreclaw/pair/finish?sid=${encodeURIComponent(session.sid)}#pk=${encodeURIComponent(session.pkGatewayB64)}`;
}

/**
 * Resolve whether this plugin is running on a `local` or `remote` gateway.
 *
 * Follows the same config surface `buildPairingUrl` uses:
 *   - `pluginConfig.publicUrl` set + non-localhost     → remote
 *   - `gateway.remote.url` set + non-localhost         → remote
 *   - `gateway.bind === 'lan' | 'tailnet' | 'custom'`  → remote
 *   - anything else                                    → local
 *
 * We treat a `publicUrl` or `remote.url` that points at `localhost` /
 * `127.*` as local because that is what a dev-loopback override looks like;
 * no one publishes a remote QR pairing for localhost.
 */
export function resolveGatewayMode(
  api: Pick<OpenClawPluginApi, 'config' | 'pluginConfig'>,
): GatewayMode {
  const cfg = api.config as
    | { gateway?: { bind?: string; remote?: { url?: string } } }
    | undefined;
  const pluginCfg = (api.pluginConfig ?? {}) as { publicUrl?: string };
  const looksLocal = (url: string | undefined): boolean => {
    if (!url) return true;
    const u = url.trim().toLowerCase();
    if (u === '') return true;
    return /^(?:wss?:\/\/|https?:\/\/)?(?:localhost|127\.|0\.0\.0\.0)/.test(u);
  };
  if (typeof pluginCfg.publicUrl === 'string' && !looksLocal(pluginCfg.publicUrl)) {
    return 'remote';
  }
  const remoteUrl = cfg?.gateway?.remote?.url;
  if (typeof remoteUrl === 'string' && !looksLocal(remoteUrl)) {
    return 'remote';
  }
  const bind = cfg?.gateway?.bind;
  if (bind === 'lan' || bind === 'tailnet' || bind === 'custom') {
    return 'remote';
  }
  return 'local';
}

// ---------------------------------------------------------------------------
// Candidate-pool sizing
// ---------------------------------------------------------------------------

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
export function computeCandidatePool(factCount: number): number {
  const cache = readBillingCache();
  if (cache?.features?.max_candidate_pool != null) return cache.features.max_candidate_pool;
  // Fallback to local formula if no server config
  return Math.min(Math.max(factCount * 3, 400), 5000);
}

// ---------------------------------------------------------------------------
// Encrypt/decrypt hex helpers (on-chain blob encoding)
// ---------------------------------------------------------------------------

export function encryptToHex(plaintext: string, key: Buffer): string {
  const b64 = encrypt(plaintext, key);
  return Buffer.from(b64, 'base64').toString('hex');
}

export function decryptFromHex(hexBlob: string, key: Buffer): string {
  const hex = hexBlob.startsWith('0x') ? hexBlob.slice(2) : hexBlob;
  const b64 = Buffer.from(hex, 'hex').toString('base64');
  return decrypt(b64, key);
}

// ---------------------------------------------------------------------------
// Lexical scoring / relative time
// ---------------------------------------------------------------------------

export function textScore(query: string, docText: string): number {
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
export function relativeTime(isoOrMs: string | number): string {
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
