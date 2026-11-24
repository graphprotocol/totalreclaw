/**
 * Who is the MCP client? One answer for both protocol eras.
 *
 *   - 2025 era (`initialize`): the SDK stores `params.clientInfo` and returns it
 *     from `server.getClientVersion()`.
 *   - 2026-07-28 era (`server/discover`, no handshake): the client identifies
 *     itself on every request in `_meta["io.modelcontextprotocol/clientInfo"]`
 *     (a SHOULD in the spec). The SDK's stdio entry does NOT copy it into
 *     `getClientVersion()` (only its HTTP entry does), so on a modern stdio
 *     connection that accessor stays `undefined`. Claude Desktop's `.mcpb`
 *     extension session can open in this era, which dropped the one-time tip.
 *
 * `observeClientInfo` records what a request envelope carries; `getClientInfo`
 * prefers the legacy handshake value and falls back to the observed one. A
 * stdio process serves exactly one connection, so module state is safe.
 * Nothing here reads or logs anything but the client's name and version.
 */

import { CLIENT_INFO_META_KEY } from '@modelcontextprotocol/server';

export interface ClientInfo {
  name: string;
  version?: string;
}

let observed: ClientInfo | undefined;

/** Narrow an untrusted `clientInfo` value to `{name, version?}`; `undefined` if it has no usable name. */
function parseClientInfo(raw: unknown): ClientInfo | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const { name, version } = raw as { name?: unknown; version?: unknown };
  if (typeof name !== 'string' || name.length === 0) return undefined;
  return typeof version === 'string' ? { name, version } : { name };
}

/** Remember the client identity from a modern-era request envelope (`ctx.mcpReq.envelope`). No-op when absent. */
export function observeClientInfo(envelope: object | undefined): void {
  const info = parseClientInfo((envelope as Record<string, unknown> | undefined)?.[CLIENT_INFO_META_KEY]);
  if (info) observed = info;
}

/** The client's identity in either era, or `undefined` when it never said. */
export function getClientInfo(server: { getClientVersion(): { name?: string; version?: string } | undefined } | undefined): ClientInfo | undefined {
  return parseClientInfo(server?.getClientVersion()) ?? observed;
}

/** Test helper. */
export function resetClientIdentity(): void {
  observed = undefined;
}
