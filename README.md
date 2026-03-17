# TotalReclaw

**Your AI agent knows more about you than your closest friends. You should be the only one who controls that.**

TotalReclaw is a zero-knowledge encrypted memory service for personal AI agents. Your memories are encrypted on your device before they leave — no one can read them, not even us. You hold the only keys.

- **Zero-knowledge** — E2E encrypted with AES-256-GCM. Only you can read your data.
- **Portable** — One 12-word recovery phrase. Any device, any agent, no lock-in.
- **Decentralized storage** — Encrypted data stored on Gnosis Chain, indexed by [The Graph](https://thegraph.com). No single server holds your memories.

---

## What is TotalReclaw?

Your AI agent remembers things across conversations — preferences, decisions, facts about your projects and life. That data is more personal than your search history. It shouldn't live on someone else's servers in plaintext.

TotalReclaw encrypts everything on your device and stores encrypted blobs on a decentralized network. The managed service at [api.totalreclaw.xyz](https://api.totalreclaw.xyz) facilitates on-chain storage, gas sponsorship, billing, and query routing — without ever seeing your data. If you prefer full control, you can [self-host](#self-hosting) the open-source server and store encrypted memories in your own PostgreSQL database instead.

---

## Quick Start

### Option A: OpenClaw (recommended — fully automatic)

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

Or just ask your agent:

> "Install the totalreclaw plugin"

The agent handles setup: generates your encryption keys and registers you. You'll be asked to write down a 12-word recovery phrase — that's the only thing you need to keep safe.

After setup, memory is automatic. Your agent remembers important things from conversations and loads relevant memories at the start of each new one. No commands needed.

### Option B: Claude Desktop / Cursor / Any MCP Agent

1. Run the setup wizard:
   ```bash
   npx @totalreclaw/mcp-server setup
   ```
   The wizard generates your recovery phrase, sets up encryption, registers you, and prints a config snippet.

2. Paste the config snippet into your MCP client (the wizard tells you exactly where).

3. Start chatting. Your agent has memory tools it can use when appropriate.

MCP agents use explicit tool calls rather than automatic hooks — the agent decides when to remember and recall, and you can also ask it directly ("remember that I prefer dark mode" or "what do you remember about my project?").

---

## How It Works

1. **You set up with a 12-word recovery phrase** (like a crypto wallet)
2. **All memories are encrypted on your device** before leaving
3. **Encrypted data is stored on a decentralized network** (Gnosis Chain + The Graph) — no single server holds your memories
4. **The managed service handles infrastructure** — gas sponsorship, query routing, billing — without seeing plaintext
5. **Same phrase on any device = same keys = same memories**

---

## Why TotalReclaw?

Other AI memory solutions exist — [Mem0](https://mem0.ai), [Zep](https://getzep.com), and others. They work well, but they read your data. Your memories, preferences, and personal context live on their servers in plaintext.

TotalReclaw is different: your data is encrypted before it leaves your device. The service facilitates storage and retrieval without ever seeing your memories. One 12-word phrase gives you access from any device, any agent, with no vendor lock-in.

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
|                    MANAGED SERVICE (infrastructure)                      |
+-------------------------------------------------------------------------+
|  Facilitates: on-chain writes (Pimlico gas sponsorship),                |
|  subgraph queries (The Graph), billing, registration.                   |
|  Never sees plaintext.                                                  |
+-------------------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------------------+
|                   DECENTRALIZED STORAGE (Gnosis Chain)                  |
+-------------------------------------------------------------------------+
|  Encrypted ciphertext + blind indices stored on-chain.                  |
|  Indexed by The Graph for fast retrieval.                               |
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

**Managed service:** Handles on-chain writes via Pimlico (ERC-4337), proxies queries to The Graph, manages billing and registration. Never sees plaintext.

**Decentralized storage:** Gnosis Chain for data anchoring. The Graph subgraph for indexed retrieval.

**Search:** Encrypted candidates are retrieved via blind index matching. The client decrypts them locally and re-ranks using BM25 + cosine similarity with reciprocal rank fusion (RRF).

**Dedup:** Two complementary layers prevent duplicate memories — cosine similarity catches paraphrases ("prefers dark mode" ~ "likes dark themes"), while LLM-guided classification catches contradictions ("prefers dark mode" → "switched to light mode"). See [memory-dedup.md](./docs/guides/memory-dedup.md) for the full architecture.

---

## Self-Hosting

If you prefer full control over your data infrastructure, you can self-host the open-source server. Encrypted memories are stored in your own PostgreSQL database instead of the decentralized network. No dependency on totalreclaw.xyz — unlimited usage, no billing.

```bash
cd server
cp .env.example .env   # Configure your database URL, secrets, etc.
docker-compose up -d
# Server runs at http://localhost:8080
```

Then configure your client:

```bash
export TOTALRECLAW_SERVER_URL=http://localhost:8080
export TOTALRECLAW_SELF_HOSTED=true
```

Both approaches encrypt your data identically on your device — the difference is where the encrypted blobs are stored. The managed service gives you decentralized storage with no infrastructure to manage. Self-hosting gives you full control and lower latency.

---

## Repository Structure

```
totalreclaw/
├── client/          # TypeScript client library (E2EE, LSH, embeddings, reranking)
├── skill/           # OpenClaw plugin (automatic memory for OpenClaw agents)
├── skill-nanoclaw/  # NanoClaw skill package + MCP bridge
├── mcp/             # MCP server for Claude Desktop, Cursor, Windsurf, etc.
├── server/          # Self-hosted server (FastAPI + PostgreSQL)
├── contracts/       # Solidity smart contracts (EventfulDataEdge)
├── subgraph/        # The Graph subgraph (AssemblyScript mappings)
├── database/        # Database schema for self-hosted server
├── tests/           # Integration and E2E tests
└── docs/            # Specs, guides, and deployment docs
```

**OpenClaw users:** The plugin you install lives in a [separate repo](https://github.com/p-diogo/totalreclaw-plugin). This repo contains the full open-source stack.

---

## Documentation

- [Beta Tester Guide](./docs/guides/beta-tester-guide.md) — getting started, troubleshooting, known limitations
- [Detailed Technical Guide](./docs/guides/beta-tester-guide-detailed.md) — full reference with configuration and environment variables
- [Architecture Spec](./docs/specs/totalreclaw/architecture.md) — E2EE design with LSH + blind buckets
- [totalreclaw.xyz](https://totalreclaw.xyz) — project homepage

---

## npm Packages

| Package | Description |
|---------|-------------|
| [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) | OpenClaw plugin — automatic encrypted memory |
| [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) | MCP server for Claude Desktop, Cursor, etc. |
| [@totalreclaw/client](https://www.npmjs.com/package/@totalreclaw/client) | Client library (used internally by plugin and MCP server) |

---

## Contributing

Coming soon. For now, please [file issues on GitHub](https://github.com/p-diogo/totalreclaw/issues).

---

## License

This project is dual-licensed:

- **Self-hosted server** (`server/`) — [AGPL-3.0](./server/LICENSE)
- **All other code** (client, plugins, MCP, contracts, subgraph) — [MIT](./LICENSE)
