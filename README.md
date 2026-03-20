<p align="center">
  <img src="docs/assets/logo.png" alt="TotalReclaw" width="120" />
</p>

<h1 align="center">TotalReclaw</h1>

<p align="center">
  <strong>End-to-end encrypted memory for AI agents — portable, yours forever</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> &middot;
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw">npm</a> &middot;
  <a href="./docs/guides/beta-tester-guide.md">Getting Started</a> &middot;
  <a href="./docs/specs/totalreclaw/architecture.md">Architecture</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?label=MCP%20Server&color=7B5CFF" alt="npm MCP Server"></a>
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw"><img src="https://img.shields.io/npm/v/@totalreclaw/totalreclaw?label=OpenClaw%20Plugin&color=7B5CFF" alt="npm Plugin"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

Your AI agent knows more about you than your closest friends. You should be the only one who controls that.

TotalReclaw encrypts every memory on your device before it leaves. The service facilitates storage and retrieval without ever seeing your data. One 12-word recovery phrase gives you access from any device, any agent, with no vendor lock-in.

- **End-to-end encrypted** -- AES-256-GCM. Only you can read your data.
- **Portable** -- One 12-word recovery phrase. Any device, any agent, no lock-in.
- **Your data, forever** -- Encrypted data stored on [Gnosis Chain](https://www.gnosis.io/), indexed by [The Graph](https://thegraph.com).

## Quick Start

### OpenClaw (recommended -- fully automatic)

Ask your agent:

> "Install the @totalreclaw/totalreclaw plugin"

The agent handles everything: generates your encryption keys, registers you, and sets up automatic memory. You'll be asked to write down a 12-word recovery phrase -- that's the only thing you need to keep safe.

After setup, memory is automatic. Your agent remembers important things from conversations and loads relevant memories at the start of each new one.

### Claude Desktop / Cursor / Windsurf

```bash
npx @totalreclaw/mcp-server setup
```

The setup wizard generates your recovery phrase, registers you, and prints a config snippet to paste into your MCP client. See the [@totalreclaw/mcp-server README](./mcp/README.md) for details.

## How It Works

```
  Your Device                    Managed Service                  Gnosis Chain
 ┌──────────────┐              ┌──────────────────┐            ┌──────────────┐
 │ Extract facts │              │ Gas sponsorship   │            │ Encrypted    │
 │ Encrypt (AES) │─────────────▸│ Query routing     │───────────▸│ ciphertext + │
 │ Generate LSH  │  ciphertext  │ Billing           │  on-chain  │ blind indices │
 │ Blind indices │  only        │ Never sees        │  storage   │ indexed by   │
 │ Search+rerank │◂─────────────│ plaintext         │◂───────────│ The Graph    │
 └──────────────┘              └──────────────────┘            └──────────────┘
```

1. **Facts extracted** from conversations by the LLM
2. **Encrypted on your device** with AES-256-GCM before leaving
3. **Stored on-chain** via Gnosis Chain with ERC-4337 gas sponsorship
4. **Retrieved via blind indices** -- the server never sees your queries
5. **Decrypted and re-ranked locally** using BM25 + cosine + RRF fusion

## Why TotalReclaw?

Other AI memory solutions exist -- [Mem0](https://mem0.ai), [Zep](https://getzep.com), and others. They work well, but they read your data. Your memories, preferences, and personal context live on their servers in plaintext.

TotalReclaw is different: your data is encrypted before it leaves your device. The service facilitates storage and retrieval without ever seeing your memories.

## Packages

| Package | Description | Install |
|---------|-------------|---------|
| [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) | OpenClaw plugin -- automatic encrypted memory | `openclaw plugins install @totalreclaw/totalreclaw` |
| [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) | MCP server for Claude Desktop, Cursor, Windsurf | `npx @totalreclaw/mcp-server setup` |
| [@totalreclaw/client](https://www.npmjs.com/package/@totalreclaw/client) | Client library (E2EE, LSH, embeddings, reranking) | `npm install @totalreclaw/client` |

## Free Tier & Pricing

| Tier | Writes | Reads | Price |
|------|--------|-------|-------|
| **Free** | 250/month | Unlimited | $0 |
| **Pro** | 10,000/month | Unlimited | $5/month |

Counter resets monthly. Pay with card via Stripe.

## Self-Hosting

Run the open-source server with your own PostgreSQL database. No dependency on totalreclaw.xyz.

```bash
cd server && cp .env.example .env && docker-compose up -d
```

Then set `TOTALRECLAW_SELF_HOSTED=true` and `TOTALRECLAW_SERVER_URL=http://localhost:8080` on your client.

Both approaches encrypt your data identically on your device -- the difference is where the encrypted blobs are stored.

## Repository Structure

```
totalreclaw/
├── client/          TypeScript client library (E2EE, LSH, embeddings, reranking)
├── skill/           OpenClaw plugin (automatic memory via lifecycle hooks)
├── skill-nanoclaw/  NanoClaw skill package + MCP bridge
├── mcp/             MCP server for Claude Desktop, Cursor, Windsurf
├── server/          Self-hosted server (FastAPI + PostgreSQL)
├── contracts/       Solidity smart contracts (EventfulDataEdge)
├── subgraph/        The Graph subgraph (AssemblyScript mappings)
└── docs/            Specs, guides, and deployment docs
```

## Documentation

- [Getting Started](./docs/guides/beta-tester-guide.md) -- setup, troubleshooting, known limitations
- [Detailed Technical Guide](./docs/guides/beta-tester-guide-detailed.md) -- full reference with configuration
- [Architecture Spec](./docs/specs/totalreclaw/architecture.md) -- E2EE design with LSH + blind buckets
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage

## License

- **Self-hosted server** (`server/`) -- [AGPL-3.0](./server/LICENSE)
- **All other code** -- [MIT](./LICENSE)
