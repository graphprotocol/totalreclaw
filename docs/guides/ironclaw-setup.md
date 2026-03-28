# IronClaw Setup Guide

Set up TotalReclaw as the encrypted memory layer for your IronClaw agent. Your memories are encrypted on-device before they leave -- IronClaw's TEE protects the runtime, TotalReclaw protects the data at rest.

> **Note:** TotalReclaw requires a local IronClaw installation (not NEAR AI hosted). The hosted environment does not include Node.js, which is needed to run the MCP server. Installing TotalReclaw as a "Skill" from ClawHub only injects instructions -- it does not register the tools.

## Prerequisites

- **Local IronClaw installation** -- hosted IronClaw (NEAR AI Cloud) is not supported (no Node.js runtime in the TEE container)
- **IronClaw v0.22+** (with MCP client support)
- **Node.js 22+** (for the MCP server process)
- ~600 MB disk space for the local embedding model (one-time download)

## 1. Install and run the setup wizard

```bash
npx @totalreclaw/mcp-server setup
```

The wizard:
1. Generates a 12-word recovery phrase (or imports an existing one)
2. Derives encryption keys locally
3. Registers with the TotalReclaw relay
4. Downloads the embedding model for local semantic search (~600 MB, cached)
5. Prints a config snippet

> **Save your recovery phrase somewhere safe.** It is the only key to your memories. There is no password reset, no recovery, no support ticket that can help. Write it down and store it securely.

> **Credential vault tip:** If IronClaw's credential vault is available, store the recovery phrase there instead of as an environment variable. This keeps it isolated from the LLM and WASM tools.

## 2. Add TotalReclaw to IronClaw

IronClaw connects to MCP servers via its built-in MCP client. Add TotalReclaw to your IronClaw MCP configuration:

### Option A: IronClaw config file

Add to your IronClaw MCP configuration (typically `~/.ironclaw/mcp.json` or via the IronClaw dashboard):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

> **Note:** The server URL defaults to `https://api.totalreclaw.xyz` (the managed service). You only need to set `TOTALRECLAW_SERVER_URL` if you are running a self-hosted server.

### Option B: IronClaw CLI

```bash
ironclaw mcp add totalreclaw --command "npx @totalreclaw/mcp-server" \
  --env TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase goes here"
```

> Replace `your twelve word recovery phrase goes here` with the actual phrase from the setup wizard.

## 3. Verify the connection

Ask your IronClaw agent:

> "Do you have access to TotalReclaw memory tools?"

The agent should confirm it can see tools like `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, `totalreclaw_export`, and `totalreclaw_status`.

## 4. First use

TotalReclaw's MCP server instructs the agent to use memory proactively. Once connected:

- **Automatic recall**: The agent searches your memory at the start of every conversation
- **Proactive storage**: The agent stores preferences, decisions, and important context without being asked
- **Manual commands**: You can also say "remember that I prefer dark mode" or "what do you know about my projects?"

### Example conversation

```
You: I'm working on a Rust project called skynet-lite. It's a lightweight task scheduler.

Agent: [Automatically stores: "User is working on a Rust project called skynet-lite, a lightweight task scheduler"]

--- next conversation ---

You: Can you help me with my project?

