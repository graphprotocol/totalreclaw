/**
 * Single tool-dispatch table for the TotalReclaw MCP server.
 *
 * The server serves two storage-mode pipelines — self-hosted (`http`) and
 * managed service (`subgraph`) — that used to live in two parallel `switch`
 * statements plus per-tool glue duplicated across both. This module collapses
 * that into one data-driven router:
 *
 *   - `TOOL_MANIFEST` is the mode-independent tool list returned by ListTools.
 *   - `SUBGRAPH_POLICY` / `HTTP_POLICY` capture the per-(tool, mode)
 *     cross-cutting behaviour (quota-error trapping + memory-context cache
 *     invalidation) as data instead of copy-pasted `try`/`catch` arms.
 *   - `createCallToolHandler` is the router. The concrete handlers are injected
 *     as pre-bound "bundles" (name → handler) so this module stays free of the
 *     crypto/subgraph weight and is unit-testable with fakes.
 *
 * Routing order is preserved verbatim from the original entry point:
 *   1. `totalreclaw_setup` → removed-tool envelope (phrase-safety, 3.2.1).
 *   2. `totalreclaw_support` → available in every mode (incl. unconfigured).
 *   3. `totalreclaw_pair`    → available in every mode (incl. unconfigured).
 *   4. unconfigured mode     → not-configured envelope for everything else.
 *   5. resolve the mode bundle, look up the handler, apply the policy, and
 *      wrap the whole thing in the shared quota → auth → generic error funnel.
 */

import type { Tool } from '@modelcontextprotocol/sdk/types.js';
import type { ToolResponse } from './tools/types.js';
import {
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
  importFromToolDefinition,
  importBatchToolDefinition,
  consolidateToolDefinition,
  statusToolDefinition,
  upgradeToolDefinition,
  debriefToolDefinition,
  supportToolDefinition,
  accountToolDefinition,
  pairToolDefinition,
} from './tools/index.js';
import { pinToolDefinition, unpinToolDefinition } from './tools/pin.js';
import { retypeToolDefinition } from './tools/retype.js';
import { setScopeToolDefinition } from './tools/set-scope.js';

export type ServerMode = 'http' | 'subgraph' | 'unconfigured';

/**
 * The tool list advertised by ListTools. Identical in both storage modes —
 * mode gating happens at dispatch time (an unsupported tool returns a
 * mode-specific error envelope), not in the manifest.
 */
export const TOOL_MANIFEST = [
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
  importFromToolDefinition,
  importBatchToolDefinition,
  consolidateToolDefinition,
  statusToolDefinition,
  upgradeToolDefinition,
  debriefToolDefinition,
  supportToolDefinition,
  accountToolDefinition,
  pinToolDefinition,
  unpinToolDefinition,
  retypeToolDefinition,
  setScopeToolDefinition,
  pairToolDefinition,
  // The tool definitions declare `inputSchema.type` as `string` rather than the
  // literal `"object"` the SDK's `Tool` type wants; the widening is harmless at
  // runtime (they are all `"object"`). The cast keeps ListTools strongly typed.
] as Tool[];

/** A tool handler bound to its mode-appropriate context (state or client). */
export type ToolHandler = (args: unknown) => Promise<ToolResponse>;

/** Map of tool name → bound handler for a single storage mode. */
export type HandlerBundle = Partial<Record<string, ToolHandler>>;

/**
 * Cross-cutting behaviour applied around a handler after routing.
 *  - `quotaGuard`: trap QUOTA_EXCEEDED and return the standard quota envelope
 *     instead of letting it bubble (both modes ultimately produce the same
 *     envelope via the top-level catch, but the guard keeps the original
 *     per-tool short-circuit).
 *  - `invalidateCache`: on success, invalidate the memory-context resource
 *     cache and notify subscribers.
 */
export interface ToolPolicy {
  quotaGuard?: boolean;
  invalidateCache?: boolean;
}

/** Per-tool policy in managed-service (subgraph) mode. */
export const SUBGRAPH_POLICY: Record<string, ToolPolicy> = {
  totalreclaw_remember: { quotaGuard: true, invalidateCache: true },
  totalreclaw_forget: { invalidateCache: true },
  totalreclaw_debrief: { quotaGuard: true, invalidateCache: true },
  totalreclaw_pin: { quotaGuard: true, invalidateCache: true },
  totalreclaw_unpin: { quotaGuard: true, invalidateCache: true },
  totalreclaw_retype: { quotaGuard: true, invalidateCache: true },
  totalreclaw_set_scope: { quotaGuard: true, invalidateCache: true },
};

/** Per-tool policy in self-hosted (HTTP) mode. */
export const HTTP_POLICY: Record<string, ToolPolicy> = {
  totalreclaw_remember: { quotaGuard: true },
  totalreclaw_forget: { invalidateCache: true },
  totalreclaw_consolidate: { invalidateCache: true },
  totalreclaw_debrief: { quotaGuard: true },
};

