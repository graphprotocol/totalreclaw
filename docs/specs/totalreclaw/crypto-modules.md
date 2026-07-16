# Crypto Module Duplication

Three packages contain independent implementations of the same cryptographic primitives.

## Why three copies?

| Package | Path | Reason for separate copy |
|---------|------|--------------------------|
| **Client library** | `client/src/crypto/` | Canonical reference. Uses Node.js `crypto` + `argon2` (C binding). Split across 5 files (kdf, aes, blind, fingerprint, seed). |
| **OpenClaw plugin** | `skill/plugin/crypto.ts` | Single-file bundle. Cannot import from `client/` due to OpenClaw's plugin sandbox. Uses `@noble/hashes` (pure JS) instead of the `argon2` C binding. |
| **MCP server** | `mcp/src/subgraph/crypto.ts` | Standalone npm package (`@totalreclaw/mcp-server`). Same `@noble/hashes` stack as the plugin. Published independently, no monorepo dependency on `client/`. |

The plugin and MCP copies are nearly identical to each other. The client library differs in structure (multiple files, async API, C-binding Argon2) but produces byte-identical output for the same inputs.

## Which is canonical?

`client/src/crypto/` is the reference implementation. The plugin and MCP copies are simplified ports that must match its output exactly.

## What must stay in sync

Any change to these values in one copy must be mirrored in all three:

| Parameter | Value | Files |
|-----------|-------|-------|
| HKDF info strings | `totalreclaw-auth-key-v1`, `totalreclaw-encryption-key-v1`, `openmemory-dedup-v1`, `openmemory-lsh-seed-v1` | kdf.ts, crypto.ts (x2) |
| Argon2id params | t=3, m=65536, p=4, dkLen=32 | kdf.ts, crypto.ts (x2) |
| AES-GCM wire format | `[iv:12][tag:16][ciphertext]`, base64-encoded | aes.ts, crypto.ts (x2) |
| Blind index tokenization | lowercase, remove punctuation (Unicode-aware), split whitespace, min 2 chars, SHA-256 per token + `stem:` prefix for Porter stems | blind.ts, crypto.ts (x2) |
| Content fingerprint | HMAC-SHA256(dedupKey, NFC + lowercase + collapse whitespace + trim) | fingerprint.ts, crypto.ts (x2) |
| BIP-39 key derivation | HKDF from 512-bit seed, first 32 bytes as salt | seed.ts, crypto.ts (x2) |

## How to verify parity

The parity test suite at `tests/parity/` verifies byte-identical output across all shared operations:

```bash
cd tests/parity && npm install && npx tsx parity-test.ts
```

It covers: key derivation, auth key hash, blind indices, content fingerprints, LSH seed + bucket hashes, tokenization, BM25, cosine similarity, RRF fusion, and cross-implementation encrypt/decrypt round-trips.

## Future plan

Post-launch, extract the shared primitives into a `@totalreclaw/crypto` npm package. The plugin, MCP server, and client would all depend on it, eliminating the duplication. This is deferred because:

1. The current parity tests catch drift reliably.
2. Extracting a shared package adds build/publish complexity before the product ships.
3. The OpenClaw plugin sandbox constraints need to be validated against an external dependency.
