# IronClaw Setup Guide

Set up TotalReclaw as the encrypted memory layer for your IronClaw agent. Your memories are encrypted on-device before they leave -- IronClaw's TEE protects the runtime, TotalReclaw protects the data at rest.

> **Status (paused 2026-04-18): first-class IronClaw integration is on hold.** There is no `ironclaw mcp add` / `nearai mcp add` CLI and no IronClaw lifecycle hooks. The path below uses the **generic TotalReclaw MCP server** (`@totalreclaw/mcp-server`), which works with any MCP-compatible host, IronClaw included. Because the MCP server has no IronClaw hooks, auto-extraction has to be driven by a cron-style routine (see §5); recall happens when the agent decides to call a tool.

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

You should see 18 tools. Account setup uses `totalreclaw_pair`, which is browser-mediated (see §3).

## 3. Pair your account (browser-mediated — recovery phrase never enters chat)

Restart IronClaw and start a conversation. The first time you mention anything worth remembering (e.g. "remember that my name is Pedro"), the agent will detect that TotalReclaw is not configured yet and offer to set it up with the `totalreclaw_pair` tool.

`totalreclaw_pair` opens a short relay session and returns a **URL + 6-digit PIN**. The 12-word recovery phrase is generated (or imported) **in your browser** — never typed into chat:

1. The agent calls `totalreclaw_pair` (`mode: "generate"` for a new phrase, or `mode: "import"` to reuse one you already have) and hands you the URL + PIN.
2. Open the URL on your phone or another browser and enter the PIN.
3. The browser generates a new 12-word BIP-39 phrase (and asks you to write it down + retype 3 words) — or, in `import` mode, accepts a phrase you paste in the browser.
4. The browser encrypts the phrase (x25519 ECDH + AES-256-GCM) and uploads ciphertext to the MCP server. The phrase never touches the LLM context, the chat transcript, stdout, or logs.
5. The MCP server decrypts the phrase in-memory, derives your keys, registers with the relay, and writes `~/.totalreclaw/credentials.json` (mode 0600, owner-only).
6. **Restart IronClaw** so the MCP server re-reads `credentials.json` and switches out of unconfigured mode. The pairing session expires in ~5 minutes.

On subsequent restarts the MCP server loads `~/.totalreclaw/credentials.json` automatically — no pairing needed.

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

The MCP server exposes 18 tools. Descriptions are summarized from the tool definitions in `mcp/src/tools/`.

| Tool | Description |
|------|-------------|
| `totalreclaw_pair` | Set up the account: browser-mediated recovery-phrase generation or import. Returns a URL + PIN; the phrase is created in the browser and never enters LLM context. |
| `totalreclaw_remember` | Store an atomic fact proactively (with v1 type, scope, importance). Dedup is automatic. |
| `totalreclaw_recall` | Search the encrypted vault by natural-language query; top 8 reranked by provenance + semantic + recency. |
| `totalreclaw_forget` | Permanently remove a memory by `memory_id`, or by query (tombstones up to 50 matches). |
| `totalreclaw_export` | Export the decrypted vault as Markdown or JSON (one-click portable backup). |
| `totalreclaw_import` | Restore memories from a prior TotalReclaw export backup (JSON/Markdown). |
| `totalreclaw_import_from` | Import from Mem0, MCP Memory, ChatGPT, Claude, Gemini, MemoClaw, or JSON/CSV. Dry-run first. |
| `totalreclaw_import_batch` | Internal chunked-polling helper for large imports (50+ conversations); not meant to be called by name. |
| `totalreclaw_consolidate` | Cluster near-duplicates and merge each to the best match. **Self-hosted only** (no batch delete on the managed service). |
| `totalreclaw_status` | Check tier, usage, and quota against the relay billing endpoint. |
| `totalreclaw_upgrade` | Get a one-time Stripe checkout URL for Pro. |
| `totalreclaw_debrief` | Store a structured Crystal session summary at the end of a substantive conversation. |
| `totalreclaw_support` | Get support contact, docs links, and a troubleshooting bundle. Works even when unconfigured. |
| `totalreclaw_account` | Account overview: wallet, tier, usage, feature flags, and a safe (first + last words only) recovery hint. |
| `totalreclaw_pin` | Lock a memory against auto-supersession. |
| `totalreclaw_unpin` | Remove a pin so a memory can be superseded again. |
| `totalreclaw_retype` | Change a memory's v1 type (e.g. preference → directive). |
| `totalreclaw_set_scope` | Assign a memory to a scope (work, personal, health, ...). |

## Pricing

- **Free tier** — 250 memories/month on Gnosis mainnet. Permanent storage. Cosine dedup (paraphrase detection). E2E encrypted. No credit card required.
- **Pro tier** — 1,500 memories/month on Gnosis mainnet. Permanent. LLM-guided dedup (catches contradictions). Custom extraction interval. Pay via the `totalreclaw_upgrade` tool or visit [totalreclaw.xyz/pricing](https://totalreclaw.xyz/pricing/). See `totalreclaw_status` for current pricing.

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

Check your internet connection. The pairing flow needs to reach `api.totalreclaw.xyz` to register.

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
