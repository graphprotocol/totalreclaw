# ZeroClaw Setup Guide

Set up TotalReclaw as the encrypted memory backend for your ZeroClaw agent. All memories are encrypted on-device before they leave — ZeroClaw handles the agent logic, TotalReclaw handles the encrypted storage.

## Prerequisites

- **ZeroClaw** installed and working
- **Rust 1.87+** (for building the crate)
- Internet connection (for the relay and subgraph)
- ~600 MB disk space if using local embeddings (one-time download)

## 1. Add the Dependency

Add `totalreclaw-memory` to your ZeroClaw build:

```toml
# In Cargo.toml
[dependencies]
totalreclaw-memory = "0.1"

# Optional: local ONNX embeddings (~700MB RAM)
# totalreclaw-memory = { version = "0.1", features = ["local-embeddings"] }
```

## 2. Configure ZeroClaw

Set the memory backend in `~/.zeroclaw/config.toml`:

```toml
[memory]
backend = "totalreclaw"

[memory.totalreclaw]
recovery_phrase_path = "~/.totalreclaw/credentials.json"
embedding_config_path = "~/.totalreclaw/embedding-config.json"
```

## 3. First Run — Setup Wizard

On first use, TotalReclaw will guide you through:

**Recovery phrase** — generates a new 12-word BIP-39 phrase (or import an existing one). This phrase derives all your encryption keys. Same phrase = same memories across ZeroClaw, OpenClaw, Claude Desktop, and Hermes Agent.

**Embedding setup** — choose how embeddings are computed:

| Option | Privacy | Requirements |
|--------|---------|-------------|
| **Local ONNX** (recommended) | Maximum — nothing leaves your machine | ~600MB download, ~700MB RAM |
| **Ollama** | Local — privacy-preserving | Running Ollama with an embedding model |
| **ZeroClaw's provider** | Depends on provider | Your configured `embedding_provider` |
| **LLM provider** | Remote — provider sees text | API key for embedding endpoint |

Your memories are always E2E encrypted at rest. The embedding choice only affects where the embedding vector is computed — the plaintext is never sent to TotalReclaw's servers.

## How It Works

```
Your text → AES-256-GCM encrypt → Blind indices (SHA-256) + LSH buckets
  → Protobuf encode → On-chain via relay → The Graph subgraph
```

On recall:
```
Query → Hot cache check (cosine >= 0.85 → instant return)
  → Blind index trapdoors → Subgraph search → Decrypt candidates
  → BM25 + Cosine + RRF reranking → Top results → Cache result
```

The relay never sees your plaintext. The subgraph stores only encrypted blobs and blind hashes.

## What You Get Automatically

Because TotalReclaw implements ZeroClaw's `Memory` trait, you get these features for free — no hooks needed:

- **Auto-save** — ZeroClaw's consolidation calls `store()` automatically
- **Auto-recall** — ZeroClaw calls `recall()` at conversation start
- **Decay** — Core memories persist forever; episodic/context memories fade naturally (7-day half-life, handled by ZeroClaw)
- **Conflict resolution** — ZeroClaw checks semantic similarity before storing duplicates
- **Cross-channel persistence** — memories work across all 25+ ZeroClaw channels
- **Hot cache** — recent query results are cached in-memory. If a new query is semantically similar (cosine >= 0.85) to one answered recently, cached results are returned instantly without hitting the subgraph. Holds up to 30 entries per session. Automatically cleared after storing new facts to prevent stale results.

### Category Mapping

| Memory Type | ZeroClaw Category | Decay |
|-------------|-------------------|-------|
| fact | Core | None |
| preference | Core | None |
| decision | Core | None |
| goal | Core | None |
| summary | Core | None |
| episodic | Conversation | 7-day half-life |
| context | Daily | 7-day half-life |

## Portability

The same recovery phrase works across all TotalReclaw-compatible agents:

- **ZeroClaw** — native Rust backend (this guide)
- **OpenClaw** — plugin with auto-extract hooks
- **Claude Desktop** — via MCP server
- **Hermes Agent** — Python plugin
- **IronClaw** — via MCP server

Switch agents, keep your memories. No export/import needed.

## Pricing

Check [totalreclaw.xyz/pricing](https://totalreclaw.xyz/pricing) for current pricing.

- **Free tier** — Base Sepolia testnet, limited writes
- **Pro tier** — Gnosis mainnet, unlimited

## Troubleshooting

**Ollama not running** — If using Ollama embeddings, ensure `ollama serve` is running and you've pulled an embedding model (`ollama pull nomic-embed-text`).

**Slow first recall** — If using local embeddings, the ~600MB model downloads on first use. Subsequent calls use the cached model.

**Recall misses recent facts** — After storing, facts take 5-40 seconds to appear in the subgraph. This is inherent to on-chain storage. ZeroClaw's auto-save writes in the background, so you rarely notice.

**No internet** — The relay and subgraph require internet. If offline, `store()` and `recall()` will fail. Consider SQLite as a fallback backend for offline use.
