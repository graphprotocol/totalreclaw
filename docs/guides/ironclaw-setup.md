# IronClaw Setup Guide

Set up TotalReclaw as the encrypted memory layer for your IronClaw agent. Your memories are encrypted on-device before they leave -- IronClaw's TEE protects the runtime, TotalReclaw protects the data at rest.

> **Note:** TotalReclaw requires a local IronClaw installation (not NEAR AI hosted). The hosted environment does not include Node.js, which is needed to run the MCP server. Installing TotalReclaw as a "Skill" from ClawHub only injects instructions -- it does not register the tools.

## Prerequisites

- **Local IronClaw installation** -- hosted IronClaw (NEAR AI Cloud) is not supported (no Node.js runtime in the TEE container)
- **IronClaw v0.22+** (with MCP client support)
- **Node.js 22+** (for the MCP server process)
- ~600 MB disk space for the local embedding model (one-time download)

## 1. Install TotalReclaw

```bash
npm install -g @totalreclaw/mcp-server
```

## 2. Add to IronClaw

> **Known issue:** The `ironclaw mcp add` command shown below may not exist in current versions of the `nearai` CLI. If this command is not available, you can configure the MCP server manually by adding it to your IronClaw MCP config file (typically `~/.nearai/mcp.json` or similar). Consult IronClaw documentation for the current MCP integration method.

```bash
ironclaw mcp add totalreclaw --transport stdio --command totalreclaw-mcp
```

That's it -- no environment variables, no config files, no recovery phrase needed yet.

Verify the connection:

```bash
ironclaw mcp test totalreclaw
```

You should see 14 tools including `totalreclaw_setup`.

## 3. Start chatting

Restart IronClaw and start a conversation. The first time you mention anything worth remembering (e.g. "remember that my name is Pedro"), the agent will:

1. Detect that TotalReclaw is not configured yet
2. Ask: "Do you have an existing recovery phrase, or should I generate a new one?"
3. Generate or import your phrase via the `totalreclaw_setup` tool
4. Register with the relay, download the embedding model, and activate -- all automatically

Your recovery phrase and credentials are saved to `~/.totalreclaw/credentials.json` (owner-only permissions). On subsequent restarts, the MCP server loads them automatically.

> **Save your recovery phrase somewhere safe.** It is the only key to your memories. There is no password reset, no recovery, no support ticket that can help. Write it down and store it securely.

## 4. How it works

Once set up, TotalReclaw works automatically:

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
| `totalreclaw_setup` | Set up TotalReclaw (generate or import recovery phrase) |
| `totalreclaw_remember` | Store facts in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories as Markdown or JSON |
| `totalreclaw_status` | Check billing status and usage |
| `totalreclaw_import` | Re-import previously exported memories |
| `totalreclaw_import_from` | Import from Mem0, ChatGPT, Claude, or MCP Memory Server |
| `totalreclaw_consolidate` | Merge duplicate memories (self-hosted only) |
| `totalreclaw_upgrade` | Get a Stripe checkout link for Pro |
| `totalreclaw_migrate` | Migrate testnet memories to mainnet after Pro upgrade |
| `totalreclaw_debrief` | Summarize and store key takeaways from a session |
| `totalreclaw_support` | Get help and troubleshooting information |
| `totalreclaw_account` | View account details and wallet address |

## Pricing

| Tier | Memories | Storage | Price |
|------|----------|---------|-------|
| **Free** | Unlimited | Testnet (trial -- may be reset) | $0 |
| **Pro** | Unlimited | Permanent on-chain (Gnosis) | See [totalreclaw.xyz/pricing](https://totalreclaw.xyz/pricing/) |

Upgrade anytime via the `totalreclaw_upgrade` tool -- the agent handles it for you.

## Security model

TotalReclaw and IronClaw provide complementary security layers:

| Layer | IronClaw | TotalReclaw |
|-------|----------|-------------|
| **Runtime isolation** | TEE (Intel TDX) | -- |
| **Tool sandboxing** | WASM sandbox | -- |
| **Data encryption** | -- | XChaCha20-Poly1305 (client-side) |
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

### "MCP server not found" or activation timeout

Make sure Node.js 22+ is installed and `totalreclaw-mcp` is available:

```bash
npm install -g @totalreclaw/mcp-server
which totalreclaw-mcp
```

### "Registration failed"

Check your internet connection. The setup tool needs to reach `api.totalreclaw.xyz` to register.

### "No memories found" on first recall

The first time you use TotalReclaw, there are no memories yet. Start a conversation, share some information, and the agent will store it automatically. Memories should appear in subsequent conversations.

### Slow first response

The embedding model (~600 MB) downloads on first use. This is a one-time cost. Subsequent runs use the cached model.

### Agent uses built-in memory instead of TotalReclaw

IronClaw has its own `memory_write`/`memory_read` tools. If the agent uses those instead of TotalReclaw, tell it explicitly: "Use totalreclaw_remember instead of memory_write for all memory operations."

## Portability

Your recovery phrase works across all TotalReclaw-compatible agents:

- **IronClaw** (this guide)
- **OpenClaw** (`openclaw skills install totalreclaw`)
- **Claude Desktop / Cursor / Windsurf** (`npx @totalreclaw/mcp-server setup`)
- **NanoClaw** (set `TOTALRECLAW_RECOVERY_PHRASE` in deployment config)

Same phrase, same memories. Switch agents without losing anything.

## Learn more

- [Getting Started Guide](beta-tester-guide.md) -- full reference with configuration details
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage
- [Pricing](https://totalreclaw.xyz/pricing/) -- free and Pro tier details
