<p align="center">
  <img src="../docs/assets/logo.png" alt="TotalReclaw" width="80" />
</p>

<h1 align="center">TotalReclaw for NanoClaw</h1>

<p align="center">
  <strong>End-to-end encrypted memory for NanoClaw agents</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> &middot;
  <a href="../docs/guides/nanoclaw-getting-started.md">Getting Started</a> &middot;
  <a href="../docs/guides/beta-tester-guide.md">Beta Guide</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?label=MCP%20Server&color=7B5CFF" alt="npm MCP Server"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

Automatic encrypted memory + knowledge graph for NanoClaw agents, powered by the `@totalreclaw/mcp-server`. Memories are encrypted inside the container before leaving -- no server, service, or third party can read them.

Once configured, your NanoClaw agent automatically extracts facts from conversations, recalls relevant memories at the start of each session, and preserves context before compaction. No manual action needed from end users.

**v3.0.0 ships Memory Taxonomy v1** — every memory is typed (`claim` / `preference` / `directive` / `commitment` / `episode` / `summary`) and tagged with source, scope, and volatility. Inherits the four new MCP tools (`totalreclaw_pin`, `totalreclaw_unpin`, `totalreclaw_retype`, `totalreclaw_set_scope`) from the underlying MCP server. See the [memory types guide](../docs/guides/memory-types-guide.md).

## Quick Start

### Step 1: Generate a recovery phrase

The **recovery phrase** is a 12-word phrase that derives all encryption keys. TotalReclaw never sends it to any server -- it stays inside the container.

Run the setup wizard on any machine with Node.js installed:

```bash
npx @totalreclaw/mcp-server setup
```

The wizard will:
1. Generate a 12-word recovery phrase
2. Register the phrase with the managed service
3. Print the phrase for you to save

> **Save your recovery phrase somewhere safe.** It is the only way to recover your encrypted memories. If you lose it, your memories are gone permanently.

> **Note:** The first run of the MCP server downloads a ~600MB embedding model for local inference. This is cached and only happens once per deployment.

If you already have a recovery phrase from a previous TotalReclaw setup (Claude Desktop, OpenClaw, another NanoClaw instance), you can reuse it. The wizard will ask.

### Step 2: Set environment variables

Add these to your NanoClaw deployment (Docker env, `.env` file, or platform config):

```bash
TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

The NanoClaw agent-runner automatically spawns `@totalreclaw/mcp-server` as a background process with this variable. No further configuration needed.

> **Note:** The server URL defaults to `https://api.totalreclaw.xyz` (the managed service). You only need to set `TOTALRECLAW_SERVER_URL` if you are running a self-hosted server.

### Step 3: Verify

Ask your NanoClaw agent:

> "Do you have access to TotalReclaw memory tools?"

