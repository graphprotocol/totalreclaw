# totalreclaw

End-to-end encrypted memory + knowledge graph for AI agents.

Store, search, and recall memories across any AI agent with zero-knowledge encryption. Your data is encrypted on-device before it leaves -- the server never sees plaintext.

As of **2.0.0**, the client uses **Memory Taxonomy v1** (6 canonical types: `claim | preference | directive | commitment | episode | summary`) and **Retrieval v2 Tier 1** source-weighted reranking (user-sourced facts rank higher than assistant-sourced facts on tied BM25 + cosine scores). See the [memory types guide](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/memory-types-guide.md) and [TS plugin 3.0.0 migration notes](https://github.com/p-diogo/totalreclaw/blob/main/skill/plugin/CHANGELOG.md) — the Python client follows the same pattern. Existing pre-v1 vault entries decrypt transparently.

## Features

- **End-to-end encrypted** -- XChaCha20-Poly1305 encryption, HKDF key derivation from a BIP-39 recovery phrase
- **Portable** -- Same recovery phrase works across Hermes, OpenClaw, Claude Desktop, IronClaw, ZeroClaw
- **Memory Taxonomy v1** -- 6 speech-act types + required provenance (`user | user-inferred | assistant | external | derived`) and 8 life-domain scopes. v1 is the only write path (no env-var gating).
- **Retrieval v2 Tier 1** -- source-weighted reranking via `totalreclaw-core@2.0.0` PyO3 bindings (user-sourced facts rank higher on tied scores)
- **G-pipeline extraction** -- merged-topic prompt, provenance filter (lax), comparative rescoring, volatility heuristic
- **Local embeddings** -- Harrier-OSS-v1-270M runs on-device (no API calls)
- **Hybrid search** -- BM25 + cosine similarity + RRF reranking
- **LSH bucketing** -- Locality-sensitive hashing for encrypted search
- **On-chain storage** -- Managed service stores on Gnosis/Base Sepolia via ERC-4337; outer protobuf v4

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

    # Store a memory — v1 taxonomy defaults: type="claim", source="user", scope="unspecified".
    # Importance is 1-10 (int) or 0-1 (float, auto-normalized).
    # v1 types: claim | preference | directive | commitment | episode | summary
    # scope, volatility, reasoning also accepted.
    fact_id = await client.remember(
        "Pedro prefers dark mode for all editors",
        fact_type="preference",
        scope="personal",
        importance=8,
    )

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
Plaintext → XChaCha20-Poly1305 encrypt → Blind indices (SHA-256) → LSH buckets → On-chain via relay
                                                                              ↓
Query → Blind trapdoors → GraphQL search → Decrypt candidates → BM25+Cosine+RRF rerank → Top 8
```

All encryption happens client-side. The relay server and on-chain storage never see plaintext.

## Cross-Language Parity

This Python client produces byte-for-byte identical outputs to the TypeScript implementation (`@totalreclaw/mcp-server`):

- Key derivation (HKDF-SHA256)
- XChaCha20-Poly1305 wire format (nonce || tag || ciphertext)
- Blind indices (SHA-256 + Porter stemming)
- Content fingerprints (HMAC-SHA256)
- LSH bucket hashes (32-bit x 20 tables)

Memories stored by the Python client can be recalled by the MCP server, and vice versa.

## Learn More

- [Client setup guide (v1)](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/client-setup-v1.md)
- [Memory types guide](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/memory-types-guide.md)
- [v1 migration guide](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/v1-migration.md)
- [Environment variables](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/env-vars-reference.md)
- [Hermes setup guide](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md)
- [Feature comparison](https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/feature-comparison.md)

## Recent changes

### 2.0.2 — 2026-04-19

Real-user QA on a clean VPS install of 2.0.1 flagged four bugs that made
the Python client effectively unusable for new users. All four are fixed
in 2.0.2:

- **`Event loop is closed`** across `totalreclaw_status`, `totalreclaw_export`,
  and the `pre_llm_call` auto-recall hook. Root cause: sync hook callbacks
  created a fresh `asyncio.new_event_loop()` per call, caching an
  `httpx.AsyncClient` that got orphaned across loop boundaries. Fix:
  process-wide background event loop (`totalreclaw.agent.loop_runner`) that
  owns every async call from sync code, plus per-loop caching of the
  internal `httpx.AsyncClient`.
- **`totalreclaw_export` returning `count=0`** despite facts on-chain.
  Same root cause as above — the subgraph query silently failed with
  `Event loop is closed` and the outer `except` returned an empty list.
  Fixed by the same per-loop httpx cache.
- **`TOTALRECLAW_SESSION_ID` rejected as a removed env var** by 2.0.1's
  v1 cleanup. Broke Axiom session-scoped log queries. Restored: the
  `RelayClient` now accepts `session_id=` (or picks it up from the env
  var) and forwards it as `X-TotalReclaw-Session` on every HTTP call.
- **No chain-id auto-detect for Pro-tier users.** 2.0.1 hardcoded chain
  `84532` (Base Sepolia), so Pro Python users had writes signed for the
  wrong chain and the relay silently returned AA23. `resolve_chain_id()`
  now reads `/v1/billing/status` once and switches to chain `100` (Gnosis)
  for Pro accounts, matching MCP. Best-effort: network errors fall back
  to free-tier chain so offline users keep working.

No data-loss: existing facts on-chain remain intact; only the Python
client's user-experience layer changed.

### 2.0.1 — 2026-04-18

- Fixed `wallet_address` property returning the EOA placeholder before
  `resolve_address()` ran. The property now raises `RuntimeError` if read
  before resolution; accessing it after `await client.resolve_address()` or
  after the first `remember/recall/forget/export` call works as before.
- Added `await client.get_wallet_address()` async getter that resolves
  lazily and returns the Smart Account address in one call — preferred for
  introspection code that doesn't want to sequence a separate
  `resolve_address()` step.
- **Not a data-loss bug.** UserOps mined correctly in 2.0.0; facts land
  on-chain under the correct Smart Account. Only the introspection API
  was wrong, which misled QA tooling into querying the subgraph with the
  EOA.

## License

MIT
