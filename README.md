<p align="center">
  <img src="docs/assets/logo.png" alt="TotalReclaw" width="120" />
</p>

<h1 align="center">TotalReclaw</h1>

<p align="center">
  <strong>End-to-end encrypted memory for AI agents — portable, yours forever</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> ·
  <a href="https://clawhub.ai/skills/totalreclaw">ClawHub</a> ·
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw">npm</a> ·
  <a href="docs/guides/openclaw-setup.md">Getting Started</a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?label=MCP%20Server&color=7B5CFF" alt="npm MCP Server" /></a>
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw"><img src="https://img.shields.io/npm/v/@totalreclaw/totalreclaw?label=OpenClaw%20Plugin&color=7B5CFF" alt="npm Plugin" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License" /></a>
</p>

---

Your AI remembers everything — without remembering it for Big Tech.

- **Private** — XChaCha20-Poly1305 encryption happens on-device. The server never sees plaintext, even if fully compromised.
- **Portable** — one 12-word recovery phrase works across every supported agent. Switch platforms without losing a single memory. [Compare integrations →](docs/guides/feature-comparison.md)
- **Yours forever** — memories are anchored to Gnosis Chain and indexed by The Graph. No vendor lock-in, no data hostage.

Other AI memory tools ([Mem0](https://mem0.ai), [Zep](https://getzep.com)) store your data in plaintext on their servers. TotalReclaw can't read your data by design — that's the difference between a privacy promise and a structural guarantee.

## Quick Start

### OpenClaw

Tell your agent: *"Install the TotalReclaw skill from ClawHub"*

Or via terminal:

```bash
openclaw skills install totalreclaw
```

The agent handles everything: generates encryption keys, registers, and sets up automatic memory. See the [OpenClaw setup guide](docs/guides/openclaw-setup.md).

### Claude Desktop / Cursor / Windsurf

```bash
npx @totalreclaw/mcp-server setup
```

The wizard generates your recovery phrase, registers you, and prints a config snippet. See the [MCP setup guide](docs/guides/claude-code-setup.md).

### NanoClaw / IronClaw / Hermes

Each platform has its own setup guide:

- **[NanoClaw](docs/guides/nanoclaw-getting-started.md)** — automatic memory via agent-runner MCP bridge
- **[IronClaw](docs/guides/ironclaw-setup.md)** — NEAR AI TEE + TotalReclaw E2EE
- **[Hermes](docs/guides/hermes-setup.md)** — Python agent plugin

## How It Works

1. **Your agent extracts facts** from conversations — preferences, decisions, context
2. **Everything is encrypted on-device** — keys derived from your recovery phrase, never sent anywhere
3. **Encrypted data is stored on-chain** — the server only sees ciphertext and hashed search tokens
4. **Search works over encrypted data** — blind indices and LSH let the server find relevant memories without reading them
5. **Results are decrypted and ranked locally** — BM25 + cosine similarity + importance scoring

> Full technical deep dive: [Architecture](docs/architecture.md)

## Import from Other AI Tools

Already using another AI assistant with memory? Tell your agent:

- *"Import my ChatGPT memories into TotalReclaw"*
- *"Import my Claude memories into TotalReclaw"*
- *"Import my Gemini history into TotalReclaw"*

Supported sources: **ChatGPT** (memories or conversations.json), **Claude** (memory export), **Gemini** (Google Takeout), **Mem0** (API or file), **MCP Memory Server** (JSONL). All imports are encrypted client-side before storage. See the [import guide](docs/guides/importing-memories.md).

## Packages

| Package | Description | Install |
| --- | --- | --- |
| [@totalreclaw/totalreclaw](https://clawhub.ai/skills/totalreclaw) | OpenClaw skill — automatic encrypted memory | `openclaw skills install totalreclaw` |
| [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) | MCP server for Claude Desktop and other MCP clients | `npx @totalreclaw/mcp-server setup` |
| [@totalreclaw/client](https://www.npmjs.com/package/@totalreclaw/client) | Client library (encryption, indexing, search, re-ranking) | `npm install @totalreclaw/client` |
| [totalreclaw](https://pypi.org/project/totalreclaw/) | Python client + Hermes Agent plugin | `pip install totalreclaw` |

## Pricing

Free tier is unlimited on testnet. Upgrade to Pro for permanent on-chain storage.

**[See pricing →](https://totalreclaw.xyz/pricing)**

## Self-Hosting

Run the open-source server with PostgreSQL — no dependency on totalreclaw.xyz:

```bash
cd server && cp .env.example .env && docker-compose up -d
```

Set `TOTALRECLAW_SELF_HOSTED=true` and `TOTALRECLAW_SERVER_URL=http://localhost:8080` on the client. Both modes encrypt identically — the difference is where the encrypted blobs are stored.

## Repository Structure

```
totalreclaw/
├── client/          TypeScript client library (encryption, indexing, search, re-ranking)
├── rust/            Rust core (XChaCha20-Poly1305, LSH, reranker) — compiles to WASM + PyO3
├── python/          Python client library + Hermes Agent plugin
├── skill/           OpenClaw plugin (automatic memory via lifecycle hooks)
├── skill-nanoclaw/  NanoClaw skill package + MCP bridge
├── mcp/             MCP server for Claude Desktop and other MCP clients
├── server/          Self-hosted server (FastAPI + PostgreSQL)
├── contracts/       Smart contracts (Gnosis Chain)
├── subgraph/        The Graph subgraph (AssemblyScript indexer)
├── docs/            Architecture, specs, and guides
└── tests/           Integration, parity, and E2E tests
```

## Documentation

- [OpenClaw Setup](docs/guides/openclaw-setup.md) — install, configure, start using memory
- [Claude Desktop / Cursor / Windsurf](docs/guides/claude-code-setup.md) — MCP server setup
- [Feature Comparison](docs/guides/feature-comparison.md) — what works on each platform
- [Architecture](docs/architecture.md) — encryption, LSH, search, deduplication, network layer
- [Importing Memories](docs/guides/importing-memories.md) — ChatGPT, Claude, Gemini, Mem0
- [totalreclaw.xyz](https://totalreclaw.xyz) — project homepage

## License

- **Self-hosted server** (`server/`) — [AGPL-3.0](server/LICENSE)
- **All other code** — [MIT](LICENSE)
