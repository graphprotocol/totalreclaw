<p align="center">
  <img src="../docs/assets/logo.png" alt="TotalReclaw" width="80" />
</p>

<h1 align="center">@totalreclaw/client</h1>

<p align="center">
  <strong>TypeScript client library for TotalReclaw -- E2EE, LSH blind indexing, embeddings, and reranking</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> &middot;
  <a href="https://www.npmjs.com/package/@totalreclaw/client">npm</a> &middot;
  <a href="https://github.com/p-diogo/totalreclaw">Repository</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/client"><img src="https://img.shields.io/npm/v/@totalreclaw/client?color=7B5CFF" alt="npm version"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

Build encrypted memory into any AI agent. All encryption happens client-side -- the server never sees plaintext.

This is the core cryptographic library used by [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) (OpenClaw plugin) and [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server). Most users should install one of those packages instead.

**Requirements:** Node.js 18+

## Installation

```bash
npm install @totalreclaw/client
```

## Features

- **AES-256-GCM encryption** -- All memories encrypted client-side
- **Blind index search** -- LSH-based blind indices for searching encrypted data
- **Local embeddings** -- Qwen3-Embedding-0.6B for semantic similarity (no API keys, 100+ languages)
- **BM25 + cosine reranking** -- Reciprocal rank fusion for high-quality retrieval
- **Smart Account derivation** -- BIP-39 mnemonic to ERC-4337 Smart Account address
- **Memory decay** -- Importance-based lifecycle management

## Quick Start

```typescript
import { TotalReclaw } from '@totalreclaw/client';

const client = new TotalReclaw({ serverUrl: 'https://api.totalreclaw.xyz' });
await client.init();

const userId = await client.register('your twelve word recovery phrase');

await client.remember('I prefer coffee over tea in the morning');

const results = await client.recall('what do I like to drink?');
```

## Crypto Primitives

| Operation | Algorithm | Purpose |
|-----------|-----------|---------|
| Key derivation | Argon2id + HKDF-SHA256 | Memory-hard password hashing + auth key derivation |
| Encryption | AES-256-GCM | Authenticated encryption of memories and embeddings |
| Blind indices | SHA-256 | Searchable encryption without exposing plaintext |
| LSH | Random hyperplane | Approximate nearest neighbor search on encrypted data |
| Embeddings | Qwen3-Embedding-0.6B (1024d) | Local semantic similarity (no API calls, 100+ languages) |
| Reranking | BM25 + cosine + RRF | Multi-signal result fusion |

## Development

```bash
npm install     # Install dependencies
npm test        # Run tests (217 tests)
npm run build   # Build
```

## Learn More

- [Architecture Spec](../docs/specs/totalreclaw/architecture.md) -- E2EE design with LSH + blind buckets
- [totalreclaw.xyz](https://totalreclaw.xyz)
- [Main Repository](https://github.com/p-diogo/totalreclaw)

## License

MIT