Agent: [Recalls skynet-lite context] Sure! For skynet-lite, your Rust task scheduler...
```

## 5. Set up auto-extraction with routines (optional)

IronClaw supports background routines for periodic tasks. You can set up a routine to extract and store memories after conversations automatically.

### Cron-based extraction routine

Create a routine that periodically prompts your agent to review recent conversations and store important facts:

```json
{
  "name": "totalreclaw-extract",
  "schedule": "*/30 * * * *",
  "prompt": "Review your recent conversations. Extract any important facts, preferences, decisions, or context the user shared and store them using totalreclaw_remember. Extract atomic facts with appropriate importance scores (7-9 for core identity, 4-6 for moderate facts, 1-3 for minor details)."
}
```

> **Note:** IronClaw does not have OpenClaw-style lifecycle hooks (`agent_end`, `before_agent_start`). The MCP server's prompt layer handles conversation-start recall automatically, but end-of-conversation extraction depends on either the agent's own initiative or a routine like the one above.

### Message-triggered extraction (if supported)

If your IronClaw version supports event-triggered routines (e.g., `on_thread_idle`), you can trigger extraction after each conversation ends:

```json
{
  "name": "totalreclaw-extract",
  "trigger": "on_thread_idle",
  "delay": "5s",
  "prompt": "Extract important facts from the conversation that just ended and store them with totalreclaw_remember."
}
```

## Available tools

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store facts in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota |
| `totalreclaw_import` | Re-import previously exported memories |
| `totalreclaw_import_from` | Import from Mem0 or MCP Memory Server |
| `totalreclaw_consolidate` | Merge duplicate memories (self-hosted only) |
| `totalreclaw_upgrade` | Get a Stripe checkout link for Pro |
| `totalreclaw_migrate` | Migrate testnet memories to mainnet after Pro upgrade |

## Pricing

| Tier | Memories | Storage | Price |
|------|----------|---------|-------|
| **Free** | 500/month | Testnet (trial) | $0 |
| **Pro** | Unlimited | Permanent on-chain (Gnosis) | See `totalreclaw_status` |

Ask your agent to run `totalreclaw_status` to check current pricing and usage.

## Security model

TotalReclaw and IronClaw provide complementary security layers:

| Layer | IronClaw | TotalReclaw |
|-------|----------|-------------|
| **Runtime isolation** | TEE (Intel TDX) | -- |
| **Tool sandboxing** | WASM sandbox | -- |
| **Data encryption** | -- | AES-256-GCM (client-side) |
| **Key management** | Credential vault | Recovery phrase (BIP-39) |
| **Storage** | PostgreSQL (TEE-locked) | On-chain (Gnosis Chain) |
| **Portability** | Machine-locked | Any agent, any device |

IronClaw's TEE protects your data while it is being processed. TotalReclaw protects your data at rest and in transit -- even if the storage layer is fully compromised, only ciphertext is exposed.

### LLM provider privacy

TotalReclaw encrypts memories at rest and in transit to the relay. However, recalled memories are decrypted locally and injected as context into LLM requests. If your IronClaw agent uses a third-party LLM (OpenAI, Anthropic, etc.), decrypted memories will be visible to that provider.

To keep memories private end-to-end, use one of these LLM backends:
- **`nearai`** -- NEAR AI private inference (TEE-based, hardware-isolated)
- **`ollama`** -- local inference, nothing leaves your machine
- **`tinfoil`** -- third-party confidential inference via TEEs

This is not specific to TotalReclaw -- any memory system that provides context to an LLM has this property.

## Troubleshooting

### "MCP server not found"

Make sure Node.js 22+ is installed and `npx @totalreclaw/mcp-server` works from your terminal.

### "Registration failed"

Check your internet connection. The setup wizard needs to reach `api.totalreclaw.xyz` to register.

### "No memories found" on first recall

The first time you use TotalReclaw, there are no memories yet. Start a conversation, share some information, and the agent will store it automatically. Memories should appear in subsequent conversations.

### Slow first response

The embedding model (~600 MB) downloads on first use if it was not pre-downloaded during setup. This is a one-time cost. Subsequent runs use the cached model.

### Recovery phrase in credential vault

If IronClaw supports a credential vault, store `TOTALRECLAW_RECOVERY_PHRASE` there instead of in the MCP config. This ensures the phrase is never exposed to the LLM or WASM tools -- only the MCP server process can access it.

## Portability

Your recovery phrase works across all TotalReclaw-compatible agents:

- **IronClaw** (this guide)
- **OpenClaw** (`openclaw skills install totalreclaw`)
- **Claude Desktop / Cursor / Windsurf** (`npx @totalreclaw/mcp-server setup`)
- **NanoClaw** (set `TOTALRECLAW_RECOVERY_PHRASE` in deployment config)

Same phrase, same memories. Switch agents without losing anything.

## Learn more

- [Getting Started Guide](beta-tester-guide.md) -- full reference with configuration details
- [Architecture Deep Dive](../architecture.md) -- encryption, LSH, search, deduplication
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage
