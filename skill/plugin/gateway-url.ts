/**
 * gateway-url — autodetect the gateway's externally-reachable URL for QR
 * pairing. This module runs sync + network-I/O-free so the OpenClaw
 * dangerous-code scanner never flags it (the 3.3.1-rc.1 implementation
 * used `child-process.execFileSync('tailscale', ...)` which blocked every
 * `openclaw plugins install` — see QA report
 * `docs/notes/QA-plugin-3.3.1-rc.1-20260422-0121.md`).
 *
 * Two layers:
 *
 *   1. Tailscale — PASSIVE detection via `os.networkInterfaces()`. If a
 *      `tailscale*` NIC has a CGNAT IPv4 (100.64/10), we return that IP
 *      as an auto-detected host — the operator can verify + override via
 *      `plugins.entries.totalreclaw.config.publicUrl` when they want a
 *      proper MagicDNS hostname. We DO NOT call `tailscale` the CLI —
 *      that requires `child-process` which the scanner blocks.
 *
 *   2. LAN — first non-loopback, non-virtual IPv4 interface. Emit with a
 *      caveat that the URL only works on the same network.
 *
 *   3. Null — no signal; caller falls through to localhost with a warning.
 *
 * The caller is expected to surface `detected.note` to the operator and
 * tell them to set `publicUrl` when auto-detect isn't good enough
 * (remote-accessible https, MagicDNS, etc.).
 *
 * Scope and scanner surface
 * -------------------------
 * - No `child-process` import — the original scanner-blocking flaw.
 * - No `fetch` / `post` / `http.request` substrings — the potential-
 *   exfiltration rule is also clear.
 * - Only `node:os` (synchronous, local) is used; no disk reads, no
 *   subprocess execution, no network calls.
 */

import os from 'node:os';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DetectedGatewayHost {
  /** Kind of host detected — determines warning copy. */
  kind: 'tailscale' | 'lan';
  /** Host (no scheme, no port). */
  host: string;
  /** If true, assume TLS (https + port 443). */
  tls: boolean;
  /** Explanatory string the caller can surface to the operator. */
  note?: string;
}

// ---------------------------------------------------------------------------
// Tailscale — passive detection (no subprocess, no network I/O)
// ---------------------------------------------------------------------------

/** CGNAT range 100.64.0.0/10 — Tailscale assigns IPs here by default. */
function isTailscaleCGNAT(addr: string): boolean {
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(addr)) return false;
  const parts = addr.split('.').map((p) => Number.parseInt(p, 10));
  if (parts[0] !== 100) return false;
  return parts[1] >= 64 && parts[1] <= 127;
}

/**
 * Passive Tailscale detection — checks `os.networkInterfaces()` for a
 * `tailscale*` NIC carrying a CGNAT IPv4. Returns null if not found.
 *
 * Unlike rc.1, this does NOT shell out to `tailscale status` — that
 * tripped the OpenClaw scanner's dangerous-code detector and blocked
 * install. The trade-off: we surface the raw CGNAT IP instead of the
 * MagicDNS hostname. Operators who want a MagicDNS host must set
 * `plugins.entries.totalreclaw.config.publicUrl` explicitly (documented
 * in SKILL.md).
 */
export function detectTailscaleHost(options?: {
  /** Override os.networkInterfaces for tests. */
  networkInterfaces?: () => NodeJS.Dict<os.NetworkInterfaceInfo[]>;
}): DetectedGatewayHost | null {
  const nif = (options?.networkInterfaces ?? os.networkInterfaces)();
  for (const [name, addrs] of Object.entries(nif)) {
    if (!name.toLowerCase().startsWith('tailscale')) continue;
    if (!addrs) continue;
    for (const a of addrs) {
      if (a.family !== 'IPv4' || a.internal) continue;
      if (isTailscaleCGNAT(a.address)) {
        return {
          kind: 'tailscale',
          host: a.address,
          tls: false,
          note:
            `Tailscale CGNAT IP detected on interface ${name}. For a proper ` +
            `https://<magicdns>.ts.net URL, set plugins.entries.totalreclaw.config.publicUrl ` +
            `(Tailscale CLI auto-resolution was removed in 3.3.1-rc.2 to pass the ` +
            `OpenClaw security scanner).`,
        };
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// LAN autodetect
// ---------------------------------------------------------------------------

/** Interfaces we explicitly skip — these are virtual / tunneled. */
const SKIP_IFACE_PREFIXES = [
  'lo',
  'tailscale',
  'docker',
  'br-',
  'bridge',
  'veth',
  'utun',
  'vmnet',
  'ovpn',
  'wg',
  'virbr',
  'tun',
  'ham',
];

function shouldSkipIface(name: string): boolean {
  const lower = name.toLowerCase();
  return SKIP_IFACE_PREFIXES.some((p) => lower.startsWith(p));
}

/**
 * Pick the first non-loopback, non-virtual IPv4 address. Returns null if
 * none found (headless VPS with only lo + tailscale, for example).
 */
export function detectLanHost(options?: {
  /** Override os.networkInterfaces for tests. */
  networkInterfaces?: () => NodeJS.Dict<os.NetworkInterfaceInfo[]>;
}): DetectedGatewayHost | null {
  const nif = (options?.networkInterfaces ?? os.networkInterfaces)();
  for (const [name, addrs] of Object.entries(nif)) {
    if (shouldSkipIface(name)) continue;
    if (!addrs) continue;
    for (const a of addrs) {
      if (a.family === 'IPv4' && !a.internal) {
        return {
          kind: 'lan',
          host: a.address,
          tls: false,
          note: `LAN IPv4 on interface ${name} — only reachable from the same network.`,
        };
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Composed resolver
// ---------------------------------------------------------------------------

/**
 * Try Tailscale first (passive NIC probe), then LAN. Returns null when
 * neither is available (caller falls through to localhost).
 *
 * Sync: no I/O, no subprocess, no network. Safe in sync callers like
 * `buildPairingUrl` in index.ts.
 */
export function detectGatewayHost(options?: {
  networkInterfaces?: () => NodeJS.Dict<os.NetworkInterfaceInfo[]>;
}): DetectedGatewayHost | null {
  const ts = detectTailscaleHost({ networkInterfaces: options?.networkInterfaces });
  if (ts) return ts;
  const lan = detectLanHost({ networkInterfaces: options?.networkInterfaces });
  if (lan) return lan;
  return null;
}
