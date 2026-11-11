/**
 * Chain + DataEdge resolution for the managed-service (subgraph) write path.
 *
 * Client-consistency contract (#439, sibling of the OpenClaw plugin #402/#460):
 * the relay returns authoritative `chain_id` + `data_edge_address` in
 * `GET /v1/billing/status`; chain-aware clients consume them verbatim so a
 * future chain change needs zero client release. Both tiers run on Gnosis
 * (chain 100) after ops-1 — the legacy Free → Base Sepolia (84532) routing
 * was retired, so the local default here is 100, not 84532.
 */

import { DEFAULT_DATA_EDGE_ADDRESS, type SubgraphStoreConfig } from './store.js';

/** Default chain when billing is silent — Gnosis mainnet (single-chain, both tiers). */
const DEFAULT_CHAIN_ID = 100;

/** A DataEdge address is a 20-byte hex string. Reject anything else. */
const DATA_EDGE_RE = /^0x[0-9a-fA-F]{40}$/;

export interface ResolvedChainConfig {
  /** Chain to submit the write to (verbatim from billing, else Gnosis 100). */
  chainId: number;
  /** DataEdge contract from billing, or undefined to fall through to env/default. */
  dataEdgeAddress?: string;
}

/**
 * Resolve chain + DataEdge from a `/v1/billing/status` response.
 *
 * - `chain_id`: used verbatim when a finite number; otherwise Gnosis (100).
 * - `data_edge_address`: used only when a valid `0x…40hex` string; otherwise
 *   left undefined so the caller falls through to env / the store default.
 *
 * `onMalformedDataEdge` fires when `data_edge_address` is PRESENT but fails
 * the `0x…40hex` shape check — distinct from the field being simply absent
 * (a legitimate response shape for some relay versions/tiers). A present-
 * but-garbage value is evidence of a relay bug or a tampered response and
 * was previously dropped silently; callers that care (currently
 * `fetchChainConfig` below) should log it loudly rather than let a caller
 * silently fall through to the store's default DataEdge with no trace of
 * why.
 */
export function resolveChainConfig(
  billing: Record<string, unknown> | null | undefined,
  onMalformedDataEdge?: (raw: unknown) => void,
): ResolvedChainConfig {
  let chainId = DEFAULT_CHAIN_ID;
  const rawChain = billing?.chain_id;
  if (typeof rawChain === 'number' && Number.isFinite(rawChain)) {
    chainId = rawChain;
  }

  let dataEdgeAddress: string | undefined;
  const rawEdge = billing?.data_edge_address;
  if (typeof rawEdge === 'string' && DATA_EDGE_RE.test(rawEdge)) {
    dataEdgeAddress = rawEdge;
  } else if (rawEdge !== undefined && rawEdge !== null) {
    onMalformedDataEdge?.(rawEdge);
  }

  return { chainId, dataEdgeAddress };
}

export interface WriteConfigInput {
  relayUrl: string;
  /**
   * Exactly one of `mnemonic` / `ownerPrivateKeyHex` must be set (Option E
   * Phase 2 / #581, P2-13). Mnemonic-mode clients set `mnemonic`;
   * bundle-mode clients (no mnemonic anywhere in the process — derived-
   * bundle-v1.md §4.6 point 1) set `ownerPrivateKeyHex` from
   * `bundle.signing.private_key` instead. `store.ts`'s
   * `resolveOwnerAccount` prefers `ownerPrivateKeyHex` when both happen to
   * be present (never expected in practice, but privateKey is the more
   * direct source of truth so it wins rather than erroring).
   */
  mnemonic?: string;
  /** Bundle-mode signing key — 64 lowercase hex chars, no `0x` prefix. */
  ownerPrivateKeyHex?: string;
  authKeyHex: string;
  walletAddress: string;
  chainId: number;
  dataEdgeAddress?: string;
  /**
   * #689: false while no billing lookup has succeeded. A write built from an
   * unresolved state would fall through to the PRODUCTION default DataEdge,
   * so `buildSubgraphOverrides` refuses (throws `DataEdgeUnresolvedError`)
   * unless `TOTALRECLAW_DATA_EDGE_ADDRESS` is set. Omitted = resolved (legacy
   * callers and tests).
   */
  chainResolved?: boolean;
}

/**
 * Build the `getSubgraphConfig` override object for a managed-service write,
 * threading the billing-resolved chainId + DataEdge alongside the relay creds.
 *
 * Precedence for DataEdge: an explicit `TOTALRECLAW_DATA_EDGE_ADDRESS` env var
 * must win. Because a passed override key supersedes env inside
 * getSubgraphConfig's merge, we inject the billing-derived DataEdge ONLY when
 * the env override is unset — mirroring the plugin's env || billing || default
 * ordering.
 */
