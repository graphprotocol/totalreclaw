# Feature Comparison -- TotalReclaw Integrations

TotalReclaw works across multiple AI agent platforms. The core encryption, storage, and search pipeline is identical everywhere -- the differences are in automation (lifecycle hooks) and available tools. This table shows what each platform supports.

---

## Platform Feature Matrix

| Feature | OpenClaw | MCP (Claude Desktop, Cursor, Windsurf) | NanoClaw | Hermes (Python) | IronClaw (NEAR AI) | ZeroClaw (Rust) |
|---------|:-:|:-:|:-:|:-:|:-:|:-:|
| **Automatic Memory** | | | | | | |
| Auto-recall (every message) | Yes | -- | Yes | Yes | -- | Yes |
| Auto-extract (every 3 turns) | Yes | -- | Yes | Yes | -- | Yes |
| Pre-compaction flush | Yes | -- | Yes | -- | -- | -- |
| Session debrief | Yes | Yes (tool) | Yes | Yes | Yes (tool) | Yes |
| **Explicit Tools** | | | | | | |
| Remember / Recall / Forget / Export | Yes | Yes | Yes | Yes | Yes | Yes |
| Status (billing & usage) | Yes | Yes | Yes | Yes | Yes | Yes |
| Import from Mem0 / ChatGPT / Claude | Yes | Yes | Yes | -- | Yes | -- |
| Upgrade to Pro | Yes | Yes | Yes | -- | Yes | -- |
| Migrate (testnet to mainnet) | Yes | Yes | Yes | -- | Yes | -- |
| Consolidate (bulk dedup) | Yes | Yes | Yes | -- | Yes | -- |
| **Dedup** | | | | | | |
| Cosine near-duplicate prevention | Yes | Yes | Yes | Yes | Yes | Yes |
| LLM-guided dedup (contradictions) | Yes | -- | Yes | Yes | -- | -- |
| Content fingerprint (exact match) | Yes | Yes | Yes | Yes | Yes | Yes |
| **Search** | | | | | | |
| BM25 + Cosine + RRF reranking | Yes | Yes | Yes | Yes | Yes | Yes |
| Broadened search fallback | Yes | Yes | Yes | Yes | Yes | Yes |
| Hot cache (skip remote on repeat) | Yes | -- | -- | -- | -- | Yes |

---

## Platform Notes

**OpenClaw** -- Fully automatic. Memory extraction and recall happen via lifecycle hooks with no user intervention. Best experience for users who want set-and-forget memory.

**MCP (Claude Desktop, Cursor, Windsurf)** -- Tools only. The host agent (Claude, Cursor, etc.) decides when to call memory tools based on context. No automatic extraction -- the agent uses tools when contextually appropriate. Setup via `npx @totalreclaw/mcp-server setup`.

**NanoClaw** -- Automatic, like OpenClaw. The NanoClaw agent-runner spawns the MCP server as a background process. Memory is shared within the NanoClaw group. No user setup required -- the admin configures the recovery phrase.

**Hermes (Python)** -- Automatic via pre/post LLM call hooks. LLM extraction with heuristic fallback. Core tools work; import/migrate/consolidate not yet implemented.

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
