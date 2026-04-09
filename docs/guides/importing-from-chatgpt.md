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

The full data export contains every conversation you have had with ChatGPT. TotalReclaw scans the user messages for fact-like statements using pattern matching (no LLM required).

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

TotalReclaw scans your user messages (not assistant responses) for fact-like statements:

- **Personal info**: "I am...", "I work at...", "I live in...", "My name is..."
- **Preferences**: "I like...", "I prefer...", "I don't like...", "My favorite..."
- **Decisions**: "I decided...", "I chose...", "We agreed..."
- **Goals**: "I want to...", "I plan to...", "I'm working on..."

Messages that are purely questions, greetings, or very short responses are skipped.

### What to expect

The extraction is deliberately conservative -- it uses pattern matching, not an LLM, to keep things fast and deterministic. For a typical export with thousands of conversations, expect:

- **Processing time**: A few seconds (all local, no API calls)
- **Extraction rate**: Roughly 1-5% of user messages contain extractable facts
- **Quality**: Good for personal info and preferences; may miss subtle or implicit facts

If you want higher-quality extraction, use Method 1 (ChatGPT memories) instead -- ChatGPT has already done the hard work of identifying what matters.

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

- **Start with memories, not conversations.** ChatGPT memories are pre-curated and import cleanly. The full conversation export is noisier.
- **Review after import.** Run `totalreclaw_recall` with a few queries to verify the imported facts make sense.
- **Use consolidate after large imports.** If you import from both methods, run `totalreclaw_consolidate` with `dry_run=true` to find and merge near-duplicates.
- **Curate manually if needed.** Use `totalreclaw_forget` to remove any imported facts that are outdated or incorrect.
