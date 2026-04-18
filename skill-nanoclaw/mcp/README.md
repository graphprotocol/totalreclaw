# NanoClaw TotalReclaw Integration

NanoClaw uses the published `@totalreclaw/mcp-server` package for end-to-end
encrypted memory — the same MCP server used by Claude Desktop, Cursor, and any
MCP-compatible agent.

## Architecture

The NanoClaw agent-runner (`nanoclaw-agent-runner.ts`) spawns `@totalreclaw/mcp-server`
as a stdio child process. This ensures full feature parity with all other MCP clients:

- All 7 tools (remember, recall, forget, export, import, status, upgrade)
- Subgraph mode (on-chain storage via Gnosis Chain)
- Billing/quota handling
- Resources and prompts

## Files

| File | Purpose |
|------|---------|
| `nanoclaw-agent-runner.ts` | NanoClaw agent-runner that registers TotalReclaw MCP server |

## Dependencies

The NanoClaw Docker image must include `@totalreclaw/mcp-server`:

```dockerfile
RUN npm install -g @totalreclaw/mcp-server
```

## Environment Variables

| Variable | Required | Default |
|----------|----------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | Yes | — (12-word BIP-39 recovery phrase) |
| `TOTALRECLAW_SERVER_URL` | No | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | No | `false` (managed service with on-chain storage via The Graph) |
| `TOTALRECLAW_CREDENTIALS_PATH` | No | `/workspace/.totalreclaw/credentials.json` |
| `TOTALRECLAW_CACHE_PATH` | No | `/workspace/.totalreclaw/cache.enc` |

Chain ID is no longer user-configurable (auto-detected from billing tier:
free = Base Sepolia, Pro = Gnosis). See [`docs/guides/env-vars-reference.md`](../../docs/guides/env-vars-reference.md).
