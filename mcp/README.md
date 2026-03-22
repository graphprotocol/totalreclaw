<p align="center">
  <img src="../docs/assets/logo.png" alt="TotalReclaw" width="80" />
</p>

<h1 align="center">@totalreclaw/mcp-server</h1>

<p align="center">
  <strong>Encrypted memory for Claude Desktop, Cursor, Windsurf, and any MCP-compatible agent</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> &middot;
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server">npm</a> &middot;
  <a href="../docs/guides/beta-tester-guide.md">Getting Started</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?color=7B5CFF" alt="npm version"></a>
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/dm/@totalreclaw/mcp-server" alt="npm downloads"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

End-to-end encrypted memory vault as an MCP server. Your memories are encrypted on your device before leaving -- no one can read them, not even us.

## Quick Start

### 1. Run the setup wizard

```bash
npx @totalreclaw/mcp-server setup
```

The wizard generates your 12-word recovery phrase, registers you, and prints a config snippet for your MCP client.

> **Save your recovery phrase somewhere safe.** It's the only way to recover your encrypted memories.

> **Note:** The first run downloads a ~600MB embedding model for local inference. This is cached locally and only happens once.

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
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
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
      "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
    }
  }
}
```

### 3. Verify

Ask your agent: *"Do you have access to TotalReclaw memory tools?"*

## How It Works

All encryption happens **client-side** inside the MCP server process on your machine:

1. Facts are encrypted with AES-256-GCM before leaving your device
2. Search uses blind indices (SHA-256 hashes) -- the server never sees your queries
3. Your recovery phrase derives all keys via Argon2id + HKDF
4. Encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

The server only ever sees ciphertext and hashed tokens.

## Available Tools

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a fact in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories decrypted as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota usage |
| `totalreclaw_import` | Re-import previously exported memories |
| `totalreclaw_import_from` | Import from Mem0 or MCP Memory Server |
| `totalreclaw_consolidate` | Merge duplicate and related memories |
| `totalreclaw_upgrade` | Get a link to upgrade to Pro |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word BIP-39 recovery phrase | Required |
| `TOTALRECLAW_SELF_HOSTED` | Use a self-hosted server instead of the managed service | `false` |
| `TOTALRECLAW_CHAIN_ID` | Chain ID (100=Gnosis mainnet, 84532=Base Sepolia staging) | `100` |

## Free Tier & Pricing

| Tier | Memories | Reads | Storage | Price |
|------|----------|-------|---------|-------|
| **Free** | 500/month | Unlimited | Testnet (trial) | $0 |
| **Pro** | Unlimited | Unlimited | Permanent on-chain (Gnosis) | $5/month |

Pay with card via Stripe. Counter resets monthly.

## Development

```bash
npm run build    # Build
npm test         # Run tests
npm run lint     # Lint
```

## Learn More

- [Getting Started Guide](../docs/guides/beta-tester-guide.md)
- [totalreclaw.xyz](https://totalreclaw.xyz)
- [Main Repository](https://github.com/p-diogo/totalreclaw)

## License

MIT
