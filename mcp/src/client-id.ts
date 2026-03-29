/**
 * Shared client identifier for X-TotalReclaw-Client header.
 * Set once after MCP initialize handshake from server.getClientVersion().
 */
let _clientId = 'mcp-server';

export function setClientId(id: string): void {
  _clientId = id;
}

export function getClientId(): string {
  return _clientId;
}
