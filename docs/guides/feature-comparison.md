# Feature Comparison -- TotalReclaw Integrations

TotalReclaw works across multiple AI agent platforms. The core encryption, storage, search pipeline, and v1 taxonomy are identical everywhere -- the differences are in automation (lifecycle hooks) and available tools. This table shows what each platform supports.

All clients ship v1 by default. Stable production versions: core 2.2.0, plugin 3.2.3, mcp-server 3.2.0, nanoclaw 3.0.0, python 2.3.0.

---

## Platform Feature Matrix

| Feature | OpenClaw | MCP (Claude Desktop, Cursor, Windsurf) | NanoClaw | Hermes (Python) | IronClaw (NEAR AI) | ZeroClaw (Rust) |
|---------|:-:|:-:|:-:|:-:|:-:|:-:|
| **Memory Taxonomy v1** | | | | | | |
| 6-type taxonomy (claim/preference/directive/commitment/episode/summary) | Yes (default) | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes |
| source / scope / volatility axes | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes |
| reasoning field for decision-style claims | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes |
| Retrieval v2 Tier 1 (source-weighted rerank) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes |
| Protobuf v4 outer wrapper | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes |
| **Automatic Memory** | | | | | | |
| Auto-recall (every message) | Yes | -- | Yes | Yes | -- | Yes |
| Auto-extract (G-pipeline, every 3 turns) | Yes | -- | Yes | Yes | -- | Yes |
| Pre-compaction flush | Yes | -- | Yes | -- | -- | -- |
| Session debrief (v1 `summary`, source=derived) | Yes | Yes (tool) | Yes | Yes | Yes (tool) | Yes |
| **Explicit Tools** | | | | | | |
| Remember / Recall / Forget / Export | Yes | Yes | Yes | Yes | Yes | Yes |
| Status (billing & usage) | Yes | Yes | Yes | Yes | Yes | Yes |
| Pin / Unpin (v1) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- |
| Retype (v1) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- |
| Set scope (v1) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- |
| Import from Mem0 / ChatGPT / Claude / Gemini | Yes | Yes | Yes | Yes | Yes | -- |
| Upgrade to Pro | Yes | Yes | Yes | -- | Yes | -- |
| Migrate (testnet to mainnet) | Yes | Yes | Yes | -- | Yes | -- |
| Consolidate (bulk dedup) | Yes | Yes | Yes | -- | Yes | -- |
| **Dedup** | | | | | | |
| Cosine near-duplicate prevention | Yes | Yes | Yes | Yes | Yes | Yes |
| LLM-guided dedup (contradictions) | Yes | -- | Yes | Yes | -- | -- |
| Content fingerprint (exact match) | Yes | Yes | Yes | Yes | Yes | Yes |
| **Search** | | | | | | |
| BM25 + Cosine + RRF reranking | Yes | Yes | Yes | Yes | Yes | Yes |
| Source-weighted final score (v1 Tier 1) | Yes | Yes | Yes | Yes | Yes | Yes |
| Broadened search fallback | Yes | Yes | Yes | Yes | Yes | Yes |
| Hot cache (skip remote on repeat) | Yes | -- | -- | -- | -- | Yes |

---

## Platform Notes

**OpenClaw** -- Fully automatic. Memory extraction and recall happen via lifecycle hooks with no user intervention. Best experience for users who want set-and-forget memory.

**MCP (Claude Desktop, Cursor, Windsurf)** -- Tools only. The host agent (Claude, Cursor, etc.) decides when to call memory tools based on context. No automatic extraction -- the agent uses tools when contextually appropriate. Setup via `npx @totalreclaw/mcp-server setup`.

**NanoClaw** -- Automatic, like OpenClaw. The NanoClaw agent-runner spawns the MCP server as a background process. Memory is shared within the NanoClaw group. No user setup required -- the admin configures the recovery phrase.

**Hermes (Python)** -- Automatic via pre/post LLM call hooks. LLM extraction with heuristic fallback. Full tool parity with OpenClaw plugin since 2.3.x: remember / recall / forget / export / status / account-setup (`totalreclaw_pair`) / pin / unpin / retype / set_scope / import_from / upgrade. Migrate + consolidate are managed-service-only and exposed via the plugin / MCP server.

**IronClaw (NEAR AI)** -- Uses the MCP server for tools. No lifecycle hooks -- auto-extraction requires setting up routines (cron). The TEE protects the runtime; TotalReclaw protects the data.

**ZeroClaw (Rust)** -- Native Rust implementation via the Memory trait. Automatic recall and extraction. Includes hot cache, decay handling (7-day half-life), and conflict resolution. Import/migrate not yet implemented.

---

## Setup Guides

- [OpenClaw](openclaw-setup.md)
- [Claude Desktop / Cursor / Windsurf](claude-code-setup.md)
- [NanoClaw](nanoclaw-getting-started.md)
- [Hermes (Python)](hermes-setup.md)
- [IronClaw (NEAR AI)](ironclaw-setup.md)
- [ZeroClaw (Rust)](zeroclaw-setup.md)

---

## Storage Modes

All platforms support two storage modes. The encryption is identical -- the difference is where encrypted data is stored.

| | Managed Service (default) | Self-Hosted |
|---|---|---|
| **Storage** | On-chain (Gnosis Chain) via The Graph | Your PostgreSQL database |
| **Free tier** | Unlimited on Base Sepolia testnet | Unlimited (your infrastructure) |
| **Pro tier** | Permanent on Gnosis mainnet ($3.99/mo) | N/A |
| **Setup** | Nothing -- works out of the box | Set `TOTALRECLAW_SELF_HOSTED=true` + server URL |
| **Consolidation tool** | Not available (no batch delete on-chain) | Available |
