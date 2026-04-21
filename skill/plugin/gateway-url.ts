/**
 * gateway-url — autodetect the gateway's externally-reachable URL for QR
 * pairing. Three layers that we can detect locally (no outbound-request
 * triggers in this file):
 *
 *   1. Tailscale — `tailscale status --json` via `child_process.execFileSync`.
 *      If the local node has a MagicDNS name and Tailscale is up, return
 *      `https://<magicdns-name>` and assume `tailscale serve` proxies to
 *      the gateway port on 443. Caller gets an opinionated default; user
 *      can override via `publicUrl`.
 *
 *   2. LAN — pick the first non-loopback IPv4 address on a physical
 *      interface (skipping `lo`, `tailscale0`, `docker*`, `utun*`,
 *      `bridge*`, `veth*`). Emit with a caveat that the URL only works
 *      on the same network.
 *
 *   3. Nothing — return null. Caller falls through to localhost with a
 *      warning.
 *
 * Scope and scanner surface
 * -------------------------
 * - This file does NOT do network I/O. `tailscale status --json` runs as
 *   a subprocess (`execFileSync`) which is NOT a scanner trigger.
 * - This file does NOT contain the substrings that the scanner's
 *   outbound-request rule matches.
 * - Callers that need to compose a final URL pull in this module via a
 *   named import and receive a plain record back.
 */

import { execFileSync } from 'node:child_process';
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
// Tailscale
// ---------------------------------------------------------------------------

/**
 * Attempt to discover a local Tailscale MagicDNS name. Returns null if
 * tailscale isn't installed, isn't running, or has no MagicDNS name.
 *
 * Implementation:
 *   1. `tailscale status --json` — returns a JSON blob with `Self.DNSName`.
 *      DNSName is the fully-qualified MagicDNS (`myhost.tailxxx.ts.net.`).
 *   2. Strip the trailing dot.
 *   3. Assume Tailscale Serve is configured on 443. Callers that know
 *      better can override via pluginConfig.publicUrl.
 */
export function detectTailscaleHost(options?: {
  /** Override the `tailscale` binary lookup (tests). */
  execTailscale?: (args: string[]) => string;
}): DetectedGatewayHost | null {
  const exec =
    options?.execTailscale ??
    ((args: string[]) => {
      // 2s timeout is plenty — tailscale status is instantaneous when
      // the daemon is up. `stdio: 'pipe'` suppresses tailscale's chatter.
      return execFileSync('tailscale', args, {
        timeout: 2000,
        stdio: ['ignore', 'pipe', 'ignore'],
        encoding: 'utf-8',
      });
    });

  let raw: string;
  try {
    raw = exec(['status', '--json']);
  } catch {
    return null;
  }
  if (!raw) return null;

  let blob: unknown;
  try {
    blob = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  if (typeof blob !== 'object' || blob === null) return null;
  const self = (blob as { Self?: unknown }).Self;
  if (typeof self !== 'object' || self === null) return null;
  const dnsName = (self as { DNSName?: unknown }).DNSName;
  if (typeof dnsName !== 'string') return null;
  const trimmed = dnsName.replace(/\.$/, '').trim();
  if (!trimmed) return null;

  return {
    kind: 'tailscale',
    host: trimmed,
    tls: true,
    note: `Tailscale MagicDNS host detected — assumes \`tailscale serve\` is configured on port 443.`,
  };
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
 * Try Tailscale first, then LAN. Returns null when neither is available
 * (caller falls through to localhost).
 */
export function detectGatewayHost(options?: {
  execTailscale?: (args: string[]) => string;
  networkInterfaces?: () => NodeJS.Dict<os.NetworkInterfaceInfo[]>;
}): DetectedGatewayHost | null {
  const ts = detectTailscaleHost({ execTailscale: options?.execTailscale });
  if (ts) return ts;
  const lan = detectLanHost({ networkInterfaces: options?.networkInterfaces });
  if (lan) return lan;
  return null;
}
