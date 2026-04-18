<p align="center">
  <img src="../docs/assets/logo.png" alt="TotalReclaw" width="80" />
</p>

<h1 align="center">@totalreclaw/mcp-server</h1>

<p align="center">
  <strong>Encrypted memory for Claude Desktop, Cursor, Windsurf, IronClaw, and any MCP-compatible agent</strong>
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

**v3.0.0 ships Memory Taxonomy v1** — 6 speech-act types + source / scope / volatility axes on every memory. Four new tools (`totalreclaw_pin`, `totalreclaw_unpin`, `totalreclaw_retype`, `totalreclaw_set_scope`) let agents override categorization via natural language. Source-weighted reranking ranks user-authored claims above assistant-regurgitated noise. See [memory types guide](../docs/guides/memory-types-guide.md).

**Requirements:** Node.js 18+

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
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
      }
    }
  }
}
```

> **Note:** The server URL defaults to `https://api.totalreclaw.xyz` (the managed service). You only need to set `TOTALRECLAW_SERVER_URL` if you are running a self-hosted server.

#### Cursor / Windsurf

Add to your MCP settings (Settings > MCP Servers):

```json
{
  "totalreclaw": {
    "command": "npx",
    "args": ["-y", "@totalreclaw/mcp-server"],
    "env": {
      "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase goes here"
    }
  }
}
```

#### IronClaw (NEAR AI)

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

If IronClaw supports a credential vault, store `TOTALRECLAW_RECOVERY_PHRASE` there instead of in the config file. See the [IronClaw setup guide](../docs/guides/ironclaw-setup.md) for the full walkthrough including background routines.

### 3. Verify

Ask your agent: *"Do you have access to TotalReclaw memory tools?"*

## How It Works

