# totalreclaw-core

Shared core library powering every TotalReclaw client — a single source of truth for crypto, search, reranking, wallet derivation, UserOp construction, and store pipelines.

Used natively by [`totalreclaw-memory`](https://crates.io/crates/totalreclaw-memory) (Rust / ZeroClaw), and exposed via WASM (`@totalreclaw/core` on npm) and PyO3 (`totalreclaw-core` on PyPI) bindings for TypeScript and Python clients.

## What's in the box

- **Crypto** — XChaCha20-Poly1305 envelope encryption, HKDF key derivation, BIP-39 mnemonic + BIP-44 wallet derivation, Keccak256.
- **Reranker** — BM25 + cosine + RRF with intent-weighted scoring.
- **Store / Search pipelines** — canonical claim construction, LSH blind indexing, fingerprinting.
- **Dedup & KG** — best-match near-duplicate detection, cluster facts, contradiction detection orchestration, pin semantics, decision log.
- **ERC-4337** — UserOp construction (feature-gated via `managed`), signing verified byte-for-byte against viem.
- **Hot cache + consolidation + debrief + stemmer**.

## Features

- `managed` (default) — ERC-4337 UserOp support via `alloy-primitives` / `alloy-sol-types`.
- `wasm` — WASM bindings via `wasm-bindgen` (consumed by `@totalreclaw/core` npm package).
- `python` / `python-extension` — PyO3 bindings (consumed by `totalreclaw-core` PyPI package).

## Usage

```toml
[dependencies]
totalreclaw-core = "2.0"
```

Most Rust users should depend on [`totalreclaw-memory`](https://crates.io/crates/totalreclaw-memory), which bundles core into a high-level `Memory` trait.

## License

MIT

## Links

- Homepage: <https://totalreclaw.xyz>
- Repository: <https://github.com/p-diogo/totalreclaw>
- npm (WASM): [`@totalreclaw/core`](https://www.npmjs.com/package/@totalreclaw/core)
- PyPI (PyO3): [`totalreclaw-core`](https://pypi.org/project/totalreclaw-core/)
