<p align="center">
  <img src="docs/assets/logo.png" alt="TotalReclaw" width="120" />
</p>

<h1 align="center">TotalReclaw</h1>

<p align="center">
  <strong>End-to-end encrypted memory for AI agents — portable, yours forever</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> ·
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw">npm</a> ·
  <a href="docs/guides/beta-tester-guide.md">Getting Started</a> ·
  <a href="docs/architecture.md">Architecture Deep Dive</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?label=MCP%20Server&color=7B5CFF" alt="npm MCP Server" /></a>
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw"><img src="https://img.shields.io/npm/v/@totalreclaw/totalreclaw?label=OpenClaw%20Plugin&color=7B5CFF" alt="npm Plugin" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License" /></a>
</p>

---

Finally, an AI that remembers everything — without remembering it for Big Tech.

Every memory is encrypted on the device before it leaves. Storage and retrieval happen without any server ever seeing the data. One 12-word recovery phrase gives access from any device, any agent, with no lock-in.

- **Private** — memories are encrypted on the device before they leave. No server, service, or third party can read them — even if fully compromised.
- **Portable** — one recovery phrase works across OpenClaw, NanoClaw, Claude Desktop, or any MCP-compatible agent. Switch agents without losing a single memory.
- **Yours forever** — memories are anchored to Gnosis Chain and indexed by The Graph. Upgrade to Pro for permanent, verifiable on-chain storage.

## Quick Start

### 🦞 OpenClaw

Ask your agent:

> "Install the @totalreclaw/totalreclaw plugin"

The agent handles everything: generates encryption keys, registers, and sets up automatic memory.

### 🖥️ Claude Desktop / Cursor / Windsurf

```bash
npx @totalreclaw/mcp-server setup
```

The wizard generates your recovery phrase, registers you, and prints a config snippet. See the [@totalreclaw/mcp-server README](mcp/README.md) for details.

### 🤖 NanoClaw

NanoClaw agents get TotalReclaw memory automatically. Set `TOTALRECLAW_MASTER_PASSWORD` in your deployment config — the agent-runner spawns the MCP server as a background process. See the [NanoClaw README](skill-nanoclaw/README.md).

## How It Works

1. **Your agent extracts facts** from conversations (preferences, decisions, context)
2. **Everything is encrypted on your device** before leaving — AES-256-GCM, keys derived from your recovery phrase
3. **Encrypted data is stored on-chain** — the server only sees ciphertext and hashed search tokens
4. **Search works over encrypted data** — blind indices let the server find relevant memories without reading them
5. **Results are decrypted and ranked locally** — BM25 + semantic similarity + importance scoring

The server does useful work (narrowing candidates, sponsoring gas) but never sees your data — even if fully compromised.

> For the full technical deep dive — encryption pipeline, LSH parameters, re-ranking algorithm — see [Architecture](docs/architecture.md).

## Why TotalReclaw?

Other AI memory solutions exist — [Mem0](https://mem0.ai), [Zep](https://getzep.com), and others. They work well, but they read your data. Memories, preferences, and personal context live on their servers in plaintext.

TotalReclaw encrypts everything on the device. The relay service never sees plaintext. And because memories are anchored to an open global network, they survive even if TotalReclaw itself doesn't — that's the difference between a privacy promise and a structural guarantee.

## Packages

| Package | Description | Install |
| --- | --- | --- |
| [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) | OpenClaw plugin — automatic encrypted memory | `openclaw plugins install @totalreclaw/totalreclaw` |
| [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) | MCP server for Claude Desktop and other MCP clients | `npx @totalreclaw/mcp-server setup` |
| [@totalreclaw/client](https://www.npmjs.com/package/@totalreclaw/client) | Client library (encryption, indexing, search, re-ranking) | `npm install @totalreclaw/client` |

## Pricing

TotalReclaw has a generous free tier to get started. Upgrade to Pro for permanent on-chain storage.

**[See pricing →](https://totalreclaw.xyz/pricing)**

## Self-Hosting

Run the open-source server with a local PostgreSQL database — no dependency on totalreclaw.xyz.

```
cd server && cp .env.example .env && docker-compose up -d
```

Then set `TOTALRECLAW_SELF_HOSTED=true` and `TOTALRECLAW_SERVER_URL=http://localhost:8080` on the client.

Both approaches encrypt data identically on the device — the difference is where the encrypted blobs are stored.

## Repository Structure

```
totalreclaw/
├── client/          TypeScript client library (encryption, indexing, search, re-ranking)
├── skill/           OpenClaw plugin (automatic memory via lifecycle hooks)
├── skill-nanoclaw/  NanoClaw skill package + MCP bridge
├── mcp/             MCP server for Claude Desktop and other MCP clients
├── server/          Self-hosted server (FastAPI + PostgreSQL)
├── contracts/       Smart contracts (Gnosis Chain)
├── subgraph/        The Graph subgraph (AssemblyScript indexer)
├── docs/            Architecture spec, guides, and deployment docs
└── tests/           Integration and unit tests
```

## Documentation

- [Getting Started](docs/guides/beta-tester-guide.md) — setup, troubleshooting, known limitations
- [Detailed Technical Guide](docs/guides/beta-tester-guide-detailed.md) — full reference with configuration
- [Architecture Deep Dive](docs/architecture.md) — encryption, LSH, search, deduplication, network layer
- [totalreclaw.xyz](https://totalreclaw.xyz) — project homepage

## License

- **Self-hosted server** (`server/`) — [AGPL-3.0](server/LICENSE)
- **All other code** — [MIT](LICENSE)
