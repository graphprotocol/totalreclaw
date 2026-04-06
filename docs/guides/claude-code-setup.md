# TotalReclaw Setup for Claude Code, Claude Desktop, Cursor, and Windsurf

Encrypted, cross-session memory for MCP-compatible AI agents. Set up in under 2 minutes.

---

## Prerequisites

- **Node.js 18+** (22 recommended)
- **An MCP-compatible host app:** Claude Code, Claude Desktop, Cursor, Windsurf, or similar
- ~34 MB disk space for the embedding model (one-time download, cached locally)

---

## 1. Get your recovery phrase

You have two options:

### Option A: Run the setup wizard (recommended for new users)

```bash
npx @totalreclaw/mcp-server setup
```

The wizard will:

1. Ask if you have an existing recovery phrase or need a new one
2. Generate a 12-word recovery phrase (BIP-39) if needed
3. Register with the TotalReclaw relay
4. Print a config snippet to paste into your host app

### Option B: Use an existing recovery phrase directly

If you already have a recovery phrase from another TotalReclaw client (OpenClaw, NanoClaw, Hermes, ZeroClaw), you can skip the wizard entirely. Just set `TOTALRECLAW_RECOVERY_PHRASE` in your host app config (step 2 below). The MCP server automatically registers with the relay on startup -- no manual registration step is needed.

> **Save your recovery phrase somewhere safe.** It is the only key to your encrypted memories. There is no password reset, no recovery email, no support ticket that can help. If you lose it, your memories are permanently unrecoverable. This is by design.

---

## 2. Add to your host app

Copy the config snippet from the wizard into your app's MCP configuration file.

### Claude Code

Add to `~/.claude.json` (or use `claude mcp add`):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project root (or global settings via Settings > MCP Servers):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

---

## 3. Verify

Restart your host app and ask:

> "Do you have access to TotalReclaw memory tools?"

The agent should confirm it can see the tools. If not, check the troubleshooting section below.

## Available tools

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a fact in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories as Markdown or JSON (self-hosted only) |
| `totalreclaw_status` | Check billing status, tier, and usage |
| `totalreclaw_setup` | First-time setup (if not done via the CLI wizard) |
| `totalreclaw_upgrade` | Get a Stripe checkout link to upgrade to Pro |
| `totalreclaw_migrate` | Migrate testnet memories to mainnet after Pro upgrade |
| `totalreclaw_import_from` | Import from Mem0, ChatGPT, Claude, or MCP Memory Server |
| `totalreclaw_import` | Re-import previously exported JSON or Markdown (self-hosted only) |
| `totalreclaw_consolidate` | Merge duplicate memories (self-hosted only) |
| `totalreclaw_debrief` | Extract and store key takeaways from the current session |
| `totalreclaw_support` | Get help with common issues |
| `totalreclaw_account` | View account details |

---

## Usage examples

You do not need special syntax. Just talk naturally:

- "Remember that I prefer TypeScript for backend services"
- "What do you know about my project preferences?"
- "Forget the memory about my old address"
- "Show my TotalReclaw status"
- "Export all my memories as Markdown"
- "Import my memories from Mem0 using API key m0-xxx"

The MCP server also instructs the agent to recall relevant memories at the start of each conversation and to proactively store important facts you share (preferences, decisions, context). You can always be explicit, but much of it happens automatically.

## How it works

1. **All encryption happens on your machine.** Memories are encrypted with AES-256-GCM before leaving your device. The server only ever sees ciphertext.
2. **Your recovery phrase is your identity.** It derives all encryption keys via Argon2id + HKDF. Same phrase on any device or agent = same memories.
3. **Search is privacy-preserving.** Queries use blind indices (SHA-256 hashes) and locality-sensitive hashing -- the server never sees your search terms.
4. **Free tier** stores unlimited memories on Base Sepolia testnet ($0, testnet data may be reset). **Pro** stores permanently on Gnosis mainnet ($3.99/month via Stripe). Check current pricing with `totalreclaw_status` or at [totalreclaw.xyz/pricing](https://totalreclaw.xyz/pricing/).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Embedding model download is slow | Normal on first run (~34 MB). Check disk space and retry if it fails. Subsequent runs use the cached model. |
| "Not configured" error | Run `npx @totalreclaw/mcp-server setup` or ask the agent to use the `totalreclaw_setup` tool. |
| Agent does not see TotalReclaw tools | Restart the host app after adding the config. Verify with "Do you have access to TotalReclaw memory tools?" |
| "Not authenticated" / 401 | Check your recovery phrase -- exact words, exact order, no extra spaces. The MCP server auto-registers with the relay on startup, so no manual registration step is needed. If the error persists, verify your internet connection and restart the host app. |
| Recovery phrase lost | Memories are permanently unrecoverable. This is by design for end-to-end encryption security. |
| Quota exceeded (403) | Free tier has a monthly write cap. Use `totalreclaw_upgrade` to move to Pro. |

---

## Multi-device and portability

Your recovery phrase works across every TotalReclaw-compatible agent:

- **Claude Code / Claude Desktop / Cursor / Windsurf** (this guide)
- **OpenClaw** (`openclaw skills install totalreclaw`)
- **NanoClaw** (set `TOTALRECLAW_RECOVERY_PHRASE` in deployment config)
- **IronClaw** (see [IronClaw setup guide](./ironclaw-setup.md))
- **Hermes Agent** (Python client)
- **ZeroClaw** (Rust crate)

Same phrase, same memories. Switch agents without losing anything.

## Learn more

- [Getting Started Guide](./beta-tester-guide.md) -- full reference with architecture and configuration details
- [Importing Memories](./importing-memories.md) -- migrate from Mem0, MCP Memory Server, ChatGPT, and Claude
- [IronClaw Setup](./ironclaw-setup.md) -- setup guide for IronClaw (NEAR AI) agents
- [MCP Server README](../../mcp/README.md) -- environment variables and development info
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage

---

*TotalReclaw v1.0-beta -- [totalreclaw.xyz](https://totalreclaw.xyz)*
