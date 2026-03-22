# TotalReclaw -- ClawHub Listing Description

## Short Description (for search results)

End-to-end encrypted memory for AI agents. Your agent remembers -- only you can read it.

## Full Description

TotalReclaw gives your AI agent persistent, encrypted memory that works across sessions and devices. Unlike other memory solutions, TotalReclaw uses true end-to-end encryption: your memories are encrypted on your device before they ever reach the server. Not even TotalReclaw can read your data.

**What makes it different:**
- **True end-to-end encryption** -- Client-side AES-256-GCM encryption. The server only sees encrypted blobs. (Most competitors encrypt server-side, meaning the server can read your data.)
- **Portable** -- One-click plaintext export. No vendor lock-in. Your memories belong to you.
- **Automatic** -- In OpenClaw, memory extraction and recall happen automatically via lifecycle hooks. No manual commands needed.
- **Cross-device** -- Same 12-word seed phrase recovers all your memories on any device.
- **98.1% recall** -- Blind-index search with BM25 + cosine + RRF fusion reranking delivers near-perfect retrieval.
- **Free tier** -- 500 memories/month, unlimited reads. Pro at $5/month (unlimited, permanent on-chain).

**How it works:** Install, set a password, done. Your agent automatically extracts facts, preferences, and decisions from conversations, encrypts them, and stores them. At the start of each new conversation, relevant memories are recalled and injected into context.

**Works with:** OpenClaw (automatic via hooks), Claude Desktop, Cursor, and any MCP-compatible agent.

### TotalReclaw vs. agentmemory

| Feature | TotalReclaw | agentmemory |
|---------|-------------|-------------|
| Encryption | Client-side E2EE (server never sees plaintext) | Server-side (server can read your data) |
| Data portability | One-click plaintext export | No export |
| Key management | BIP-39 seed phrase (user-controlled) | Server-managed keys |
| Search method | Blind-index + encrypted reranking | Plaintext vector search |
| On-chain option | Yes (Gnosis Chain subgraph) | No |
| Cross-device | Same seed = same memories anywhere | Tied to account |

The fundamental difference: with TotalReclaw, even a compromised server reveals nothing. With server-side encryption, a breach exposes all your data.

## Keywords

memory, e2ee, e2e-encryption, encryption, privacy, agent-memory, persistent-context, cross-device, portable

## Category

memory