All cryptographic operations (XChaCha20-Poly1305, HKDF key derivation, LSH hashing, blind indices, content fingerprinting) are powered by [`@totalreclaw/core`](https://www.npmjs.com/package/@totalreclaw/core) -- a unified Rust/WASM module shared across all TotalReclaw clients.

All encryption happens **client-side** inside the MCP server process on your machine:

1. Facts are encrypted with XChaCha20-Poly1305 before leaving your device
2. Search uses blind indices (SHA-256 hashes) -- the server never sees your queries
3. Your recovery phrase derives all keys via Argon2id + HKDF
4. Encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

The server only ever sees ciphertext and hashed tokens.

## Available Tools

All 19 tools are invoked by the host agent from natural language context. Tool schemas include v1 taxonomy fields (`type`, `source`, `scope`, `reasoning`).

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a fact in encrypted memory. Supports v1 taxonomy (`type`, `scope`, `reasoning`) + legacy types for migration |
| `totalreclaw_recall` | Search memories by natural language query. Source-weighted ranking (Retrieval v2 Tier 1) |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_pin` | Pin a memory so auto-resolution never supersedes it. Accepts `fact_id` or `memory_id` |
| `totalreclaw_unpin` | Remove the pin, returning the memory to active status |
| `totalreclaw_retype` *(new in v3.0.0)* | Change the v1 type of a memory (e.g. `preference` → `directive`) via supersession |
| `totalreclaw_set_scope` *(new in v3.0.0)* | Change the v1 scope of a memory (e.g. set to `work` / `personal`) |
| `totalreclaw_export` | Export all memories decrypted as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota usage |
| `totalreclaw_import` | Re-import previously exported memories |
| `totalreclaw_import_from` | Import from Mem0, MCP Memory Server, ChatGPT, Claude, or generic JSON/CSV |
| `totalreclaw_import_batch` | Batch import with background polling |
| `totalreclaw_consolidate` | Merge duplicate and related memories |
| `totalreclaw_debrief` | End-of-conversation summary to capture broader context |
| `totalreclaw_upgrade` | Get a link to upgrade to Pro |
| `totalreclaw_migrate` | Migrate testnet memories to mainnet after Pro upgrade |
| `totalreclaw_account` | View account details (wallet, tier, quota, phrase hint) |
| `totalreclaw_support` | Troubleshooting help + contact links |
| `totalreclaw_setup` | Generate or import a recovery phrase (first-run flow) |

Users invoke the new v1 tools naturally: *"pin that"*, *"that was actually a rule, not a preference"*, *"file that under work"*. Tool descriptions teach the host LLM to match utterances to tools.

### Memory Taxonomy v1 (`@totalreclaw/core 2.0`)

MCP server v3.0.0 writes v1 inner blobs (outer protobuf wrapper `version = 4`).
The six v1 types correspond to Searle's speech-act classes:

- **claim** — assertive ("lives in Lisbon", "chose PostgreSQL"). Absorbs legacy fact/context/decision.
- **preference** — expressive ("likes dark mode").
- **directive** — imperative ("always check d.get(errors)"). Replaces legacy `rule`.
- **commitment** — commissive ("will ship v2 Friday"). Replaces legacy `goal`.
- **episode** — narrative ("deployed v1.0 on March 15"). Replaces legacy `episodic`.
- **summary** — derived synthesis (debrief pipelines only).

v0 types (`fact`, `context`, `decision`, `rule`, `goal`, `episodic`) are still
accepted by the `remember` tool and auto-mapped to v1 equivalents. Recall
output includes `source` and `scope` when available.

See [`docs/specs/totalreclaw/memory-taxonomy-v1.md`](../docs/specs/totalreclaw/memory-taxonomy-v1.md).

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word BIP-39 recovery phrase | Required |
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL (only needed for self-hosted) | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | Use a self-hosted server instead of the managed service | `false` |
| `TOTALRECLAW_CREDENTIALS_PATH` | Override credentials file location | `~/.totalreclaw/credentials.json` |
| `TOTALRECLAW_CACHE_PATH` | Override encrypted cache file location | `~/.totalreclaw/cache.enc` |

> **v1 env cleanup:** `TOTALRECLAW_CHAIN_ID`, `TOTALRECLAW_EMBEDDING_MODEL`, `TOTALRECLAW_STORE_DEDUP`, `TOTALRECLAW_LLM_MODEL`, `TOTALRECLAW_SESSION_ID`, `TOTALRECLAW_TAXONOMY_VERSION`, `TOTALRECLAW_CLAIM_FORMAT`, and `TOTALRECLAW_DIGEST_MODE` were removed. Chain is auto-detected from billing tier (free = Base Sepolia, Pro = Gnosis). The MCP server silently ignores these vars for a transition period. See the [env vars reference](../docs/guides/env-vars-reference.md).

## Free Tier & Pricing

| Tier | Memories | Reads | Storage | Price |
|------|----------|-------|---------|-------|
| **Free** | 500/month | Unlimited | Testnet (trial) | $0 |
| **Pro** | Unlimited | Unlimited | Permanent on-chain (Gnosis) | See `totalreclaw_status` |

Pay with card via Stripe. Use `totalreclaw_status` to check current pricing. Counter resets monthly.

## Development

```bash
npm run build    # Build
npm test         # Run tests
npm run lint     # Lint
```

## Learn More

- [Client setup guide (v1)](../docs/guides/client-setup-v1.md) — one install command per client
- [Memory types guide](../docs/guides/memory-types-guide.md) — what gets stored and natural-language overrides
- [v1 migration guide](../docs/guides/v1-migration.md) — upgrading from v0
- [Environment variables](../docs/guides/env-vars-reference.md) — the 5 env vars that matter
- [Feature comparison](../docs/guides/feature-comparison.md) — what works on each client
- [IronClaw setup guide](../docs/guides/ironclaw-setup.md) — full walkthrough for IronClaw (NEAR AI) agents
- [totalreclaw.xyz](https://totalreclaw.xyz)
- [Main repository](https://github.com/p-diogo/totalreclaw)

## License

MIT
