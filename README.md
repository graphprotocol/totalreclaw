<p align="center">
  <img src="docs/assets/logo.png" alt="TotalReclaw" width="120" />
</p>

<h1 align="center">TotalReclaw</h1>

<p align="center">
  <strong>End-to-end encrypted memory + knowledge graph for AI agents — portable, yours forever</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> ·
  <a href="https://clawhub.ai/skills/totalreclaw">ClawHub</a> ·
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw">npm</a> ·
  <a href="docs/guides/client-setup-v1.md">Getting Started</a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/mcp-server"><img src="https://img.shields.io/npm/v/@totalreclaw/mcp-server?label=MCP%20Server&color=7B5CFF" alt="npm MCP Server" /></a>
  <a href="https://www.npmjs.com/package/@totalreclaw/totalreclaw"><img src="https://img.shields.io/npm/v/@totalreclaw/totalreclaw?label=OpenClaw%20Plugin&color=7B5CFF" alt="npm Plugin" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License" /></a>
</p>

---

Your AI remembers everything — without remembering it for Big Tech. And starting with v1, every memory is structured: 6 speech-act types, provenance, scope, and volatility on every entry.

- **Private** — XChaCha20-Poly1305 encryption happens on-device. The server never sees plaintext, even if fully compromised.
- **Structured** — v1 taxonomy: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`, each with source / scope / volatility. [Learn more →](docs/guides/memory-types-guide.md)
- **Portable** — one 12-word recovery phrase works across every supported agent. Switch platforms without losing a single memory. [Compare integrations →](docs/guides/feature-comparison.md)
- **Yours forever** — memories are anchored to Gnosis Chain and indexed by The Graph. No vendor lock-in, no data hostage.

Other AI memory tools ([Mem0](https://mem0.ai), [Zep](https://getzep.com)) store your data in plaintext on their servers. TotalReclaw can't read your data by design — that's the difference between a privacy promise and a structural guarantee.

> **New in v1 (April 2026):** memory taxonomy, source-weighted reranking, 3 new MCP tools (`totalreclaw_pin` / `totalreclaw_retype` / `totalreclaw_set_scope`), protobuf v4 wire format. v1 is the default on every client. Existing vaults decrypt transparently. See the [v1 migration guide](docs/guides/v1-migration.md).

## Quick Start

One command per client. v1 is the default — no env toggles, no feature flags.

| Client | Install |
|---|---|
| **OpenClaw** | `openclaw skills install totalreclaw` |
| **Claude Desktop / Cursor / Windsurf** | `npx @totalreclaw/mcp-server setup` |
| **NanoClaw** | add `TOTALRECLAW_RECOVERY_PHRASE` to deployment env |
| **Python / Hermes** | `pip install totalreclaw` |
| **Rust / ZeroClaw** | `cargo add totalreclaw-memory` |
| **IronClaw** | via MCP server — see [IronClaw setup](docs/guides/ironclaw-setup.md) |

Then set one env var on your host:

```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

If you don't have a phrase yet, the setup wizard generates one. See the [client setup guide](docs/guides/client-setup-v1.md) for per-platform details.

## How It Works

1. **Your agent extracts facts** from conversations — typed by speech act (claim, preference, directive, commitment, episode, summary) and tagged with source, scope, volatility.
2. **Everything is encrypted on-device** — keys derived from your recovery phrase, never sent anywhere.
3. **Encrypted data is stored on-chain** — the server only sees ciphertext and hashed search tokens.
4. **Search works over encrypted data** — blind indices and LSH let the server find relevant memories without reading them.
5. **Results are decrypted and ranked locally** — BM25 + cosine + RRF with source-weighted reranking (v1 Tier 1).

> Full technical deep dives: [Architecture](docs/architecture.md), [Memory Taxonomy v1](docs/specs/totalreclaw/memory-taxonomy-v1.md), [Tiered Retrieval](docs/specs/totalreclaw/tiered-retrieval.md).

## What's new in v1

v1 shipped across every client in April 2026. Highlights:

- **6-type speech-act taxonomy** — `claim / preference / directive / commitment / episode / summary`. Replaces the ambiguous 8-type v0 list.
- **3 orthogonal axes** — `source` (user / user-inferred / assistant / external / derived), `scope` (work / personal / health / family / creative / finance / misc / unspecified), `volatility` (stable / updatable / ephemeral).
- **Source-weighted reranking** — user-authored claims consistently rank above assistant-regurgitated noise. Structurally fixes the [97.8% junk problem](https://github.com/mem0ai/mem0/issues/4573) documented in other memory systems.
- **3 new MCP tools** — `totalreclaw_pin`, `totalreclaw_retype`, `totalreclaw_set_scope`. Invoked by natural language ("pin that", "file that under work").
- **Protobuf v4 wire format** — outer wrapper bump; subgraph schema unchanged.
- **Env var cleanup** — 6 experimental env vars removed. 5 user-facing env vars remain. See the [env vars reference](docs/guides/env-vars-reference.md).

> Benchmark headline: *(placeholder — filled in once phase 2 500-conv run lands 2026-04-18)* — v1's source-weighted reranker preserves recall quality while breaking the importance-clustering pathology.

Full details: [v1 migration guide](docs/guides/v1-migration.md) and [memory types guide](docs/guides/memory-types-guide.md).

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

### Start here

- [Client setup — v1](docs/guides/client-setup-v1.md) — one install command per client.
- [Memory types guide](docs/guides/memory-types-guide.md) — what gets stored and how to override it via natural language.
- [v1 migration guide](docs/guides/v1-migration.md) — if you're upgrading from v0.
- [Environment variables](docs/guides/env-vars-reference.md) — the 5 env vars that matter.
- [Feature comparison](docs/guides/feature-comparison.md) — which features work on which client.

### Per-client setup

- [OpenClaw](docs/guides/openclaw-setup.md)
- [Claude Desktop / Cursor / Windsurf](docs/guides/claude-code-setup.md)
- [NanoClaw](docs/guides/nanoclaw-getting-started.md)
- [Hermes (Python)](docs/guides/hermes-setup.md)
- [IronClaw (NEAR AI)](docs/guides/ironclaw-setup.md)
- [ZeroClaw (Rust)](docs/guides/zeroclaw-setup.md)

### Specs + architecture

- [Architecture](docs/architecture.md) — encryption, LSH, search, deduplication, network layer.
- [Memory Taxonomy v1](docs/specs/totalreclaw/memory-taxonomy-v1.md) — normative cross-client spec.
- [Retrieval v2](docs/specs/totalreclaw/retrieval-v2.md) — source-weighted reranking + future tiers.
- [Tiered Retrieval](docs/specs/totalreclaw/tiered-retrieval.md) — implementation-focused retrieval deep dive.
- [Client Consistency](docs/specs/totalreclaw/client-consistency.md) — cross-client contracts.

### Other

- [Importing memories](docs/guides/importing-memories.md) — ChatGPT, Claude, Gemini, Mem0.
- [totalreclaw.xyz](https://totalreclaw.xyz) — project homepage.

## License

- **Self-hosted server** (`server/`) — [AGPL-3.0](server/LICENSE)
- **All other code** — [MIT](LICENSE)
