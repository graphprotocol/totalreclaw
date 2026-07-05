# Importing from ChatGPT

TotalReclaw can import your ChatGPT conversation history and saved memories, so everything ChatGPT knows about you is encrypted and portable across any AI agent.

---

## Two Import Methods

ChatGPT stores your data in two places. You can import from either or both.

| Method | What it contains | Best for |
|--------|-----------------|----------|
| **ChatGPT Memories** (recommended) | Pre-curated facts ChatGPT saved about you | Quick import, high-quality facts |
| **conversations.json** | Full conversation export with all messages | Comprehensive extraction (slower, LLM-driven) |

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

The agent calls `totalreclaw_import_from` with `source: "chatgpt"` and your pasted content. Each line is run through an LLM extraction pass that classifies it and normalises it into an atomic fact, then encrypted into your TotalReclaw vault.

### What to expect

ChatGPT memories are already curated facts (e.g., "User prefers dark mode", "User works at Acme Corp"), so they import cleanly with minimal noise. A typical import of 50-100 memories takes 1-2 minutes — the bottleneck is the LLM extraction pass, not network or storage.

---

## Method 2: Import conversations.json

The full data export contains every conversation you have had with ChatGPT. TotalReclaw uses **LLM extraction** to identify facts, preferences, decisions, and other personal context from your conversations.

### Step 1: Export your ChatGPT data

1. Open [ChatGPT](https://chatgpt.com)
2. Click your profile icon (bottom left) -> **Settings**
3. Go to **Data Controls**
4. Click **Export data** -> **Confirm export**
5. ChatGPT sends a download link to your email (may take a few minutes to hours)
6. Download the archive — **no need to unzip it**

### Step 2: Import into TotalReclaw

Tell your agent:

> "Import my ChatGPT export from /path/to/chatgpt-export.zip"

The zip works as-is: TotalReclaw reads the conversation files inside it directly (recent exports split them across `conversations-000.json`, `conversations-001.json`, ...). An unpacked export folder or a single `conversations.json` works too. Small exports can also be pasted as JSON content, but for anything sizeable the file path is recommended (drag-and-drop is the preferred path on OpenClaw + Hermes).

The agent calls `totalreclaw_import_from` with `source: "chatgpt"` and the file path or content.

### How extraction works

TotalReclaw uses **LLM extraction** (not pattern matching) to identify facts from your conversation history. The pipeline is:

1. **Parse**: walk each conversation's mapping tree along the canonical thread (the messages you actually kept — edited or regenerated drafts are skipped) and pull user + assistant messages with their original timestamps. Both roles are included because the assistant's response often clarifies what the user meant. Each ChatGPT conversation becomes one session in your vault, with its own summary card (Crystal), so imported memory stays organized by conversation instead of arriving as loose facts.
2. **Chunk**: split each conversation into batches of ~20 messages (`CHUNK_SIZE`). Large conversations become multiple chunks (`part 1/N`, `part 2/N`, ...).
3. **Extract**: each chunk is run through an LLM (the host's LLM on MCP integrations; your gateway's LLM on OpenClaw; Hermes' configured LLM otherwise). The LLM identifies facts, preferences, decisions, goals, and context.
4. **Triage + profile**: extracted facts are deduplicated and merged into a coherent profile via the smart-import pipeline (see [`rust/totalreclaw-core/src/smart_import.rs`](https://github.com/p-diogo/totalreclaw/blob/main/rust/totalreclaw-core/src/smart_import.rs) for the schema).
5. **Encrypt + store**: each surviving fact is encrypted with your TotalReclaw key (XChaCha20-Poly1305), fingerprinted for dedup, and stored.

### What to expect

Because extraction is LLM-driven and runs per-chunk, processing time scales with the number of conversations:

- **Small exports** (<50 chunks, ~1000 messages): ~30 seconds
- **Typical exports** (a few hundred conversations): 5-15 minutes
- **Large exports** (3,000+ conversations): 10-60+ minutes depending on your LLM provider and rate limits

The import runs in the **background** on OpenClaw + Hermes — you can keep chatting with your agent during the import. Ask "how's the import?" anytime to see progress. On MCP integrations (Claude Desktop, Claude Code, Cursor, Windsurf), the host's LLM drives the extraction loop one chunk per turn, so you'll see chunks process in the foreground.

### Privacy disclosure

LLM extraction sends your conversation content **in cleartext** to your LLM provider. Before the import starts, your agent will show an explicit privacy disclosure naming the provider (e.g., "Anthropic via Claude on Hermes", "OpenAI via your OpenClaw gateway") and ask for explicit confirmation. TotalReclaw itself never sees plaintext — but the LLM doing the extraction does.

If you don't want to expose conversation content to your LLM, use **Method 1 (ChatGPT memories)** — the memories are already pre-curated short facts, and while they still go through LLM normalisation, the surface area is far smaller than full conversations.

---

## Dry Run First

Always preview before importing:

> "Import my ChatGPT memories with dry run first"

This parses your data and shows an estimate: `total_chunks`, `est_facts`, `est_minutes`, `est_completion_iso`, and the projected LLM cost. Review and confirm before running the actual import.

---

## File size limits

- **Hard cap**: 500 MB per import file. Larger files are rejected up-front with guidance to split.
- **RAM pre-flight**: imports require ~10× the file size in free RAM (JSON parse peak overhead). The tool checks available memory before loading and aborts with a clear error if the gateway / host doesn't have enough headroom. Workarounds: split the export, or upgrade your VPS.
- **Streaming parser**: not yet — phase 2 follow-up.

---

## Tier gating

- **Free tier**: one import lifetime (across all sources). Subsequent attempts are blocked with an upgrade prompt.
- **Pro tier** ($3.99/mo): unlimited imports. The pre-flight projects the LLM cost vs your subscription; soft warning if the projection exceeds $1.00, hard block above $5.00 (you can override with a flag).

---

## After Import

Imported memories behave identically to natively stored ones:

- **Searchable immediately** via `totalreclaw_recall`
- **Encrypted** with your recovery phrase (XChaCha20-Poly1305)
- **Tagged** with `import_source:chatgpt` for filtering
- **Deduplicated** -- re-importing the same data skips duplicates automatically (content fingerprint)

---

## Abort + resume

- **Abort**: say "stop the import" — the background loop exits at the next chunk boundary. Already-stored facts stay (dedup makes resume idempotent).
- **Resume**: re-run the import with the same file. State persists in `~/.totalreclaw/import-state/<import_id>.json`; the loop picks up from the last completed chunk. On OpenClaw + Hermes, restarts auto-resume if the previous run was less than 1 hour ago.

---

## Tips

- **Start with memories, not conversations.** ChatGPT memories are pre-curated and import cleanly with minimal LLM cost. The full conversation export is noisier and costs more LLM tokens.
- **Review after import.** Run `totalreclaw_recall` with a few queries to verify the imported facts make sense.
- **Use consolidate after large imports.** If you import from both methods, run `totalreclaw_consolidate` with `dry_run=true` to find and merge near-duplicates.
- **Curate manually if needed.** Use `totalreclaw_forget` to remove any imported facts that are outdated or incorrect.