It should confirm access to tools like `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, and `totalreclaw_export`.

You can also ask:

> "What's my TotalReclaw status?"

The agent will show your tier, usage count, and storage mode.

## How It Works

### Encryption

All encryption happens **inside the MCP server process** running in the NanoClaw container:

- Facts are encrypted with XChaCha20-Poly1305 before leaving the container
- Search uses blind indices (SHA-256 hashes) -- the server never sees your queries
- The recovery phrase derives all keys via Argon2id + HKDF
- With the managed service, encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

The server only ever sees ciphertext and hashed tokens.

### Automatic Memory Hooks

NanoClaw's lifecycle hooks provide fully automatic memory -- no manual action needed from users:

| Hook | What happens |
|------|-------------|
| `before-agent-start` | Retrieves relevant memories before processing the user's message |
| `agent-end` | Extracts and stores new facts periodically after agent turns |
| `pre-compact` | Full extraction before context truncation to preserve everything |

### Namespace Mapping

The skill maps NanoClaw's `groupFolder` to TotalReclaw's `namespace`:

- `main` group folder maps to `main` namespace
- `work` group folder maps to `work` namespace
- Any custom group folder maps to a namespace of the same name

This provides memory isolation between different NanoClaw groups. Memories stored in one namespace are not visible in another.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word recovery phrase (required) | -- |
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL (only needed for self-hosted) | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | Set to `true` for self-hosted mode | `false` |
| `TOTALRECLAW_NAMESPACE` | Default namespace | Group folder name |
| `TOTALRECLAW_AUTO_EXTRACT` | Enable automatic fact extraction | `true` |

> **v3.0.0 env cleanup:** `TOTALRECLAW_CHAIN_ID` and `TOTALRECLAW_EXTRACT_INTERVAL` were removed. Chain is auto-detected from billing tier; extraction interval is server-tuned via the relay billing response. See the [env vars reference](../docs/guides/env-vars-reference.md).

## Available Tools

The MCP server provides 19 tools to the NanoClaw agent (v3.0.0 adds v1 taxonomy tools):

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a memory with v1 taxonomy (type, source, scope, reasoning) |
| `totalreclaw_recall` | Search memories; results reranked by source weight (v1 Tier 1) |
| `totalreclaw_forget` | Delete a specific memory by ID or query |
| `totalreclaw_export` | Export all memories decrypted as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota usage |
| `totalreclaw_pin` | **New in v1** — lock a memory against auto-supersession |
| `totalreclaw_unpin` | **New in v1** — remove pin lock |
| `totalreclaw_retype` | **New in v1** — change memory type |
| `totalreclaw_set_scope` | **New in v1** — assign memory to a scope |
| `totalreclaw_import` | Re-import previously exported memories |
| `totalreclaw_import_from` | Import from Mem0 or MCP Memory Server |
| `totalreclaw_upgrade` | Get a link to upgrade to Pro |

## Architecture

The NanoClaw agent-runner spawns `@totalreclaw/mcp-server` as a stdio child process. This gives NanoClaw full feature parity with Claude Desktop, Cursor, and any other MCP client.

```
NanoClaw Container
+-------------------------------------------+
|  Agent Runner                             |
|  +-------------------------------------+ |
|  | Lifecycle Hooks                      | |
|  | (before-agent-start, agent-end,      | |
|  |  pre-compact)                        | |
|  +-------------------------------------+ |
|           |                               |
|           v                               |
|  +-------------------------------------+ |
|  | @totalreclaw/mcp-server (stdio)     | |
|  | - XChaCha20-Poly1305 encryption            | |
|  | - Blind index generation            | |
|  | - Local embedding + re-ranking      | |
|  +-------------------------------------+ |
|           |                               |
+-------------------------------------------+
            | (only ciphertext leaves)
            v
   TotalReclaw Managed Service
   (relay, on-chain storage, The Graph)
```

## Shared Memory Across Platforms

Because TotalReclaw uses the same encryption and storage across all platforms, the same recovery phrase gives access to the same memories everywhere:

- Memories stored by a NanoClaw agent can be recalled in Claude Desktop
- Memories stored in OpenClaw can be recalled by NanoClaw
- Export from one platform, import into another

This makes TotalReclaw a universal memory layer across all your AI agents.

## Troubleshooting

**"TotalReclaw tools not available"**
- Verify `TOTALRECLAW_RECOVERY_PHRASE` is set in the container environment
- Check that `@totalreclaw/mcp-server` is installed in the Docker image (`npm install -g @totalreclaw/mcp-server`)
- Restart the NanoClaw agent after changing environment variables

**"Registration failed" or network errors**
- Verify the container can reach `https://api.totalreclaw.xyz`
- If using a self-hosted server, verify `TOTALRECLAW_SERVER_URL` is set and reachable

**Memories not appearing across sessions**
- Confirm the same recovery phrase is used across deployments
- Check the namespace -- memories in the `work` namespace are not visible in `main`

**Quota exceeded (403 errors)**
- Free tier allows 500 writes per month. Ask the agent: "What's my TotalReclaw status?"
- Upgrade to Pro: ask the agent "How do I upgrade TotalReclaw?"

## Learn More

- [NanoClaw Getting Started Guide](../docs/guides/nanoclaw-getting-started.md) -- detailed first-time setup walkthrough
- [Beta Tester Guide](../docs/guides/beta-tester-guide.md) -- full reference for all platforms
- [Main Repository](https://github.com/p-diogo/totalreclaw)
- [totalreclaw.xyz](https://totalreclaw.xyz)

## License

MIT
