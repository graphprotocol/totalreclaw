# MCP dual-era protocol support (2025-11-25 `initialize` + 2026-07-28 `server/discover`)

| | |
|---|---|
| **Status** | Implemented in `@totalreclaw/mcp-server` (stdio) — DEP-15 / PRD-04 F15; RC pending |
| **Applies to** | `mcp/` (local stdio server). Section 6 is the contract the enclave's Streamable-HTTP `/mcp` endpoint mirrors. |
| **SDK** | `@modelcontextprotocol/server` ~2.1 (MCP TypeScript SDK v2). Replaced `@modelcontextprotocol/sdk` 1.x, whose newest line (1.30.x) speaks 2025-11-25 at most. |
| **Protocol revisions** | Modern: 2026-07-28. Legacy: 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05, 2024-10-07. |
| **Last reviewed** | 2026-09-27 |

## 1. Why

MCP 2026-07-28 made the protocol stateless: the `initialize` / `notifications/initialized` handshake and protocol sessions are gone, every request carries its protocol version and client capabilities in `_meta`, and servers must answer the new `server/discover` RPC. Hosts are mid-migration — some open with `initialize`, some probe with `server/discover` first. A server that speaks one era strands the hosts on the other, so TotalReclaw answers both ("dual-era" in the spec's terms).

## 2. Era selection on stdio

The SDK's `serveStdio` entry reads the client's **first** message and pins the process to one era. `mcp/src/server-setup.ts` (`serveTotalReclawStdio`) hands it a factory that builds the same `Server` for either era.

| Opening message | Era | Result |
|---|---|---|
| `initialize` with no modern `_meta` claim | legacy | Answered with the requested revision if it is a legacy one, otherwise `2025-11-25`. |
| `server/discover` with a valid 2026-07-28 envelope | modern (probe) | `DiscoverResult`. If the next request is `initialize` instead, the probe instance is discarded and the process pins legacy. |
| Any other request with a valid 2026-07-28 envelope | modern | Served statelessly; `server/discover` is optional. |
| A request without `_meta` that is not `initialize` | legacy | Served as the 1.x-SDK server did. |
| Envelope naming a revision other than 2026-07-28 | — | `-32022` `{ "supported": ["2026-07-28"], "requested": "<v>" }`; the process stays unpinned. |
| Envelope missing `clientCapabilities` (or otherwise malformed) | — | `-32602` with `data.envelope = { "key": "<meta key>", "problem": "<why>" }`. |

After pinning:

- **Legacy process:** `server/discover` → `-32601` `"Method not found"` (plain message, see section 5). Requests that carry the modern `_meta` keys are still served under legacy semantics.
- **Modern process:** `initialize` → `-32022` (supported: `2026-07-28`); a request without the envelope → `-32602`; `ping` → `-32601` (removed in 2026-07-28).

The envelope is the three `_meta` keys `io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientCapabilities` (both required) and `io.modelcontextprotocol/clientInfo` (recommended).

stdio has no header layer, so the 2026-07-28 HTTP header rules (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, `-32020 HeaderMismatch`) do not apply to this server. They apply to the enclave (section 6).

## 3. What is identical in both eras

Pinned by tests (section 7):

| Surface | Value |
|---|---|
| Tools | The 18 `TOOL_MANIFEST` entries — names, order, descriptions, `inputSchema`, `annotations` — byte-identical to the 1.x-SDK `tools/list` payload (`mcp/tests/fixtures/tools-list.golden.json`). |
| Server identity | `{ "name": "totalreclaw", "version": "1.0.0" }` (legacy: `initialize.serverInfo`; modern: `_meta["io.modelcontextprotocol/serverInfo"]` on every result). |
| Capabilities | `{ "tools": {}, "prompts": {}, "resources": { "subscribe": true, "listChanged": true } }` |
| Instructions | `SERVER_INSTRUCTIONS` (`mcp/src/prompts.ts`) — legacy: `initialize.instructions`; modern: `server/discover.instructions`. |
| Prompts, resources | `totalreclaw_instructions`, `totalreclaw_start`, `totalreclaw_save`; `memory://context/summary`. |
| Tool-call content | Whatever the dispatcher returns, unchanged. |

Modern-only additions on the wire: `resultType: "complete"` on every result; `ttlMs: 0` and `cacheScope: "private"` on `server/discover` and list results (the SDK default — no client-side caching, so a server upgrade can never serve a stale tool list).

Change notifications: when a fact is stored the server sends `notifications/resources/updated` for `memory://context/summary`. Legacy hosts receive it unsolicited (as before). Modern hosts receive it only on a `subscriptions/listen` stream that asked for `resourceSubscriptions` on that URI, stamped with `_meta["io.modelcontextprotocol/subscriptionId"]`. `toolsListChanged` is never honoured (the tool list is static; `tools.listChanged` is not advertised).

## 4. Wire examples

Legacy `initialize` result (instructions shortened):

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{},"prompts":{},"resources":{"subscribe":true,"listChanged":true}},"serverInfo":{"name":"totalreclaw","version":"1.0.0"},"instructions":"…"}}
```

Modern `server/discover` request and result:

```json
{"jsonrpc":"2.0","id":"discover-1","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"ExampleHost","version":"1.0.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}
{"jsonrpc":"2.0","id":"discover-1","result":{"resultType":"complete","supportedVersions":["2026-07-28"],"capabilities":{"tools":{},"prompts":{},"resources":{"subscribe":true,"listChanged":true}},"instructions":"…","ttlMs":0,"cacheScope":"private","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"totalreclaw","version":"1.0.0"}}}}
```

Modern `tools/call` result:

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{…}"}],"resultType":"complete","_meta":{"io.modelcontextprotocol/serverInfo":{"name":"totalreclaw","version":"1.0.0"}}}}
```

Unsupported revision:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32022,"message":"Unsupported protocol version: 2099-01-01","data":{"supported":["2026-07-28"],"requested":"2099-01-01"}}}
```

## 5. Host behaviour (as of 2026-09-27)

| Host | Opens with | Era on this server |
|---|---|---|
| Claude Desktop (local stdio servers) | `initialize` — no public statement of 2026-07-28 support for local servers | legacy |
| Claude Code, `MCP_PROTOCOL_NEGOTIATION` unset or `legacy` (default for stdio) | `initialize` | legacy |
| Claude Code, `MCP_PROTOCOL_NEGOTIATION=auto` | `server/discover` probe | modern |
| Cursor | `initialize` — its MCP docs name no protocol revision | legacy |
| NanoClaw (Claude Agent SDK), IronClaw | whatever their MCP runtime sends | either |
| Any client on MCP SDK v2 (`versionNegotiation: { mode: "auto" }`) | `server/discover` probe | modern |

Notes:

- SDK-v2 stdio clients in `auto` mode run the probe on a short-lived **sibling process** spawned with the same command, then start the real one. Expect two server starts per connect; the startup billing lookup and the (idempotent) relay registration therefore run twice.
- Claude Code 2.1.283 negotiates 2026-07-28 wrongly when a legacy server's `-32601` reply to the probe mentions a protocol version (anthropics/claude-code#97391). This server never does: before `initialize` it answers the probe properly, after `initialize` it answers `"Method not found"`.
- Claude Code 2.1.220 was reported treating a legacy server's `initialize` answer of `2025-11-25` as a 2026-07-28 session (anthropics/claude-code#97189). If a host sends `initialize` with `protocolVersion: "2026-07-28"`, this server answers `2025-11-25` and keeps serving that process, including requests that carry the modern `_meta` keys.

## 6. What the enclave (Python, Streamable HTTP) must mirror

The enclave spec (`enclave-mcp-v1.md` §4.1, §6) requires a stateless, dual-era `POST /mcp`. Mirror this server, plus the HTTP rules stdio does not have:

1. **Era per request, not per connection.** A body that is `initialize` without a modern envelope claim is legacy; a request carrying the envelope is modern. Serve both on the same endpoint.
2. **Legacy over HTTP, statelessly.** Answer `initialize` (2025-11-25 or the older revision requested); never mint an `Mcp-Session-Id`, ignore one if sent; `GET`/`DELETE` on `/mcp` → `405`.
3. **Modern header validation** (2026-07-28 Streamable HTTP): `MCP-Protocol-Version` must equal `_meta["io.modelcontextprotocol/protocolVersion"]`; `Mcp-Method` must equal `method`; `Mcp-Name` must equal `params.name` / `params.uri` for `tools/call`, `resources/read`, `prompts/get` (decode the `=?base64?…?=` form first). Missing or mismatched → HTTP `400` + JSON-RPC `-32020` (HeaderMismatch).
4. **Modern errors:** unsupported revision → `400` + `-32022` with `data.supported`; unknown method → `404` + `-32601`; malformed envelope → JSON-RPC `-32602` (the spec fixes the code, not the HTTP status).
5. **Authentication first, in both eras:** `401` + `WWW-Authenticate: Bearer resource_metadata="…"` for an unauthenticated `initialize` **and** an unauthenticated `server/discover`.
6. **Same identity and guidance in both eras:** `serverInfo` `{ "name": "totalreclaw-enclave", "version": "<release>" }`; `instructions` (the enclave text from spec §4.2) in both `initialize` and `server/discover`.
7. **Deterministic tool order.** Keep an enclave tools golden fixture and assert both eras return it, as `mcp/tests/tools-list-golden.test.ts` and `mcp/tests/dual-era-stdio.test.ts` do here.
8. **Modern result fields:** `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]` on every result; `ttlMs` + `cacheScope` on `server/discover` and list results. Use `ttlMs: 0` / `cacheScope: "private"` unless `serverInfo.version` changes every time the tool list changes (clients partition their cache by `name@version`).
9. **Notifications** only on `subscriptions/listen` streams; with `tools` as the only capability the enclave acknowledges an empty filter.

**Python SDK status.** `mcp` 2.x is the stable line since 2.0.0 (2026-07-28); 2.2.0 (2026-09-07, Python ≥ 3.10) is current and 1.x is security-fixes only. Its release notes say one `MCPServer` serves the 2026-07-28 revision and every 2025-era client over Streamable HTTP and stdio "with nothing to configure", deciding the stdio era from the opening request. The enclave spike (spec §11 Q1) should verify on 2.2.x: header validation (item 3), the `401` on `server/discover` (item 5), and that no `Mcp-Session-Id` is minted for legacy requests (item 2); if any is missing, add a thin ASGI middleware in front of the SDK app rather than forking the SDK.

## 7. Tests

| File | What it pins |
|---|---|
| `mcp/tests/tools-list-golden.test.ts` | `TOOL_MANIFEST` equals the golden captured from the 1.x-SDK server. |
| `mcp/tests/dual-era-stdio.test.ts` | Raw JSON-RPC over the SDK's `StdioServerTransport`: every row of sections 2–3, the section 5 bug shapes, change notifications in both eras, the probe fallback. |
| `mcp/tests/dual-era-client.test.ts` | The reference SDK-v2 client in `legacy`, `auto` and pinned modes sees the same tools, instructions, prompts, resources and tool results. |
| `mcp/tests/dual-era-process.test.ts` | The built `dist/index.js` over a real pipe: both eras, stdout carries only JSON-RPC, exit on stdin EOF. |
| `mcp/scripts/e2e-dual-era-staging.cjs` | Staging E2E: remember + recall in the legacy era, recall + forget in the modern era, staging DataEdge asserted before any write. |

## 8. References

- MCP 2026-07-28 changelog — https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Versioning and backward compatibility — https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- stdio transport — https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
- Streamable HTTP transport — https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- `server/discover` — https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- TypeScript SDK v2 migration — https://ts.sdk.modelcontextprotocol.io/v2/migration/upgrade-to-v2.html and https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28.html
- Python SDK v2.0.0 release — https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0
- Claude Code MCP docs (`MCP_PROTOCOL_NEGOTIATION`) — https://code.claude.com/docs/en/mcp
- anthropics/claude-code#97391 — https://github.com/anthropics/claude-code/issues/97391
- anthropics/claude-code#97189 — https://github.com/anthropics/claude-code/issues/97189
- Cursor MCP docs — https://cursor.com/docs/context/mcp
- Server spec: [mcp-server.md](./mcp-server.md)