/** Injected dependencies the router needs to route and to fail gracefully. */
export interface DispatchDeps {
  /** Current storage mode (`http` | `subgraph` | `unconfigured`). */
  getMode: () => ServerMode;
  /** Support tool handler — reachable in every mode. */
  handleSupport: ToolHandler;
  /** Pair tool handler — reachable in every mode. */
  handlePair: ToolHandler;
  /**
   * Mode-independent handlers (status / upgrade / account) that talk to the
   * relay directly. Checked before `resolveBundle` so they never depend on the
   * lazily-built self-hosted client.
   */
  common: HandlerBundle;
  /**
   * Build the handler bundle for a configured mode. Called per request; the
   * HTTP bundle may lazily construct the self-hosted client and therefore may
   * throw — the router lets that surface through the shared error funnel.
   */
  resolveBundle: (mode: 'http' | 'subgraph') => Promise<HandlerBundle>;
  isQuotaExceededError: (error: unknown) => boolean;
  quotaExceededResponse: () => ToolResponse;
  isAuthError: (error: unknown) => boolean;
  authHintResponse: () => ToolResponse;
  /** Invalidate the memory-context resource cache + notify subscribers. */
  onMutate: () => void;
}

/** Response for the removed `totalreclaw_setup` tool (phrase-safety, 3.2.1). */
export function setupRemovedResponse(): ToolResponse {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        error: 'tool_removed',
        message:
          'The totalreclaw_setup tool was removed in @totalreclaw/mcp-server@3.2.1 ' +
          'for phrase-safety. Follow the URL-driven install flow at ' +
          'https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md. ' +
          'The user sources their recovery phrase out-of-band (OpenClaw or Hermes ' +
          'browser pair flow, or an offline BIP-39 generator) and pastes it directly ' +
          'into TOTALRECLAW_RECOVERY_PHRASE in the MCP host config — never into chat.',
      }),
    }],
    isError: true,
  };
}

/** Response when a tool is called before the server is configured. */
export function notConfiguredResponse(): ToolResponse {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        error: 'not_configured',
        message:
          'TotalReclaw is not configured yet. Follow the URL-driven install flow ' +
          'at https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md — ' +
          'the user pastes their recovery phrase directly into the MCP host config (TOTALRECLAW_RECOVERY_PHRASE), ' +
          'never into chat.',
      }),
    }],
    isError: true,
  };
}

/** Response for a tool name with no handler in the active mode. */
export function unknownToolResponse(name: string): ToolResponse {
  return {
    content: [{ type: 'text', text: JSON.stringify({ error: `Unknown tool: ${name}` }) }],
    isError: true,
  };
}

/** Response for an uncaught handler error that is neither quota nor auth. */
export function genericErrorResponse(error: unknown): ToolResponse {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({ error: error instanceof Error ? error.message : 'Unknown error' }),
    }],
    isError: true,
  };
}

/**
 * Build the CallTool router. Returns `(name, args) => Promise<ToolResponse>`
 * — the single entry point the server's CallTool handler delegates to.
 */
export function createCallToolHandler(
  deps: DispatchDeps,
): (name: string, args: unknown) => Promise<ToolResponse> {
  return async function dispatchTool(name: string, args: unknown): Promise<ToolResponse> {
    // 1. Removed setup tool — structured error, never surfaces a phrase.
    if (name === 'totalreclaw_setup') return setupRemovedResponse();

    // 2 + 3. Support and pair are reachable in every mode, including
    //         unconfigured (pair is the MCP-only onboarding entry point).
    if (name === 'totalreclaw_support') return deps.handleSupport(args);
    if (name === 'totalreclaw_pair') return deps.handlePair(args);

    // 4. Unconfigured: everything else returns setup guidance.
    const mode = deps.getMode();
    if (mode === 'unconfigured') return notConfiguredResponse();

    // 5. Configured dispatch, wrapped in the shared quota → auth → generic funnel.
    try {
      // Mode-independent tools (status / upgrade / account) route first — they
      // hit the relay directly and must not depend on the self-hosted client.
      const commonHandler = deps.common[name];
      if (commonHandler) return await commonHandler(args);

      const bundle = await deps.resolveBundle(mode);
      const handler = bundle[name];
      if (!handler) return unknownToolResponse(name);

      const policy = (mode === 'subgraph' ? SUBGRAPH_POLICY : HTTP_POLICY)[name] ?? {};

      if (policy.quotaGuard) {
        try {
          const result = await handler(args);
          if (policy.invalidateCache) deps.onMutate();
          return result;
        } catch (error) {
          if (deps.isQuotaExceededError(error)) return deps.quotaExceededResponse();
          throw error;
        }
      }

      const result = await handler(args);
      if (policy.invalidateCache) deps.onMutate();
      return result;
    } catch (error) {
      if (deps.isQuotaExceededError(error)) return deps.quotaExceededResponse();
      if (deps.isAuthError(error)) return deps.authHintResponse();
      return genericErrorResponse(error);
    }
  };
}
