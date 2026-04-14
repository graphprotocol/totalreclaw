# Importing from ChatGPT

TotalReclaw can import your ChatGPT conversation history and saved memories, so everything ChatGPT knows about you is encrypted and portable across any AI agent.

---

## Two Import Methods

ChatGPT stores your data in two places. You can import from either or both.

| Method | What it contains | Best for |
|--------|-----------------|----------|
| **ChatGPT Memories** (recommended) | Pre-curated facts ChatGPT saved about you | Quick import, high-quality facts |
| **conversations.json** | Full conversation export with all messages | Comprehensive extraction (slower) |

---

## Method 1: Import ChatGPT Memories (Recommended)

ChatGPT's memory feature stores curated facts about you -- the same kind of atomic facts TotalReclaw stores. This is the fastest and highest-quality import path.

### Step 1: Export your ChatGPT memories

1. Open [ChatGPT](https://chatgpt.com)
2. Click your profile icon (bottom left) -> **Settings**
3. Go to **Personalization** -> **Memory**
4. Click **Manage** to see all saved memories
5. Select all the text and copy it (Ctrl+A / Cmd+A, then Ctrl+C / Cmd+C)

### Step 2: Import into TotalReclaw

Tell your agent:

> "Import my ChatGPT memories into TotalReclaw"

Then paste the copied text when asked. Or provide it directly:

> "Import these ChatGPT memories: [paste text]"

The agent calls `totalreclaw_import_from` with `source: "chatgpt"` and your pasted content. Each line becomes an encrypted memory in your TotalReclaw vault.

### What to expect

ChatGPT memories are already curated facts (e.g., "User prefers dark mode", "User works at Acme Corp"), so they import cleanly with minimal noise. A typical import of 50-100 memories takes a few seconds.

---

## Method 2: Import conversations.json

The full data export contains every conversation you have had with ChatGPT. TotalReclaw walks the conversation tree, chunks messages into batches, and uses your configured LLM to extract atomic facts (same smart-import pipeline used everywhere else).

### Step 1: Export your ChatGPT data

1. Open [ChatGPT](https://chatgpt.com)
2. Click your profile icon (bottom left) -> **Settings**
3. Go to **Data Controls**
4. Click **Export data** -> **Confirm export**
5. ChatGPT sends a download link to your email (may take a few minutes to hours)
6. Download and unzip the archive
7. Find `conversations.json` inside the archive

### Step 2: Import into TotalReclaw

Tell your agent:

> "Import my ChatGPT conversations from /path/to/conversations.json"

Or paste the JSON content directly. For large exports, providing the file path is recommended.

The agent calls `totalreclaw_import_from` with `source: "chatgpt"` and the file path or content.

### How extraction works

The adapter traverses each conversation's mapping tree, collecting user and assistant messages in chronological order (assistant replies are kept because they give the LLM the context it needs to understand what the user meant). When a message has multiple children — for example, a regenerated assistant response — the adapter follows the latest sibling, matching the "current" branch you see in the ChatGPT UI.

Messages are windowed into chunks of 80 for LLM extraction (narrative preservation beats small batches). If a chunk exceeds ~40K estimated input tokens — unusual but possible with very long messages — it's recursively halved until it fits. The extractor returns atomic facts across the full 7-category taxonomy (fact, preference, decision, episodic, goal, context, summary).

Short/empty messages and non-text parts (images, tool calls) are skipped automatically. Empty conversations, system/tool messages, and orphaned mapping nodes are also skipped.

### What to expect

- **Processing time**: Proportional to conversation count. Extraction is LLM-bound, so it's slower than memories-only imports — expect minutes, not seconds, for large exports.
- **Cost**: Uses your own LLM API key (the one configured in your agent). Zero cost to TotalReclaw.
- **Quality**: High — the LLM captures implicit facts, decisions with reasoning, and preferences that pattern matching would miss.
- **Dedup**: Store-time cosine dedup + server-side fingerprinting kick in, so re-importing is safe.

If you want a fast, cheap first pass, start with Method 1 (ChatGPT memories). Method 2 is worth it when you want the full long-tail of context from years of conversations.

---

## Dry Run First

Always preview before importing:

> "Import my ChatGPT memories with dry run first"

This parses your data and shows a preview of the first 10 facts without storing anything. Review and confirm before running the actual import.

---

## After Import

Imported memories behave identically to natively stored ones:

- **Searchable immediately** via `totalreclaw_recall`
- **Encrypted** with your recovery phrase (XChaCha20-Poly1305)
- **Tagged** with `import_source:chatgpt` for filtering
- **Deduplicated** -- re-importing the same data skips duplicates automatically

---

## Tips

- **Start with memories for a fast first pass.** ChatGPT memories are small and pre-curated, so they import in seconds. Run `conversations.json` afterwards when you want the long-tail context.
- **Review after import.** Run `totalreclaw_recall` with a few queries to verify the imported facts make sense.
- **Use consolidate after large imports.** If you import from both methods, run `totalreclaw_consolidate` with `dry_run=true` to find and merge near-duplicates.
- **Curate manually if needed.** Use `totalreclaw_forget` to remove any imported facts that are outdated or incorrect.

---

## Local-only import (sensitive datasets)

If your ChatGPT export contains sensitive material and you don't want plaintext to ever leave your machine during extraction, run the import against a **fully local stack**: local llama.cpp server, local anvil chain, local subgraph, local relay shim. No remote LLM, no remote bundler, no remote subgraph.

### 1. Bring up the local stack

The `totalreclaw-internal/dev-stack/` directory has an orchestrated setup (docker-compose + scripts). From a fresh clone:

```bash
cd totalreclaw-internal/dev-stack
make up     # anvil + graph-node + ipfs + postgres + subgraph deploy + devrelay
make smoke  # round-trip encrypt → on-chain → subgraph → decrypt
```

### 2. Start a local LLM

The launcher defaults to Unsloth Qwen3.5-9B (`UD-Q4_K_XL` quant, ~5.6 GB) — the same model the Gemini import used. First run downloads from HuggingFace; subsequent runs reuse the cache.

```bash
./scripts/start-llama-server.sh           # uses cached Qwen3.5-9B or auto-downloads
./scripts/local-llm-preflight.ts          # /v1/models + JSON extraction + p50 latency check
```

The launcher binds strictly to `127.0.0.1:8001` (matches the Gemini import config) with:
- **128K context** (`-c 131072`) — comfortably holds the 80-message / 40K-token chunk budget.
- **Unsloth-recommended non-thinking sampling**: temp 0.7, top_p 0.8, top_k 20, min_p 0.0.
- **Reasoning off** — Qwen3.5-9B's default mode. Thinking tokens are not emitted, so every output token goes toward the JSON extraction.
- **Model alias** `unsloth/Qwen3.5-9B` — matches the hard-coded model name in `totalreclaw-internal/e2e/gemini-import/import-local.ts`.

Override any of these via env: `LLAMA_PORT=9000 LLAMA_MODEL=/path/to/other.gguf ./scripts/start-llama-server.sh`.

### 3. Configure the plugin

Copy `dev-stack/.env.local.example` to `.env.local` and source it, or set these env vars in your OpenClaw session:

```bash
export TOTALRECLAW_SERVER_URL=http://127.0.0.1:8787
export TOTALRECLAW_CHAIN_ID=31337
export TOTALRECLAW_RPC_URL=http://127.0.0.1:8545
export TOTALRECLAW_ENTRYPOINT_ADDRESS=0x0000000071727De22E5E9d8BAf0edAc6f37da032
export TOTALRECLAW_DATA_EDGE_ADDRESS=0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca
export OPENAI_BASE_URL=http://127.0.0.1:8001/v1
export OPENAI_API_KEY=local
export OPENAI_MODEL=unsloth/Qwen3.5-9B
export TOTALRECLAW_IMPORT_LOCAL_ONLY=1   # <-- hard guard
```

### 4. The `TOTALRECLAW_IMPORT_LOCAL_ONLY=1` guard

When this env var is set to `1`, `totalreclaw_import_from` refuses to run if the resolved LLM endpoint's hostname isn't `127.0.0.1`, `localhost`, or `::1`. A single misconfigured provider key (leftover `OPENAI_API_KEY` for cloud OpenAI, say) can't silently exfiltrate your prompts — the import path throws before any message leaves the process.

### 5. Run the import

Use a fresh, disposable BIP-39 recovery phrase for the vault (don't reuse your normal one). Dry-run first, inspect the profile + triage decisions, then run live:

> "Import my ChatGPT conversations from /path/to/conversations.json with dry run first"

During the run, you can verify hygiene with `lsof -iTCP -sTCP:ESTABLISHED` on the plugin process — it should only show connections to `127.0.0.1`.
