/**
 * MCP server construction + request-handler wiring for TotalReclaw.
 *
 * `index.ts` owns the storage-mode state (subgraph keys / self-hosted client)
 * and the concrete tool handlers; this module owns the plumbing that connects
 * an MCP TypeScript SDK v2 (`@modelcontextprotocol/server`) `Server` to that
 * logic:
 *   - tools/list     → the single `TOOL_MANIFEST`.
 *   - tools/call     → the injected dispatch router (see `dispatch.ts`).
 *   - resources/*    → the memory-context resource (self-hosted only).
 *   - prompts/*      → the instruction + auto-memory prompt fallbacks.
 *   - Cache wiring   → invalidate the memory-context cache on remember/mutate.
 *
 * Dual-era stdio (DEP-15): `serveTotalReclawStdio` hands a server FACTORY to
 * the SDK's `serveStdio`, which reads the client's opening message and pins
 * the connection to one protocol era:
 *   - `initialize` (no modern `_meta` claim) → 2025-era instance, served
 *     exactly as the pre-v2 server was (Claude Desktop, Cursor, Claude Code
 *     with MCP_PROTOCOL_NEGOTIATION unset/legacy).
 *   - `server/discover` or any request carrying
 *     `_meta["io.modelcontextprotocol/protocolVersion"]` → 2026-07-28
 *     instance (stateless, no handshake).
 * The same factory builds both, so tools, schemas, order and instructions are
 * identical in both eras. See docs/specs/totalreclaw/mcp-dual-era.md.
 */

import {
  Server,
  type CallToolResult,
  type GetPromptResult,
  type ListResourcesResult,
  type Transport,
} from '@modelcontextprotocol/server';
import { serveStdio, type StdioServerHandle } from '@modelcontextprotocol/server/stdio';

import type { TotalReclaw } from '@totalreclaw/client';
import type { ToolResponse } from './tools/types.js';
import { TOOL_MANIFEST } from './dispatch.js';
import { SERVER_INSTRUCTIONS, PROMPT_DEFINITIONS, getPromptMessages } from './prompts.js';
import {
  memoryContextResource,
  readMemoryContext,
  invalidateMemoryContextCache,
} from './resources/index.js';
import { setOnRememberCallback } from './tools/remember.js';

/** Server identity advertised in `initialize` (2025) and `_meta` serverInfo (2026-07-28). Unchanged from the v1-SDK server. */
export const SERVER_INFO = { name: 'totalreclaw', version: '1.0.0' } as const;

/** Capabilities advertised in both eras. Unchanged from the v1-SDK server. */
export const SERVER_CAPABILITIES = {
  tools: {},
  prompts: {},
  resources: { subscribe: true, listChanged: true },
};

/** Dependencies the server wiring needs from the entry point. */
export interface ServerSetupDeps {
  /** CallTool router built by `createCallToolHandler` in `index.ts`. */
  callTool: (name: string, args: unknown) => Promise<ToolResponse>;
  /** True when running against the managed service (subgraph) — no resource reads. */
  isManagedMode: () => boolean;
  /** Lazily build/return the self-hosted client (resource reads only). */
  getClient: () => Promise<TotalReclaw>;
}

/** Options for {@link serveTotalReclawStdio}. */
export interface ServeTotalReclawStdioOptions {
  /**
   * Test seam: serve over this transport instead of the process's stdin/stdout
   * (e.g. a `StdioServerTransport` over PassThrough streams, or an
   * `InMemoryTransport`). Production passes nothing.
   */
  transport?: Transport;
  /**
   * Called with every `Server` instance the stdio entry constructs. A client
   * that probes with `server/discover` and then falls back to `initialize`
   * causes two constructions (the probe instance is discarded); the LAST
   * instance passed here is always the live one.
   */
  onServerCreated?: (server: Server) => void;
}

/**
 * Construct one MCP `Server` and register every request handler + the
 * remember-triggered cache invalidation. Era-blind: the stdio entry decides
 * the era and the SDK's per-era wire codec encodes the results.
 */
