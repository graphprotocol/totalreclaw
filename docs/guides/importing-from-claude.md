# Importing from Claude

TotalReclaw can import your Claude memory, so the facts Claude has learned about you are encrypted and available across any AI agent.

---

## Step 1: Export your Claude memories

1. Open [Claude](https://claude.ai)
2. Click your profile icon (bottom left) -> **Settings**
3. Go to **Memory**
4. Select all the text and copy it (Ctrl+A / Cmd+A, then Ctrl+C / Cmd+C)

Claude memories are plain text, typically one fact per line. Some entries may include date prefixes like `[2026-03-15] - User prefers TypeScript`.

---

## Step 2: Import into TotalReclaw

Tell your agent:

> "Import my Claude memories into TotalReclaw"

Then paste the copied text when asked. Or provide it directly:

> "Import these Claude memories: [paste text]"

The agent calls `totalreclaw_import_from` with `source: "claude"` and your pasted content.

---

## How it works

The Claude adapter:

1. Splits the text into individual lines (one memory per line)
2. Strips formatting markers (bullet points, numbered lists, date prefixes)
3. Classifies each memory by type (preference, decision, goal, context, or fact)
4. Preserves date prefixes as timestamps when available
5. Encrypts and stores each memory in your TotalReclaw vault

No LLM is required -- the classification uses simple pattern matching. Claude memories are already well-curated facts, so they import cleanly.

---

## Dry Run First

Always preview before importing:

> "Import my Claude memories with dry run first"

This parses your data and shows a preview of the first 10 facts without storing anything. Review and confirm before running the actual import.

---

## After Import

Imported memories behave identically to natively stored ones:

- **Searchable immediately** via `totalreclaw_recall`
- **Encrypted** with your recovery phrase (XChaCha20-Poly1305)
- **Tagged** with `import_source:claude` for filtering
- **Deduplicated** -- re-importing the same data skips duplicates automatically

---

## Example

Input (copied from Claude Settings -> Memory):

```
[2026-03-15] - User prefers TypeScript over JavaScript
User works at a startup in Berlin
- User decided to use PostgreSQL because the data is relational
User wants to learn machine learning this year
```

Result:

| Fact | Type | Timestamp |
|------|------|-----------|
| User prefers TypeScript over JavaScript | preference | 2026-03-15 |
| User works at a startup in Berlin | fact | -- |
| User decided to use PostgreSQL because the data is relational | decision | -- |
| User wants to learn machine learning this year | goal | -- |

All four facts are encrypted and stored in your TotalReclaw vault.

---

## Tips

- **Claude memories are already curated.** Unlike a full conversation export, these are atomic facts that import cleanly with no noise.
- **Date prefixes are preserved.** If Claude includes dates, they are stored as timestamps for chronological context.
- **Works from any platform.** Whether you use TotalReclaw via OpenClaw, Claude Desktop (MCP), or NanoClaw, the `totalreclaw_import_from` tool is available.
