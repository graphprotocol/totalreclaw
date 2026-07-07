/**
 * MCP server construction + request-handler wiring for TotalReclaw.
 *
 * `index.ts` owns the storage-mode state (subgraph keys / self-hosted client)
 * and the concrete tool handlers; this module owns the plumbing that connects
 * an `@modelcontextprotocol/sdk` `Server` to that logic:
 *   - ListTools     → the single `TOOL_MANIFEST`.
 *   - CallTool      → the injected dispatch router (see `dispatch.ts`).
 *   - Resources     → the memory-context resource (self-hosted only).
 *   - Prompts       → the instruction + auto-memory prompt fallbacks.
 *   - Cache wiring  → invalidate the memory-context cache on remember/mutate.
 *
 * Keeping this out of `index.ts` lets the entry point read as composition:
 * resolve config → build handlers → wire the server → connect the transport.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  GetPromptRequestSchema,
  ListPromptsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
  type CallToolResult,
} from '@modelcontextprotocol/sdk/types.js';

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

/** Dependencies the server wiring needs from the entry point. */
export interface ServerSetupDeps {
  /** CallTool router built by `createCallToolHandler` in `index.ts`. */
  callTool: (name: string, args: unknown) => Promise<ToolResponse>;
  /** True when running against the managed service (subgraph) — no resource reads. */
  isManagedMode: () => boolean;
  /** Lazily build/return the self-hosted client (resource reads only). */
  getClient: () => Promise<TotalReclaw>;
}

/**
 * Construct the MCP `Server` and register every request handler + the
 * remember-triggered cache invalidation. Returns the server so the caller can
 * `connect()` a transport and read `getClientVersion()` for client-id
 * resolution.
 */
export function createTotalReclawServer(deps: ServerSetupDeps): Server {
  const server = new Server(
    { name: 'totalreclaw', version: '1.0.0' },
    {
      capabilities: {
        tools: {},
        prompts: {},
        resources: { subscribe: true, listChanged: true },
      },
      instructions: SERVER_INSTRUCTIONS,
    },
  );

  // When facts are stored, invalidate the memory-context resource cache and
  // notify subscribed clients that the resource has changed.
  setOnRememberCallback(() => {
    invalidateMemoryContextCache();
    server
      .sendResourceUpdated({ uri: memoryContextResource.uri })
      .catch((err) => console.error('Failed to send resource update:', err));
  });

  // ── Tools ──────────────────────────────────────────────────────────────
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOL_MANIFEST }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    // Handlers type `content[].type` as `string`; at runtime it is always a
    // valid content-block type (`"text"`). The cast reconciles that widening
    // with the SDK's stricter `CallToolResult` without changing behaviour.
    return (await deps.callTool(name, args)) as CallToolResult;
  });

  // ── Resources ──────────────────────────────────────────────────────────
  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [memoryContextResource],
  }));

  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
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
  server.setRequestHandler(ListPromptsRequestSchema, async () => ({
    prompts: [
      // Legacy instructions prompt (backward compat)
      { name: 'totalreclaw_instructions', description: 'Instructions for using TotalReclaw tools' },
      // Auto-memory prompt fallbacks
      ...PROMPT_DEFINITIONS,
    ],
  }));

  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const messages = getPromptMessages(name, args as Record<string, string> | undefined);
    return { messages };
  });

  return server;
}
