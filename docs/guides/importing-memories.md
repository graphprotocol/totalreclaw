# Importing Memories into TotalReclaw

TotalReclaw can import memories from external AI memory systems, so you can consolidate fragmented memory from other tools into a single encrypted vault. All imported data is encrypted client-side with your key before reaching the server — the same zero-knowledge guarantee as natively stored memories.

---

## Supported Sources

| Source | Method | Status |
|--------|--------|--------|
| **Mem0** (mem0.ai) | Live API fetch | Fully supported, E2E validated |
| **MCP Memory Server** (`@modelcontextprotocol/server-memory`) | JSONL file import | Supported (unit tested) |
| More sources planned | See [Roadmap](../ROADMAP.md#phase-5-import--migration-future) | -- |

---

## How It Works

```
Source System (e.g., Mem0)
        |
        v  (fetch via API or read file)
+-------------------+
|   Your Device     |
|                   |
|  1. Fetch/parse   |
|  2. Encrypt       |  ← AES-256-GCM with your key
|  3. Generate LSH  |  ← Blind search indices
|  4. Fingerprint   |  ← Content dedup hash
|  5. Store         |
+-------------------+
        |
        v  (encrypted blobs only)
   TotalReclaw Server
```

Key properties:

- **Zero-knowledge** — Source data is fetched and processed entirely on your device. The TotalReclaw server never sees plaintext.
- **Idempotent** — Content fingerprint dedup means running the same import twice won't create duplicates.
- **Searchable immediately** — Imported memories get the same blind indices and embeddings as natively stored ones. They appear in recall results right away.

---

## Importing from Mem0

### What You Need

1. A **Mem0 API key** — get it from [app.mem0.ai](https://app.mem0.ai) → Settings → API Keys.
2. Your **Mem0 user ID** (optional — defaults to `"user"`). This is the `user_id` you used when storing memories via the Mem0 API or SDK.

### Usage

Ask your agent:

> "Import my memories from Mem0 using API key m0-abc123..."

Or be more specific:

> "Import my Mem0 memories. My API key is m0-abc123 and my user ID is alice."

The agent calls the `totalreclaw_import_from` tool with:

| Parameter | Description | Required |
|-----------|-------------|----------|
| `source` | `"mem0"` | Yes |
| `api_key` | Your Mem0 API key (`m0-...`) | Yes |
| `source_user_id` | Your Mem0 user ID | No (defaults to `"user"`) |

### What Happens

1. The tool fetches all memories from the Mem0 REST API (`GET /v1/memories/?user_id=...`), paginating automatically.
2. Each memory's text is encrypted with your TotalReclaw encryption key (AES-256-GCM).
3. Blind search indices (LSH buckets + word trapdoors) are generated so the encrypted memories are searchable.
4. A content fingerprint (HMAC-SHA256) is generated to prevent future duplicates.
5. Each encrypted memory is stored on the TotalReclaw server (or on-chain if using the managed service).

### Expected Output

> "Successfully imported 42 memories from Mem0. All 42 memories were imported with no skipped entries."

If some memories were already imported previously:

> "Imported 42 memories from Mem0. 38 new, 4 skipped (duplicates)."

### Important Notes

- **Mem0's extraction is lossy.** Mem0 uses its own LLM to extract facts from your conversations. Not every message produces a memory — only what Mem0's model deemed important. The import brings over exactly what Mem0 stored, not the original conversations.
- **Mem0 API key is used once.** The key is only used during the import to fetch your data. It is not stored by TotalReclaw.
- **File-based import.** If you have a Mem0 JSON export file (from their data export feature), you can also import it directly without an API key. Ask: "Import my Mem0 memories from this file: [paste JSON or provide path]."

---

## Importing from MCP Memory Server

### What You Need

1. The JSONL knowledge graph file produced by `@modelcontextprotocol/server-memory`. This is typically stored at the path configured in your MCP client (e.g., `~/.mcp-memory/memory.jsonl`).

### Usage

Ask your agent:

> "Import my memories from MCP Memory Server at ~/.mcp-memory/memory.jsonl"

The agent calls `totalreclaw_import_from` with:

| Parameter | Description | Required |
|-----------|-------------|----------|
| `source` | `"mcp-memory"` | Yes |
| `file_path` | Path to the JSONL file | Yes |

### What Happens

1. The JSONL file is parsed. Each line contains an entity, relation, or observation from the MCP Memory knowledge graph.
2. Observations (the actual factual content) are extracted and converted to TotalReclaw memory format.
3. Each observation is encrypted, indexed, fingerprinted, and stored — same as Mem0 import.

### Important Notes

- **Entities and relations are preserved as context** within the imported observations. For example, an observation "Prefers dark mode" attached to entity "Alice" is stored as "Alice: Prefers dark mode".
- **The JSONL file is read locally** — it never leaves your device unencrypted.

---

## After Import

Once imported, your memories behave identically to natively stored ones:

- **Auto-recall** (OpenClaw plugin) — imported memories are injected into agent context when relevant, just like any other memory.
- **Explicit recall** — `totalreclaw_recall` searches across all memories, including imported ones.
- **Export** — `totalreclaw_export` includes imported memories in the export.
- **Forget** — you can delete individual imported memories with `totalreclaw_forget`.

There is no distinction between "imported" and "native" memories after import. They are all encrypted blobs with blind indices.

---

## Availability

The `totalreclaw_import_from` tool is available in:

| Platform | Available | Notes |
|----------|-----------|-------|
| **OpenClaw plugin** | Yes | Ask your agent or use the tool directly |
| **MCP server** | Yes | Tool is registered and callable by any MCP client |
| **NanoClaw** | Yes | Via the MCP server spawned by the agent-runner |

**Storage mode support:** Import works in both **self-hosted (HTTP) mode** and **managed service mode**. In managed service mode, each imported memory is submitted on-chain via the relay. Large imports (1,000+ memories) may take longer in managed service mode due to per-transaction gas sponsorship.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Invalid API key" (Mem0) | Verify your key starts with `m0-` and hasn't expired. Get a new one at [app.mem0.ai](https://app.mem0.ai). |
| "No memories found" (Mem0) | Check your `source_user_id`. The default is `"user"` but you may have used a different ID. |
| Import succeeded but recall doesn't find them | Wait a few seconds for indexing, then try an explicit recall: "Use totalreclaw_recall to search for [topic]." |
| "File not found" (MCP Memory) | Provide the full absolute path to the JSONL file. |
| Duplicates after re-import | This shouldn't happen — content fingerprint dedup prevents it. If it does, run the import again; existing duplicates won't be re-created. |

---

## Future Import Sources

The following sources are planned but not yet implemented:

- **MemoClaw** (memoclaw.com) — via their API (SDK is MIT-licensed)
- **Zep** (getzep.com) — session-based memory with facts and summaries
- **LanceDB** — local or cloud vector store export
- **QMD** — OpenClaw's native memory system
- **Generic JSON/CSV** — catch-all for other tools
- **Claude / ChatGPT / Gemini** — conversation history import via data export

See the [full roadmap](../ROADMAP.md#phase-5-import--migration-future) for details.
