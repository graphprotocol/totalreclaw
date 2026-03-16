# TotalReclaw

**Your AI agent knows more about you than your closest friends. You should be the only one who controls that.**

TotalReclaw is a zero-knowledge encrypted memory layer for AI agents. Your data is encrypted on your device before it leaves — no one can read it, not even us. You hold the only keys.

- **Zero-knowledge** — E2E encrypted. Only you can read your data.
- **Portable** — One 12-word phrase. Any device, any agent, no lock-in.
- **Decentralized** — Optionally anchor your data on-chain (Gnosis Chain), indexed by [The Graph](https://thegraph.com). No vendor lock-in.

---

## What is TotalReclaw?

Your AI agent remembers things across conversations — preferences, decisions, facts about your projects and life. That data is more personal than your search history. It shouldn't live on someone else's servers in plaintext. TotalReclaw encrypts everything on your device, stores only encrypted blobs that no one else can read, and works across any agent.

---

## Quick Start

### Option A: OpenClaw (recommended -- fully automatic)

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

Or just ask your agent:

> "Install the totalreclaw plugin"

The agent handles setup: generates your encryption keys and registers you. You'll be asked to write down a 12-word recovery phrase -- that's the only thing you need to keep safe.

After setup, memory is automatic. Your agent remembers important things from conversations and loads relevant memories at the start of each new one. No commands needed.

**OpenClaw users:** See the [totalreclaw-plugin](https://github.com/p-diogo/totalreclaw-plugin) repo for the full plugin README, tool reference, and configuration docs.

### Option B: Claude Desktop / Cursor / Any MCP Agent

1. Run the setup wizard:
   ```bash
   npx @totalreclaw/mcp-server setup
   ```
   The wizard generates your recovery phrase, sets up encryption, registers you, and prints a config snippet.

2. Paste the config snippet into your MCP client (the wizard tells you exactly where).

3. Start chatting. Your agent has memory tools it can use when appropriate.

MCP agents use explicit tool calls rather than automatic hooks -- the agent decides when to remember and recall, and you can also ask it directly ("remember that I prefer dark mode" or "what do you remember about my project?").

---

## How It Works

1. **You set up with a 12-word recovery phrase** (like a crypto wallet)
2. **All memories are encrypted on your device** before reaching the server
3. **The server stores only encrypted blobs** + blind search indices -- it can never read your data
4. **On-chain mode** (default) anchors data on Gnosis Chain, indexed by [The Graph](https://thegraph.com)'s decentralized network — no single server controls your memories
5. **Same phrase on any device = same keys = same memories**

---

## Why TotalReclaw?

Other AI memory solutions exist — [Mem0](https://mem0.ai), [Zep](https://getzep.com), and others. They work well, but they read your data. Your memories, preferences, and personal context live on their servers in plaintext.

TotalReclaw is different: your data is encrypted before it leaves your device. The server stores only encrypted blobs and blind search indices — it can never read your memories. One 12-word phrase gives you access from any device, any agent, with no vendor lock-in.

---

## Free Tier & Pricing

| Tier | Writes | Reads | Key Features | Price |
|------|--------|-------|--------------|-------|
| **Free** | 250/month | Unlimited | Cosine dedup, auto-extract every 5 turns | $0 |
| **Pro** | 10,000/month | Unlimited | + LLM-guided dedup, faster extraction | $2-5/month |

Counter resets at the start of each calendar month. Pay with card (Stripe) or crypto (Coinbase Commerce). When you hit the limit, your agent tells you and provides an upgrade link.

> Pricing is not finalized during beta.

---

## Architecture

```
+-------------------------------------------------------------------------+
|                        CLIENT (your device)                             |
+-------------------------------------------------------------------------+
|  +--------------+  +--------------+  +--------------+  +-------------+ |
|  | Fact Extract |->|   Encrypt    |->| Generate LSH |->| Blind Index | |
|  |    (LLM)     |  | (AES-256-GCM)|  |   Buckets    |  |  (SHA-256)  | |
|  +--------------+  +--------------+  +--------------+  +-------------+ |
+-------------------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------------------+
|                     SERVER (encrypted storage)                          |
+-------------------------------------------------------------------------+
|  Stores: encrypted ciphertext, blind indices, encrypted embeddings     |
|  Search: blind trapdoors -> GIN index -> return encrypted candidates   |
|  Never sees plaintext.                                                  |
+-------------------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------------------+
|                   CLIENT (decryption + re-ranking)                      |
+-------------------------------------------------------------------------+
|  Decrypt candidates -> BM25 + cosine similarity + RRF fusion -> Top K  |
+-------------------------------------------------------------------------+
```

**Client-side:** AES-256-GCM encryption, locality-sensitive hashing (LSH) for blind indices, local embeddings (bge-small-en-v1.5, no API keys needed).

**Server:** FastAPI + PostgreSQL. Stores only encrypted data. Blind trapdoor search over GIN indices.

**On-chain (opt-in):** Gnosis Chain for data anchoring. The Graph subgraph for decentralized indexing.

**Search:** The server returns encrypted candidates matched by blind indices. The client decrypts them locally and re-ranks using BM25 + cosine similarity with reciprocal rank fusion (RRF).

**Dedup:** Two complementary layers prevent duplicate memories — cosine similarity catches paraphrases ("prefers dark mode" ~ "likes dark themes"), while LLM-guided classification catches contradictions ("prefers dark mode" -> "switched to light mode"). See [memory-dedup.md](./docs/guides/memory-dedup.md) for the full architecture.

---

## Storage Modes

By default, TotalReclaw uses **subgraph mode**: your encrypted data is anchored on-chain (Gnosis Chain) and indexed by [The Graph](https://thegraph.com)'s decentralized network. No single server controls your memories. Gas is sponsored — you pay nothing during beta.

If you prefer to self-host or want faster writes, you can switch to **server (HTTP) mode** by running your own backend:

```bash
cd server
cp .env.example .env   # Configure your database URL, secrets, etc.
docker-compose up -d
# Server runs at http://localhost:8080
```

Then configure your client:

```bash
export TOTALRECLAW_SERVER_URL=http://localhost:8080
export TOTALRECLAW_SUBGRAPH_MODE=false
```

Both modes encrypt your data identically on your device — the difference is where the encrypted blobs are stored. Subgraph mode gives you decentralized, tamper-proof storage with no infrastructure to manage. Server mode gives you full control and lower latency.

---

## Repository Structure

**OpenClaw users:** The plugin you install lives in a [separate repo](https://github.com/p-diogo/totalreclaw-plugin). This repo contains the full stack (server, client library, contracts, subgraph).

```
totalreclaw/
├── server/          # FastAPI + PostgreSQL backend
├── client/          # TypeScript client library (E2EE, LSH, embeddings)
├── skill/           # OpenClaw skill source (mirrored to totalreclaw-plugin)
├── skill-nanoclaw/  # NanoClaw skill package + MCP server
├── mcp/             # MCP server for Claude Desktop, Cursor, etc.
├── contracts/       # Solidity contracts (EventfulDataEdge, Paymaster)
├── subgraph/        # The Graph subgraph (AssemblyScript mappings)
├── database/        # Database schema
├── tests/           # Integration and E2E tests
└── docs/            # Specs, guides, and deployment docs
```

---

## Documentation

- [Beta Tester Guide](./docs/guides/beta-tester-guide.md) -- getting started, troubleshooting, known limitations
- [Detailed Technical Guide](./docs/guides/beta-tester-guide-detailed.md) -- full reference with configuration and environment variables
- [Architecture Spec](./docs/specs/totalreclaw/architecture.md) -- E2EE design with LSH + blind buckets
- [Crypto Modules](./docs/architecture/crypto-modules.md) -- key derivation, encryption, and hashing internals
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage

---

## npm Packages

| Package | Description |
|---------|-------------|
| [@totalreclaw/client](https://www.npmjs.com/package/@totalreclaw/client) | TypeScript client library (E2EE, LSH, embeddings) |
| [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) | MCP server for Claude Desktop, Cursor, etc. |

---

## Contributing

Coming soon. For now, please [file issues on GitHub](https://github.com/p-diogo/totalreclaw/issues).

---

## License

This project is dual-licensed:

- **Server** (`server/`) -- [AGPL-3.0](./server/LICENSE)
- **All other code** -- [MIT](./LICENSE)
