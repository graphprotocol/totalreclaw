# totalreclaw

End-to-end encrypted memory for AI agents -- the "password manager for AI memory."

Store, search, and recall memories across any AI agent with zero-knowledge encryption. Your data is encrypted on-device before it leaves -- the server never sees plaintext.

## Features

- **End-to-end encrypted** -- AES-256-GCM encryption, HKDF key derivation from BIP-39 mnemonic
- **Portable** -- Same recovery phrase works across Hermes, OpenClaw, Claude Desktop, IronClaw
- **Local embeddings** -- Harrier-OSS-v1-270M runs on-device (no API calls)
- **Hybrid search** -- BM25 + cosine similarity + RRF reranking
- **LSH bucketing** -- Locality-sensitive hashing for encrypted search
- **On-chain storage** -- Managed service stores on Gnosis/Base Sepolia via ERC-4337

## Quick Start

```bash
pip install totalreclaw
```

> **Docker users:** On slim images (e.g., `python:3.12-slim`), install a C compiler first for PyStemmer:
> ```bash
> apt-get update && apt-get install -y gcc g++
> ```

```python
import asyncio
from totalreclaw import TotalReclaw

async def main():
    client = TotalReclaw(
        recovery_phrase="your twelve word recovery phrase here",
        server_url="https://api.totalreclaw.xyz",  # default, can be omitted
    )

    # REQUIRED: resolve Smart Account address and register with relay
    await client.resolve_address()
    await client.register()

    # Store a memory (importance is a float from 0.0 to 1.0)
    fact_id = await client.remember("Pedro prefers dark mode for all editors", importance=0.8)

    # Search memories
    results = await client.recall("What does Pedro prefer?")
    for r in results:
        print(f"  [{r.rrf_score:.3f}] {r.text}")

    # Delete a memory
    await client.forget(fact_id)

    # Export all memories
    facts = await client.export_all()

    # Check billing
    status = await client.status()
    print(f"Tier: {status.tier}, Used: {status.free_writes_used}/{status.free_writes_limit}")

    await client.close()

asyncio.run(main())
```

**Important:** You must call `resolve_address()` and `register()` before any operations. `resolve_address()` derives the CREATE2 Smart Account address via an RPC call, and `register()` authenticates with the relay.

## With Embeddings (Recommended)

For semantic search, install with embedding support:

```bash
pip install totalreclaw
```

```python
from totalreclaw import TotalReclaw
from totalreclaw.embedding import get_embedding

client = TotalReclaw(recovery_phrase="...")

# Store with embedding for semantic search
text = "Pedro prefers dark mode"
embedding = get_embedding(text)
await client.remember(text, embedding=embedding)

# Search with embedding
query = "What are Pedro's UI preferences?"
query_emb = get_embedding(query)
results = await client.recall(query, query_embedding=query_emb)
```

The embedding model (~600 MB) downloads automatically on first use.

## Hermes Agent Plugin

```bash
pip install totalreclaw[hermes]
```

The plugin registers automatically with Hermes Agent v0.5.0+. See the [Hermes setup guide](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md).

## Architecture

```
Plaintext → AES-256-GCM encrypt → Blind indices (SHA-256) → LSH buckets → On-chain via relay
                                                                              ↓
Query → Blind trapdoors → GraphQL search → Decrypt candidates → BM25+Cosine+RRF rerank → Top 8
```

All encryption happens client-side. The relay server and on-chain storage never see plaintext.

## Cross-Language Parity

This Python client produces byte-for-byte identical outputs to the TypeScript implementation (`@totalreclaw/mcp-server`):

- Key derivation (HKDF-SHA256)
- AES-256-GCM wire format (iv || tag || ciphertext)
- Blind indices (SHA-256 + Porter stemming)
- Content fingerprints (HMAC-SHA256)
- LSH bucket hashes (32-bit x 20 tables)

Memories stored by the Python client can be recalled by the MCP server, and vice versa.

## License

MIT
