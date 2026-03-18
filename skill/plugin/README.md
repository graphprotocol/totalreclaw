# @totalreclaw/totalreclaw

Encrypted memory for your AI agent — zero-knowledge E2EE vault with automatic extraction, semantic search, and portable storage.

Built for [OpenClaw](https://openclaw.ai). Your memories are encrypted on your device before leaving — no one can read them, not even us.

**[totalreclaw.xyz](https://totalreclaw.xyz)**

## Install

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

Or just ask your agent:

> "Install the totalreclaw plugin"

The agent handles setup: generates your encryption keys and registers you. You'll be asked to write down a 12-word recovery phrase — that's the only thing you need to keep safe.

## How It Works

After setup, memory is **fully automatic**:

- **Start of conversation** — loads relevant memories from your vault
- **End of conversation** — extracts and encrypts new facts before storing them
- **Before context compaction** — saves everything important before the context window is trimmed

All encryption happens client-side using AES-256-GCM. Search uses blind indices (SHA-256 hashes) — the server never sees your queries or data. Your 12-word recovery phrase derives all keys via Argon2id + HKDF.

## Tools

Your agent gets these tools automatically:

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Manually store a fact |
| `totalreclaw_recall` | Search memories by natural language |
| `totalreclaw_forget` | Delete a specific memory |
| `totalreclaw_export` | Export all memories as plaintext |
| `totalreclaw_status` | Check billing status and quota |

Most of the time you won't use these directly — the automatic hooks handle memory for you.

## Features

- **Zero-knowledge E2EE** — AES-256-GCM encryption, blind index search, HKDF auth
- **Semantic search** — Local embeddings (bge-small-en-v1.5) + BM25 + cosine reranking with RRF
- **Automatic extraction** — LLM extracts facts from conversations, no manual input needed
- **Dedup** — Cosine similarity catches paraphrases; LLM-guided dedup catches contradictions (Pro)
- **On-chain storage** — Encrypted data stored on Gnosis Chain, indexed by The Graph
- **Portable** — One 12-word phrase. Any device, same memories, no lock-in
- **Import** — Migrate from Mem0 or MCP Memory Server

## Free Tier & Pricing

| Tier | Writes | Reads | Price |
|------|--------|-------|-------|
| **Free** | 250/month | Unlimited | $0 |
| **Pro** | 10,000/month | Unlimited | $2-5/month |

Pay with card (Stripe) or crypto (Coinbase Commerce). Counter resets monthly.

## Configuration

Set these environment variables before the agent starts:

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_SERVER_URL` | Server URL | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_CREDENTIALS_PATH` | Path to credentials file | `~/.totalreclaw/credentials.json` |
| `TOTALRECLAW_SELF_HOSTED` | Set to `true` to use your own self-hosted server instead of the managed service | `false` (managed service) |
| `TOTALRECLAW_EXTRACT_EVERY_TURNS` | Auto-extract interval (turns) | `5` (Free) / `2` (Pro min) |

## Using with Other Agents

TotalReclaw also works outside OpenClaw:

- **Claude Desktop / Cursor / Windsurf** — Use [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server)
- **NanoClaw** — Lightweight skill with MCP bridge

Same encryption, same recovery phrase, same memories across all agents.

## License

MIT
