# Importing from Gemini

TotalReclaw can import your Gemini conversation history, so the personal facts, preferences, and decisions Google's AI has learned about you are encrypted and portable across any AI agent.

Google doesn't have a dedicated "memory" feature like ChatGPT or Claude, but your Gemini conversation history contains the same kind of personal context. TotalReclaw extracts it from the Google Takeout export.

---

## Step 1: Export from Google Takeout

1. Go to [takeout.google.com](https://takeout.google.com)
2. Click **Deselect all**
3. Scroll down and select **Gemini Apps** only
4. Click **Next step** -> **Create export**
5. Wait for the email with the download link (usually 1-5 minutes; can be longer for very active accounts)
6. Download and unzip the archive
7. Find the file at: `Takeout/My Activity/Gemini Apps/My Activity.html`

---

## Step 2: Import into TotalReclaw

Ask your agent:

> "Import my Gemini conversation history from ~/Downloads/Takeout/My Activity/Gemini Apps/My Activity.html"

Or, on OpenClaw + Hermes, just drag the HTML file into the chat — drag-and-drop is the preferred input path.

The agent calls `totalreclaw_import_from` with `source: "gemini"` and the file path.

The flow:

1. **Dry run first**: the tool returns an estimate — `conversations_found`, `estimated_facts`, `estimated_minutes`, projected LLM cost.
2. **Privacy disclosure**: the agent shows what will be sent to your LLM provider for extraction (see [Privacy disclosure](#privacy-disclosure) below) and asks for explicit confirmation.
3. **Background execution** (OpenClaw + Hermes): the actual extraction runs out-of-band. You can keep chatting with your agent and ask "how's the import?" any time.
4. **Foreground execution** (MCP hosts — Claude Desktop, Claude Code, Cursor, Windsurf): your host's LLM drives the extraction loop one chunk per turn.
5. **Completion notification**: when the import finishes, your agent injects a system message on your next turn — e.g., *"Import done. 234 memories stored, 18 dups skipped."*

---

## How extraction works

TotalReclaw uses **LLM extraction** to identify facts from your Gemini conversations:

1. **Parse**: read the Takeout HTML and pull out individual Gemini turns (user prompt + Gemini response) with their timestamps. Timestamps come from Google's `D MMM YYYY, HH:MM:SS TZ` format (e.g., `1 Apr 2026, 18:39:35 WEST`) and are normalised to ISO 8601.
2. **Sessionise**: group consecutive turns into pseudo-sessions, splitting whenever the gap between turns exceeds 30 minutes. This recovers a conversation structure Takeout doesn't preserve directly.
3. **Chunk**: split each session into batches of ~20 messages (`CHUNK_SIZE`).
4. **Extract**: each chunk is run through an LLM (the host's LLM on MCP integrations; your gateway's LLM on OpenClaw; Hermes' configured LLM otherwise). The LLM identifies facts, preferences, decisions, goals, and context. Generic Q&A turns (recipe lookups, product searches, weather) typically produce zero facts — only personal, long-term-valuable information is extracted.
5. **Triage + profile**: extracted facts are deduplicated and merged into a coherent profile via the smart-import pipeline.
6. **Encrypt + store**: each surviving fact is encrypted with your TotalReclaw key (XChaCha20-Poly1305), fingerprinted for dedup, and stored.

---

## Privacy disclosure

LLM extraction sends your Gemini conversation content **in cleartext** to your LLM provider. Before the import starts, your agent will show an explicit privacy disclosure naming the provider (e.g., "Anthropic via Claude on Hermes", "OpenAI via your OpenClaw gateway") and ask for explicit confirmation. TotalReclaw itself never sees plaintext — but the LLM doing the extraction does.

This is the same trade-off that applies to the `conversations.json` path for ChatGPT and is documented up-front. Inline "memory" sources like ChatGPT Memories or Claude memories have a smaller surface area because the data is already pre-curated short facts; full conversation imports (Gemini Takeout, ChatGPT conversations.json) involve the entire chat history.

---

## What gets imported

- ✅ Text conversations from Gemini Apps (web + mobile)
- ✅ Timestamps + session boundaries (30-minute gap heuristic)
- ❌ Attachments — images, PDFs, audio in the Gemini export are not imported. Only text turns.
- ❌ Gemini-in-Workspace activity that doesn't land in My Activity (e.g., Smart Reply suggestions)

---

## File size + RAM limits

- **Hard cap**: 500 MB per import file. Larger exports are rejected up-front with guidance to split.
- **RAM pre-flight**: imports require ~10× the file size in free RAM. The tool checks available memory before loading and aborts with a clear error if the gateway / host doesn't have enough headroom.
- **Large exports** (3,000+ conversations): expect 10-60+ minutes depending on your LLM provider and rate limits. Background execution on OpenClaw + Hermes means you can keep using your agent during the import.

---

## Tier gating

- **Free tier**: one import lifetime (across all sources — Gemini, ChatGPT, Claude, Mem0, MCP-Memory). Subsequent attempts are blocked with an upgrade prompt.
- **Pro tier** ($3.99/mo): unlimited imports. The pre-flight projects the LLM cost vs your subscription; soft warning if the projection exceeds $1.00, hard block above $5.00 (you can override with a flag).

---

## Dry Run First

Always preview before importing:

> "Import my Gemini history with dry run first"

This parses the HTML and shows the estimate (sessions found, chunks, estimated facts, time, projected LLM cost) without storing anything. Review and confirm before running the actual import.

---

## Abort + resume

- **Abort**: say "stop the import" — the background loop exits at the next chunk boundary. Already-stored facts stay (dedup makes resume idempotent).
- **Resume**: re-run the import with the same file. State persists in `~/.totalreclaw/import-state/<import_id>.json`; the loop picks up from the last completed chunk. On OpenClaw + Hermes, restarts auto-resume if the previous run was less than 1 hour ago.

---

## After Import

Imported memories behave identically to natively stored ones:

- **Searchable immediately** via `totalreclaw_recall`
- **Encrypted** with your recovery phrase (XChaCha20-Poly1305)
- **Tagged** with `import_source:gemini` for filtering
- **Deduplicated** -- re-importing the same export skips duplicates automatically (content fingerprint)

---

## Tips

- **Run consolidate after large imports.** Gemini conversations often re-surface the same fact multiple times. After importing, run `totalreclaw_consolidate` with `dry_run=true` to find and merge near-duplicates.
- **Spot-check after import.** Run `totalreclaw_recall` with a few topic queries to verify the imported facts make sense.
- **Combine with ChatGPT + Claude imports.** Dedup is automatic — facts already stored from another source won't be duplicated.
- **Curate manually if needed.** Use `totalreclaw_forget` to remove any imported facts that are outdated or incorrect.
