# TotalReclaw Changelog

## plugin v3.0.5 (April 2026)
- Fix OpenClaw scanner false-positive from JSDoc "fetch" wording in `config.ts`. No behavior change.
- Added `scanner-sim` CI check to prevent regressions.

## v1.0-beta (March 2026) -- Private Beta
- End-to-end encrypted memory vault for AI agents (AES-256-GCM, HKDF key derivation)
- Dual-chain storage: Free tier on Base Sepolia testnet, Pro tier on Gnosis mainnet
- Stripe billing integration ($5/month Pro tier, 500 free memories/month)
- OpenClaw plugin with automatic memory extraction via lifecycle hooks
- MCP server for Claude Desktop, Cursor, and other MCP-compatible agents
- NanoClaw integration with automatic hooks and CLAUDE.md sync
- IronClaw support via MCP server (routine-based extraction)
- Import adapters: Mem0, ChatGPT, Claude, MCP Memory Server
- Testnet-to-mainnet migration tool (`totalreclaw_migrate`)
- Harrier-OSS-v1-270M embedding model (640d, ~164MB)
- BM25 + Cosine + RRF fusion reranking with dynamic candidate pool sizing
- Store-time near-duplicate dedup (cosine) and LLM-guided dedup (Pro)
- Client batching: multiple facts per UserOp via ERC-4337 executeBatch
- 9/9 E2E integration test journeys passing

## v0.2.0 (March 2026)
- End-to-end encrypted memory vault for AI agents
- OpenClaw skill with automatic memory extraction via lifecycle hooks
- MCP server for Claude Desktop, Cursor, and other MCP-compatible agents
- Stripe billing integration
- On-chain storage via Gnosis Chain with Pimlico gas sponsorship
- 78/78 E2E integration test assertions passing
- Free tier: 500 memories/month, unlimited reads

## v0.1.0 (February 2026) -- PoC
- Initial proof of concept
- AES-256-GCM encryption with blind-index search
- LSH-based fuzzy matching (98.1% Recall@8)
- BM25 + Cosine + RRF fusion reranking