export function buildSubgraphOverrides(
  input: WriteConfigInput,
): Partial<SubgraphStoreConfig> {
  // #689 defence in depth: callers must run ensureWriteChainResolved first.
  if (input.chainResolved === false && !process.env.TOTALRECLAW_DATA_EDGE_ADDRESS) {
    throw new DataEdgeUnresolvedError(input.relayUrl);
  }
  const overrides: Partial<SubgraphStoreConfig> = {
    relayUrl: input.relayUrl,
    authKeyHex: input.authKeyHex,
    walletAddress: input.walletAddress,
  };

  // Exactly one signing source is threaded through — never both, and never
  // neither (store.ts's resolveOwnerAccount throws an actionable error if
  // neither ends up present on the final merged config).
  if (input.ownerPrivateKeyHex) {
    overrides.ownerPrivateKeyHex = input.ownerPrivateKeyHex;
  } else if (input.mnemonic) {
    overrides.mnemonic = input.mnemonic;
  }

  // Only thread a finite chainId — never let an undefined clobber the store's
  // Gnosis (100) default via the spread merge.
  if (typeof input.chainId === 'number' && Number.isFinite(input.chainId)) {
    overrides.chainId = input.chainId;
  }

  if (input.dataEdgeAddress && !process.env.TOTALRECLAW_DATA_EDGE_ADDRESS) {
    overrides.dataEdgeAddress = input.dataEdgeAddress;
  }

  return overrides;
}

// ---------------------------------------------------------------------------
// Billing fetch — the SINGLE code path both mnemonic-mode and bundle-mode
// state construction call to resolve chain + DataEdge (Option E Phase 2 /
// #581, P2-13, "Lesson 1" from the Hermes round: a bundle-configured client
// MUST NOT skip this call and assume chain resolution is complete just
// because the bundle carries `account.chain_id`. The bundle has no
// `data_edge_address` field at all — that value ONLY ever comes from the
// relay's `/v1/billing/status` response. Factoring the fetch into one
// function that every init path calls means there is no code path that can
// "forget" to call it — the mnemonic path and the bundle path share this
// exact function, not a copy of it.
//
// `fallbackChainId` is what to return if the billing call fails or is
// unreachable: mnemonic-mode passes 100 (Gnosis, the __init__-seeded
// default); bundle-mode passes `bundle.account.chain_id` (the bundle's own
// authoritative value — falling back to the free-tier default would target
// the WRONG CHAIN outright, not just the wrong environment's DataEdge; see
// `python/src/totalreclaw/client.py::_chain_id_fallback`'s docstring for the
// identical rationale on the Python side).
// ---------------------------------------------------------------------------

/**
 * `fetchChainConfig`'s result, extended with whether the billing call
 * itself actually succeeded (200 + parseable JSON). Adversarial-review
 * fixup (PR #618): a caller that only inspects `dataEdgeAddress` cannot
 * tell "billing responded but omitted the field" (rare, arguably legitimate)
 * apart from "billing was completely unreachable" (network error, timeout,
 * non-200) — and those two cases need different responses in bundle mode.
 * See `billingResolved`'s own doc comment.
 */
export interface FetchedChainConfig extends ResolvedChainConfig {
  /**
   * True iff the billing endpoint responded 200 with parseable JSON —
   * `resolveChainConfig` actually ran against real billing data (even if
   * `chain_id`/`data_edge_address` were absent or malformed within it).
   * False on network failure, timeout, or a non-200 response: the caller
   * fell all the way back to `fallbackChainId` with NO environment
   * information at all. Bundle-mode callers (`index.ts`'s
   * `initSubgraphStateFromBundle`) MUST check this before proceeding — see
   * that function's fail-closed guard.
   */
  billingResolved: boolean;
}

export interface FetchChainConfigOptions {
  serverUrl: string;
  smartAccountAddress: string;
  authKeyHex: string;
  /** Extra headers (client id, session tag, …) merged with Authorization. */
  headers?: Record<string, string>;
  /** Chain id to return when the billing call fails/is unreachable. */
  fallbackChainId: number;
  timeoutMs?: number;
  /** Injectable for tests; defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  /** Best-effort logger for the tier/chain line other init paths already print. */
  onResolved?: (billing: Record<string, unknown>, resolved: ResolvedChainConfig) => void;
  /**
   * Fires whenever the billing call did NOT succeed — network error,
   * timeout, or a non-200 response — right before falling back to
   * `fallbackChainId` / no DataEdge. `reason` is a short, human-readable
   * description (never contains key material — it only ever describes
   * transport-level failure, ``err.message``/HTTP status).
   */
  onBillingUnavailable?: (reason: string) => void;
}