export function createTotalReclawServer(deps: ServerSetupDeps): Server {
  const server = new Server(
    { ...SERVER_INFO },
    {
      capabilities: SERVER_CAPABILITIES,
      instructions: SERVER_INSTRUCTIONS,
    },
  );

  // When facts are stored, invalidate the memory-context resource cache and
  // notify the client that the resource has changed. On a 2026-07-28
  // connection the SDK delivers this only to an open `subscriptions/listen`
  // subscription that asked for resource updates (and drops it otherwise).
  setOnRememberCallback(() => {
    invalidateMemoryContextCache();
    server
      .sendResourceUpdated({ uri: memoryContextResource.uri })
      .catch((err) => console.error('Failed to send resource update:', err));
  });

  // ── Tools ──────────────────────────────────────────────────────────────
  server.setRequestHandler('tools/list', async () => ({ tools: TOOL_MANIFEST }));

  server.setRequestHandler('tools/call', async (request) => {
    const { name, arguments: args } = request.params;
    // Handlers type `content[].type` as `string`; at runtime it is always a
    // valid content-block type (`"text"`). The cast reconciles that widening
    // with the SDK's stricter `CallToolResult` without changing behaviour.
    const result = (await deps.callTool(name, args)) as CallToolResult;
    // SDK v2 contract for low-level tools/call handlers: route the result
    // through the negotiated era's codec. Identity for our text-only results.
    return server.projectCallToolResult(result, undefined);
  });

  // ── Resources ──────────────────────────────────────────────────────────
  server.setRequestHandler(
    'resources/list',
    async () => ({ resources: [memoryContextResource] }) as ListResourcesResult,
  );

  server.setRequestHandler('resources/read', async (request) => {
    const { uri } = request.params;

    if (uri === memoryContextResource.uri) {
      // The managed service does not support resource reads yet.
      if (deps.isManagedMode()) {
        return {
          contents: [
            {
              uri: memoryContextResource.uri,
              mimeType: 'text/markdown',
              text: '*Memory context resource is not available with the managed service. Use totalreclaw_recall to search memories.*',
            },
          ],
        };
      }

      const client = await deps.getClient();
      const content = await readMemoryContext(client);
      return {
        contents: [
          { uri: memoryContextResource.uri, mimeType: 'text/markdown', text: content },
        ],
      };
    }

    throw new Error(`Unknown resource: ${uri}`);
  });

  // ── Prompts ────────────────────────────────────────────────────────────
  server.setRequestHandler('prompts/list', async () => ({
    prompts: [
      // Legacy instructions prompt (backward compat)
      { name: 'totalreclaw_instructions', description: 'Instructions for using TotalReclaw tools' },
      // Auto-memory prompt fallbacks
      ...PROMPT_DEFINITIONS,
    ],
  }));

  server.setRequestHandler('prompts/get', async (request) => {
    const { name, arguments: args } = request.params;
    const messages = getPromptMessages(name, args as Record<string, string> | undefined);
    // `getPromptMessages` types `role` as `string`; at runtime it is always
    // `"user"` or `"assistant"`. Same widening cast as tools/call above.
    return { messages } as GetPromptResult;
  });

  return server;
}

/**
 * One-line, bounded description of an out-of-band stdio error for stderr.
 * Phrase-safety: a V8 `SyntaxError` from JSON.parse quotes part of the
 * offending input line, so it is replaced by a fixed sentence; any other
 * message is collapsed to one line and capped at 300 characters.
 */
export function describeStdioError(err: Error): string {
  if (err instanceof SyntaxError) return 'discarded a stdin line that is not valid JSON';
  return err.message.replace(/\s+/g, ' ').trim().slice(0, 300);
}

/**
 * Serve TotalReclaw over stdio in BOTH protocol eras (2025-11-25 `initialize`
 * and 2026-07-28 `server/discover`). Returns the SDK handle; production
 * ignores it (the process exits when the host closes stdin), tests call
 * `close()`.
 */
export function serveTotalReclawStdio(
  deps: ServerSetupDeps,
  options: ServeTotalReclawStdioOptions = {},
): StdioServerHandle {
  return serveStdio(
    () => {
      const server = createTotalReclawServer(deps);
      options.onServerCreated?.(server);
      return server;
    },
    {
      // 'serve' = answer 2025-era `initialize` openings (the SDK default,
      // stated explicitly because Claude Desktop and Cursor depend on it).
      legacy: 'serve',
      ...(options.transport ? { transport: options.transport } : {}),
      // stderr only (stdout is the protocol channel).
      onerror: (err) => console.error(`TotalReclaw MCP stdio: ${describeStdioError(err)}`),
    },
  );
}
