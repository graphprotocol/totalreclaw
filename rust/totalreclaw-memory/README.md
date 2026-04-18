# totalreclaw-memory

End-to-end encrypted memory backend for Rust. Native `Memory` trait implementation that powers [ZeroClaw](https://near.ai/zeroclaw) and any other Rust agent or app that wants the TotalReclaw memory model.

**v2.0.0 ships Memory Taxonomy v1** — every memory is typed (`claim` / `preference` / `directive` / `commitment` / `episode` / `summary`) and tagged with `source`, `scope`, and `volatility`. Retrieval uses source-weighted reranking via the shared `totalreclaw-core` crate. See the [memory types guide](../../docs/guides/memory-types-guide.md).

## Features

- **Memory trait** — implements the canonical async `store` / `recall` / `forget` / `export` / `status` / `debrief` contract.
- **Memory Taxonomy v1** — new `store_v1()` method + `V1StoreInput` struct with type / source / scope / volatility / reasoning.
- **Native ERC-4337** — UserOp construction via `alloy-primitives` / `alloy-sol-types`, verified byte-for-byte against viem.
- **Batching** — up to 15 facts per UserOp via `executeBatch()`.
- **Cosine + fingerprint dedup** — near-duplicate prevention at store time (via shared core).
- **Contradiction detection + pin semantics** — via shared core.
- **Hot cache** — 30-entry local query cache with cosine similarity matching.
- **Billing cache** — 2-hour TTL with quota warnings (>80%) and 403 invalidation.
- **Chain ID auto-detect** — free tier on Base Sepolia, Pro on Gnosis. No env var.
- **Protobuf v4 outer wrapper** — inner blob is v1 JSON.

## Quick Start

```toml
[dependencies]
totalreclaw-memory = "2.0"
```

```rust
use totalreclaw_memory::{Memory, TotalReclawMemory, V1StoreInput};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let memory = TotalReclawMemory::new(
        "your twelve word recovery phrase here",
    ).await?;

    // Store a memory with v1 taxonomy
    memory.store_v1(V1StoreInput {
        text: "Pedro prefers dark mode for all editors".into(),
        type_: "preference".into(),
        source: "user".into(),
        scope: Some("personal".into()),
        volatility: Some("stable".into()),
        importance: 0.8,
        reasoning: None,
    }).await?;

    // Recall — source-weighted reranking applied automatically
    let results = memory.recall("What does Pedro prefer?", 8).await?;
    for r in results {
        println!("[{:.3}] {}", r.score, r.text);
    }

    Ok(())
}
```

## Cross-client parity

All encryption, fingerprinting, LSH, reranking, and v1 taxonomy validation is handled by the shared [`totalreclaw-core`](../totalreclaw-core) crate — the same Rust core compiled to WASM for the TypeScript clients and PyO3 for the Python client. Memories written by this crate are byte-equivalent to memories written by `@totalreclaw/client`, `totalreclaw` (Python), or `@totalreclaw/mcp-server`.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word recovery phrase | Required |
| `TOTALRECLAW_SERVER_URL` | Relay URL | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | Use self-hosted server | `false` |

See the [env vars reference](../../docs/guides/env-vars-reference.md) for the canonical list (5 user-facing vars after the v1 cleanup).

## Learn More

- [ZeroClaw setup guide](../../docs/guides/zeroclaw-setup.md)
- [Client setup guide (v1)](../../docs/guides/client-setup-v1.md)
- [Memory types guide](../../docs/guides/memory-types-guide.md)
- [Feature comparison](../../docs/guides/feature-comparison.md)
- [Architecture](../../docs/architecture.md)

## License

MIT