const DEFAULT_BILLING_TIMEOUT_MS = 5000;

export async function fetchChainConfig(
  opts: FetchChainConfigOptions,
): Promise<FetchedChainConfig> {
  const doFetch = opts.fetchImpl ?? fetch;
  try {
    const billingUrl = `${opts.serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(opts.smartAccountAddress)}`;
    const resp = await doFetch(billingUrl, {
      method: 'GET',
      headers: {
        ...(opts.headers ?? {}),
        Authorization: `Bearer ${opts.authKeyHex}`,
      },
      signal: AbortSignal.timeout(opts.timeoutMs ?? DEFAULT_BILLING_TIMEOUT_MS),
    });
    if (resp.ok) {
      const billing = (await resp.json()) as Record<string, unknown>;
      const resolved = resolveChainConfig(billing, (raw) => {
        console.error(
          `TotalReclaw: billing returned malformed data_edge_address (${JSON.stringify(raw)}) — falling back to env/default.`,
        );
      });
      opts.onResolved?.(billing, resolved);
      return { ...resolved, billingResolved: true };
    }
    opts.onBillingUnavailable?.(`billing responded HTTP ${resp.status}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    opts.onBillingUnavailable?.(`billing fetch failed: ${msg}`);
  }
  return { chainId: opts.fallbackChainId, dataEdgeAddress: undefined, billingResolved: false };
}

// ---------------------------------------------------------------------------
// Fail-closed guard (Option E Phase 2 / #581, P2-13 — #618 adversarial
// review, item 1). The bundle has no `data_edge_address` field of its own;
// `fetchChainConfig` is the ONLY source. If that call did not succeed
// (network error, timeout, non-200 — `billingResolved: false`) and no
// explicit `TOTALRECLAW_DATA_EDGE_ADDRESS` override exists, a bundle-mode
// host has learned NOTHING about which environment it's writing to.
// Proceeding would silently fall through to `getSubgraphConfig`'s
// `DEFAULT_DATA_EDGE_ADDRESS` — the PRODUCTION contract — which is exactly
// the "skip/fail the billing call → silently write to prod" bug class this
// whole phase exists to close. This is the sibling of the already-fixed
// "skipped the call entirely" variant: the call was MADE, it just failed.
//
// Extracted as a standalone function (rather than inlined only in
// `index.ts::initSubgraphStateFromBundle`) because `index.ts` cannot be
// unit-tested directly — importing it executes `main()` — so this exact
// fail-closed decision needs its own dedicated test coverage. Mnemonic
// mode does NOT call this: it keeps the pre-existing #439 fallback
// behaviour (log loudly, proceed) — see `index.ts::initSubgraphState`.
// ---------------------------------------------------------------------------

export interface AssertDataEdgeResolvableOptions {
  /** From `fetchChainConfig`'s result. */
  billingResolved: boolean;
  /** `process.env.TOTALRECLAW_DATA_EDGE_ADDRESS` (or the test override). */
  envDataEdgeOverride: string | undefined;
  /** Relay base URL, for the error message only. */
  serverUrl: string;
}

/**
 * Throws an actionable error when bundle-mode state construction has no
 * confirmed DataEdge for this environment (billing failed, no env
 * override). No-op (returns) when either `billingResolved` is true (the
 * billing call succeeded — even if it happened not to carry a
 * `data_edge_address`, that's `resolveChainConfig`'s concern, not this
 * guard's) or an explicit env override is present.
 */
export function assertDataEdgeResolvable(opts: AssertDataEdgeResolvableOptions): void {
  if (opts.billingResolved || opts.envDataEdgeOverride) return;
  throw new Error(
    'derived-bundle-v1: cannot resolve environment DataEdge — refusing to guess the production ' +
      `default (${DEFAULT_DATA_EDGE_ADDRESS}). The billing lookup to ` +
      `${opts.serverUrl.replace(/\/+$/, '')}/v1/billing/status failed and no ` +
      'TOTALRECLAW_DATA_EDGE_ADDRESS override is set. Retry once the relay is reachable, or set ' +
      'TOTALRECLAW_DATA_EDGE_ADDRESS explicitly for this environment.',
  );
}

// ---------------------------------------------------------------------------
// Retry + lazy resolution (#689, internal p-diogo/totalreclaw-internal#689).
//
// Before #689, mnemonic mode made ONE 5 s billing call at startup and, on a
// timeout, fell back to `DEFAULT_DATA_EDGE_ADDRESS` (the PRODUCTION
// contract) for the whole process. A staging-configured server on a cold
// relay therefore wrote to production and then recalled nothing from the
// staging subgraph. Now:
//
//   1. Startup retries the billing call with backoff (a cold relay can take
//      longer than one short timeout).
//   2. If it still fails, the state stays UNRESOLVED. No write is sent.
//   3. The first write re-runs the lookup (with retries). Success fills in
//      chain + DataEdge for the rest of the process; failure fails THAT
//      write with `DataEdgeUnresolvedError` — never a guess.
//
// An explicit `TOTALRECLAW_DATA_EDGE_ADDRESS` still wins and counts as
// resolved. A successful lookup behaves exactly as before.
// ---------------------------------------------------------------------------

export interface BillingRetryOptions {
  /** Total attempts, including the first. */
  attempts: number;
  /** Per-attempt timeout. */
  timeoutMs: number;
  /** Delay before attempt i+1 (index i). The last value repeats if short. */
  backoffMs: number[];
  /** Injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

/** Startup: bounded so a dead relay doesn't stall the MCP host for long (~2 x 8 s + 1 s). */
export const STARTUP_BILLING_RETRY: BillingRetryOptions = { attempts: 2, timeoutMs: 8000, backoffMs: [1000] };
/** First write while unresolved: a little more patience, the user is waiting on a write anyway. */
export const WRITE_BILLING_RETRY: BillingRetryOptions = { attempts: 3, timeoutMs: 10000, backoffMs: [1000, 3000] };

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * `fetchChainConfig` with retries. Returns the first successful result, or
 * the last failure (`billingResolved: false`) after `attempts` tries.
 * `onBillingUnavailable` fires once per failed attempt with an
 * `(attempt i/n)` suffix.
 */
export async function fetchChainConfigWithRetry(
  opts: FetchChainConfigOptions,
  retry: BillingRetryOptions,
): Promise<FetchedChainConfig> {
  const sleep = retry.sleep ?? defaultSleep;
  const attempts = Math.max(1, retry.attempts);
  let last: FetchedChainConfig = { chainId: opts.fallbackChainId, dataEdgeAddress: undefined, billingResolved: false };
  for (let i = 0; i < attempts; i++) {
    if (i > 0) {
      const delay = retry.backoffMs[Math.min(i - 1, retry.backoffMs.length - 1)] ?? 0;
      if (delay > 0) await sleep(delay);
    }
    last = await fetchChainConfig({
      ...opts,
      timeoutMs: retry.timeoutMs,
      onBillingUnavailable: (reason) => opts.onBillingUnavailable?.(`${reason} (attempt ${i + 1}/${attempts})`),
    });
    if (last.billingResolved) return last;
  }
  return last;
}

/** The chain fields of a managed-service state that writes depend on. */
export interface WriteChainState {
  chainId: number;
  dataEdgeAddress?: string;
  /**
   * True once a billing lookup succeeded (or an env DataEdge override is
   * set). While false, no write may be sent: `ensureWriteChainResolved`
   * must succeed first.
   */
  chainResolved: boolean;
}

/** Thrown when a write is attempted with no confirmed DataEdge for this relay. */
export class DataEdgeUnresolvedError extends Error {
  constructor(serverUrl: string) {
    super(
      'TotalReclaw could not confirm which DataEdge contract belongs to the relay at ' +
        `${serverUrl.replace(/\/+$/, '')} (the billing lookup failed after retries), so this write ` +
        'was NOT sent. Nothing was stored. Retry in a minute once the relay is reachable, or set ' +
        'TOTALRECLAW_DATA_EDGE_ADDRESS explicitly for this environment.',
    );
    this.name = 'DataEdgeUnresolvedError';
  }
}

const inflight = new WeakMap<WriteChainState, Promise<void>>();

/**
 * Make sure `state` has a confirmed chain + DataEdge before a write. No-op
 * when already resolved or when `envDataEdgeOverride` is set. Otherwise
 * runs `refetch` (one in-flight lookup per state, shared by concurrent
 * writes); on success updates `state` in place, on failure throws
 * `DataEdgeUnresolvedError`. Never falls back to the production default.
 */
export async function ensureWriteChainResolved(
  state: WriteChainState,
  refetch: () => Promise<FetchedChainConfig>,
  envDataEdgeOverride: string | undefined,
  serverUrl: string,
): Promise<void> {
  if (state.chainResolved || envDataEdgeOverride) return;
  let p = inflight.get(state);
  if (!p) {
    p = (async () => {
      const r = await refetch();
      if (!r.billingResolved) throw new DataEdgeUnresolvedError(serverUrl);
      state.chainId = r.chainId;
      state.dataEdgeAddress = r.dataEdgeAddress;
      state.chainResolved = true;
    })();
    inflight.set(state, p);
    p.finally(() => inflight.delete(state)).catch(() => {});
  }
  await p;
}
