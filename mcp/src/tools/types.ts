/**
 * Shared tool-handler contract for the TotalReclaw MCP server.
 *
 * Every `totalreclaw_*` tool handler takes the same two arguments:
 * `(ctx: ToolContext, args: unknown)`. `ctx` is the dependency-injection
 * bundle (self-hosted client, relay URL, auth key, wallet, …) assembled once
 * by the dispatcher in `mcp/src/index.ts`; `args` is the raw tool input.
 *
 * Not every field is populated for every call — a handler reads only the
 * fields it needs (e.g. the self-hosted storage handlers use `ctx.client`,
 * the billing handlers use `ctx.serverUrl` + `ctx.authKeyHex`). This keeps a
 * single predictable signature across the whole tool surface instead of the
 * per-handler positional/options variance the server used to carry.
 */

import type { TotalReclaw } from '@totalreclaw/client';

export interface ToolContext {
  /** Self-hosted (HTTP mode) client. Undefined in managed-service mode. */
  client?: TotalReclaw;
  /** Relay base URL for billing/account/upgrade calls. */
  serverUrl?: string;
  /** Hex-encoded auth key for relay `Authorization: Bearer` headers. */
  authKeyHex?: string;
  /** Smart Account address (managed service) — used for billing + support. */
  walletAddress?: string | null;
  /** Recovery-phrase hint (`first … last`) for the account tool. */
  mnemonicHint?: string;
  /** Lazily fetch the owner's on-chain fact count (account tool). */
  getFactCount?: () => Promise<number>;
}

/** Standard MCP tool response envelope returned by every handler. */
export interface ToolResponse {
  content: Array<{ type: string; text: string }>;
  isError?: boolean;
}
