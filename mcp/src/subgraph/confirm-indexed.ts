/**
 * Read-after-write primitive for the MCP server — confirm a fact id has been
 * indexed by the subgraph after an on-chain mutation.
 *
 * Wraps the pure-compute halves exported by `@totalreclaw/core`
 * (`wasmConfirmIndexedQuery`, `wasmConfirmIndexedParse`) in a host-side
 * polling loop. The subgraph indexer typically lags 5-30s behind L1 inclusion
 * on Gnosis production; without this wait, mutation tools (set_scope,
 * retype, pin, unpin, forget) can return success before a follow-up
 * recall/export sees the new state.
 *
 * Mnemonic isolation: this helper never touches the mnemonic, encryption
 * key, or any decrypted blob.
 */

import { getClientId } from '../client-id.js';

/**
 * `wasmConfirmIndexed*` exports ship in `@totalreclaw/core@2.3.x`. Cast to
 * unblock the build until the published `.d.ts` is regenerated.
 */
type ConfirmIndexedCore = typeof import('@totalreclaw/core') & {
  wasmConfirmIndexedQuery(): string;
  wasmConfirmIndexedParse(responseJson: string): boolean;
  wasmConfirmIndexedDefaultPollMs(): number;
  wasmConfirmIndexedDefaultTimeoutMs(): number;
};

// Loaded lazily so the server doesn't bind to the WASM module unless an
// on-chain mutation is actually exercised.
let _wasm: ConfirmIndexedCore | null = null;
async function getWasm(): Promise<ConfirmIndexedCore> {
  if (!_wasm) {
    _wasm = (await import('@totalreclaw/core')) as unknown as ConfirmIndexedCore;
  }
  return _wasm!;
}

export interface ConfirmIndexedResult {
  indexed: boolean;
  attempts: number;
  elapsedMs: number;
  lastError?: string;
}

export interface ConfirmIndexedOptions {
  pollIntervalMs?: number;
  timeoutMs?: number;
  /** Direction: `"active"` (default, for pin/retype/set_scope) or `"inactive"` (for forget). */
  expect?: 'active' | 'inactive';
  /** Required: subgraph URL. Caller passes either `${relayUrl}/v1/subgraph` or `TOTALRECLAW_SUBGRAPH_URL`. */
  subgraphUrl: string;
  authKeyHex?: string;
  /** Test injection. */
  poster?: (
    url: string,
    body: string,
    headers: Record<string, string>,
  ) => Promise<{ ok: boolean; status: number; text: () => Promise<string> }>;
}

export async function confirmIndexed(
  factId: string,
  options: ConfirmIndexedOptions,
): Promise<ConfirmIndexedResult> {
  // WASM bindings may be unavailable (core@<2.3.0 not yet published).
  // Confirm step is observational; chain write has already succeeded.
  // Return `indexed: false` so callers surface partial=true rather than fail.
  let wasm: ConfirmIndexedCore;
  let query: string;
  let pollIntervalMs: number;
  let timeoutMs: number;
  try {
    wasm = await getWasm();
    pollIntervalMs =
      options.pollIntervalMs ?? Number(wasm.wasmConfirmIndexedDefaultPollMs?.() ?? 1000);
    timeoutMs =
      options.timeoutMs ?? Number(wasm.wasmConfirmIndexedDefaultTimeoutMs?.() ?? 30000);
    query = wasm.wasmConfirmIndexedQuery();
  } catch (err) {
    return {
      indexed: false,
      attempts: 0,
      elapsedMs: 0,
      lastError: `confirm-indexed wasm bindings unavailable: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-TotalReclaw-Client': getClientId(),
  };
  if (options.authKeyHex) headers['Authorization'] = `Bearer ${options.authKeyHex}`;

  const body = JSON.stringify({ query, variables: { id: factId } });

  const poster =
    options.poster ??
    (async (url, b, h) => {
      const r = await fetch(url, { method: 'POST', headers: h, body: b });
      return { ok: r.ok, status: r.status, text: () => r.text() };
    });

  const expect = options.expect ?? 'active';
  const start = Date.now();
  let attempts = 0;
  let lastError: string | undefined;

  while (Date.now() - start < timeoutMs) {
    attempts++;
    try {
      const r = await poster(options.subgraphUrl, body, headers);
      if (r.ok) {
        const txt = await r.text();
        try {
          const isActive = wasm.wasmConfirmIndexedParse(txt);
          const resolved = expect === 'active' ? isActive : !isActive;
          if (resolved) {
            return { indexed: true, attempts, elapsedMs: Date.now() - start };
          }
        } catch (parseErr) {
          lastError = parseErr instanceof Error ? parseErr.message : String(parseErr);
        }
      } else {
        lastError = `HTTP ${r.status}`;
      }
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    }
    const remaining = timeoutMs - (Date.now() - start);
    if (remaining <= 0) break;
    await new Promise((res) => setTimeout(res, Math.min(pollIntervalMs, remaining)));
  }

  return { indexed: false, attempts, elapsedMs: Date.now() - start, lastError };
}
