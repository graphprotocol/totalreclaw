<p align="center">
  <img src="https://raw.githubusercontent.com/graphprotocol/totalreclaw/main/docs/assets/logo.png" alt="TotalReclaw" width="80" />
</p>

<h1 align="center">@totalreclaw/client</h1>

<p align="center">
  <strong>TypeScript client library for TotalReclaw -- E2EE, LSH blind indexing, embeddings, and reranking</strong>
</p>

<p align="center">
  <a href="https://totalreclaw.xyz">Website</a> &middot;
  <a href="https://www.npmjs.com/package/@totalreclaw/client">npm</a> &middot;
  <a href="https://github.com/graphprotocol/totalreclaw">Repository</a>
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/@totalreclaw/client"><img src="https://img.shields.io/npm/v/@totalreclaw/client?color=7B5CFF" alt="npm version"></a>
  <a href="https://github.com/graphprotocol/totalreclaw/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

---

Build encrypted memory into any AI agent. All encryption happens client-side -- the server never sees plaintext.

This is the TypeScript library used by [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) (OpenClaw plugin), [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) and [@totalreclaw/skill-nanoclaw](https://www.npmjs.com/package/@totalreclaw/skill-nanoclaw). **Most users should install an agent integration instead:** [@totalreclaw/totalreclaw](https://www.npmjs.com/package/@totalreclaw/totalreclaw) for OpenClaw, [@totalreclaw/mcp-server](https://www.npmjs.com/package/@totalreclaw/mcp-server) for Claude Code, Claude Desktop, Cursor and other MCP hosts, or [`totalreclaw`](https://pypi.org/project/totalreclaw/) on PyPI for Hermes.

**Requirements:** Node.js 18+

### Architecture

All cryptographic operations (XChaCha20-Poly1305, HKDF key derivation, LSH hashing, blind indices, content fingerprinting) are powered by [`@totalreclaw/core`](https://www.npmjs.com/package/@totalreclaw/core) -- a unified Rust/WASM module that ensures byte-for-byte consistency across all TotalReclaw clients.

## Installation

```bash
npm install @totalreclaw/client
```

## Features

- **XChaCha20-Poly1305 encryption** -- All memories encrypted client-side
- **Blind index search** -- LSH-based blind indices for searching encrypted data
- **Local embeddings** -- Harrier-OSS-v1-270M for semantic similarity (no API keys)
- **BM25 + cosine reranking** -- Reciprocal rank fusion for high-quality retrieval
- **Memory decay** -- Importance-based lifecycle management

## Quick Start

The `TotalReclaw` class speaks the HTTP storage protocol (`/v1/register`, `/v1/store`, `/v1/search`) of a [self-hosted TotalReclaw server](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/self-hosted-deployment.md). The managed service at `api.totalreclaw.xyz` uses a different, on-chain protocol; to use it, install one of the agent integrations above.

```typescript
import { TotalReclaw } from '@totalreclaw/client';

const client = new TotalReclaw({ serverUrl: 'http://127.0.0.1:8080' }); // your server
await client.init(); // loads the local embedding model

// masterPassword: a secret you load from your own secret store. Never hard-code it.
const userId = await client.register(masterPassword);
const salt = client.getSalt(); // keep userId + salt to log in later: client.login(userId, masterPassword, salt)

// LSH bucketing needs a secret seed of at least 16 bytes. Reuse the same seed
// on every session, or earlier memories will not be found.
client.initLshFromSeed(lshSeed);

await client.remember('I prefer coffee over tea in the morning');

const results = await client.recall('what do I like to drink?'); // RerankedResult[]
console.log(results[0]?.fact.text);
```

## Crypto Primitives

| Operation | Algorithm | Purpose |
|-----------|-----------|---------|
| Key derivation | Argon2id + HKDF-SHA256 | Memory-hard password hashing + auth key derivation |
| Encryption | XChaCha20-Poly1305 | Authenticated encryption of memories and embeddings |
| Blind indices | SHA-256 | Searchable encryption without exposing plaintext |
| LSH | Random hyperplane | Approximate nearest neighbor search on encrypted data |
| Embeddings | Harrier-OSS-v1-270M (640d) | Local semantic similarity (no API calls) |
| Reranking | BM25 + cosine + RRF | Multi-signal result fusion |

## Development

```bash
npm install     # Install dependencies
npm test        # Run tests
npm run build   # Build
```

## Learn More

- [Architecture Spec](https://github.com/graphprotocol/totalreclaw/blob/main/docs/specs/totalreclaw/architecture.md) -- E2EE design with LSH + blind buckets
- [Self-hosted deployment](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/self-hosted-deployment.md)
- [totalreclaw.xyz](https://totalreclaw.xyz)
- [Main Repository](https://github.com/graphprotocol/totalreclaw)

## License

MIT
