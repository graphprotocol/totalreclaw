# @totalreclaw/mcp-server

Encrypted memory for your AI agent — zero-knowledge E2EE, semantic search, and portable storage.

Works with Claude Desktop, Cursor, Windsurf, and any MCP-compatible client. Your memories are encrypted on your device before leaving — no one can read them, not even us.

**[totalreclaw.xyz](https://totalreclaw.xyz)**

## Quick Start

### 1. Generate your recovery phrase

Your **recovery phrase** is a 12-word BIP-39 mnemonic that derives all encryption keys. TotalReclaw never sends it to the server — it stays on your machine.

**New user:** Generate a mnemonic using any BIP-39 tool, or let TotalReclaw generate one on first run.

**Returning user:** Use your existing 12-word phrase to restore your memories on a new device.

> **Save your recovery phrase somewhere safe.** It's the only way to recover your encrypted memories.

### 2. Add to your MCP client

#### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_SERVER_URL": "https://api.totalreclaw.xyz",
        "TOTALRECLAW_MASTER_PASSWORD": "your twelve word recovery phrase goes here replace these words"
      }
    }
  }
}
```

#### Cursor / Windsurf

Add to your MCP settings (Settings > MCP Servers):

```json
{
  "totalreclaw": {
    "command": "npx",
    "args": ["-y", "@totalreclaw/mcp-server"],
    "env": {
      "TOTALRECLAW_SERVER_URL": "https://api.totalreclaw.xyz",
      "TOTALRECLAW_MASTER_PASSWORD": "your twelve word recovery phrase goes here replace these words"
    }
  }
}
```

### 3. Verify

Ask your AI agent: *"Do you have access to TotalReclaw memory tools?"*

It should confirm access to `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, and `totalreclaw_export`.

## How It Works

All encryption happens **client-side** inside the MCP server process on your machine:

1. Facts are encrypted with AES-256-GCM before leaving your device
2. Search uses blind indices (SHA-256 hashes) — the server never sees your search terms
3. Your recovery phrase derives all keys via Argon2id + HKDF — the server never sees it
4. Encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

The server only ever sees ciphertext and hashed tokens.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_MASTER_PASSWORD` | 12-word BIP-39 recovery phrase | Required |
| `TOTALRECLAW_SELF_HOSTED` | Set to `true` to use a self-hosted server instead of the managed service. | `false` |
| `TOTALRECLAW_CHAIN_ID` | Chain ID (100=Gnosis mainnet, 10200=Chiado testnet). Managed service only. | `100` |

## Available Tools

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a fact in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories decrypted as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota usage |
| `totalreclaw_import` | Re-import previously exported memories (JSON/Markdown) |
| `totalreclaw_import_from` | Import from Mem0 or MCP Memory Server |
| `totalreclaw_consolidate` | Merge duplicate and related memories |
| `totalreclaw_upgrade` | Get a link to upgrade to Pro |

## Free Tier & Pricing

| Tier | Writes | Reads | Price |
|------|--------|-------|-------|
| **Free** | 250/month | Unlimited | $0 |
| **Pro** | 10,000/month | Unlimited | $2-5/month |

Pay with card (Stripe). Counter resets monthly.

## Development

```bash
npm run build    # Build the package
npm test         # Run tests
npm run lint     # Lint code
```

## License

MIT
