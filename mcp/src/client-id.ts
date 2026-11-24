/**
 * Shared client identifier for X-TotalReclaw-Client header.
 * Set once from the client identity (client-identity.ts): the initialize handshake (2025 era) or the per-request _meta (2026-07-28 era).
 */
let _clientId = 'mcp-server';

export function setClientId(id: string): void {
  _clientId = id;
}

export function getClientId(): string {
  return _clientId;
}
