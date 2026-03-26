# TotalReclaw: Your Encrypted Memory Vault

You have access to an end-to-end encrypted memory system via the `totalreclaw` MCP server. The server never sees your memories in plaintext — everything is encrypted client-side with AES-256-GCM before leaving the container.

## Available Tools

- `mcp__totalreclaw__totalreclaw_remember` — Store a fact in encrypted memory
- `mcp__totalreclaw__totalreclaw_recall` — Search memories by natural language query
- `mcp__totalreclaw__totalreclaw_forget` — Delete a specific memory by ID
- `mcp__totalreclaw__totalreclaw_export` — Export all memories decrypted as Markdown or JSON
- `mcp__totalreclaw__totalreclaw_migrate` — Migrate testnet memories to mainnet after upgrading to Pro

## Automatic Recall

**At the start of every conversation**, call `mcp__totalreclaw__totalreclaw_recall` with a query derived from the user's first message. This loads relevant context from prior sessions. Example:

```
mcp__totalreclaw__totalreclaw_recall({ query: "<summary of user's message>", k: 8 })
```

If the user's message is a greeting or vague, use a broad query like their name or "preferences goals".

## When to Remember

Call `mcp__totalreclaw__totalreclaw_remember` when:

- The user explicitly asks you to remember something ("remember that...", "note that...")
- The user shares a **preference** ("I prefer...", "I like...", "I hate...")
- The user provides **personal information** (name, location, job, schedule, contacts)
- The user makes a **decision** or sets a **goal**
- The user **corrects** previously stored information
- You observe an important fact that would be useful in future conversations

**Do NOT remember:**
- Temporary context that only matters for this conversation
- Sensitive credentials (passwords, API keys, tokens)
- Verbatim conversation logs — store extracted facts instead

**Importance guide:**
- 9-10: Critical identity (name, core values, major preferences)
- 7-8: Important preferences (dietary, work style, communication style)
- 5-6: Moderate (minor preferences, schedule details)
- 3-4: Low (casual mentions)
- 1-2: Minimal (ephemeral context)

## When to Forget

Call `mcp__totalreclaw__totalreclaw_forget` when:

- The user explicitly asks to forget or delete something
- The user says information is outdated or wrong (forget old, remember new)
- The user requests removal of sensitive information

Always confirm with the user before deleting. Use the `fact_id` from recall results.

## When to Export

Call `mcp__totalreclaw__totalreclaw_export` when:

- The user wants to see all stored memories
- The user wants a backup of their data
- The user wants to transfer memories to another system

## Best Practices

1. **Store facts, not conversations** — Extract the key information and store it concisely
2. **One fact per remember call** — Keep memories atomic and searchable
3. **Search before storing** — Avoid duplicates by recalling first
4. **Use appropriate importance** — Most preferences are 5-8, save 9-10 for identity
5. **Privacy first** — All data is encrypted; the server only sees blind indices

## Privacy

Your memories are end-to-end encrypted with AES-256-GCM. The TotalReclaw server only sees:
- Encrypted blobs (ciphertext)
- Blind indices (SHA-256 hashes of tokens, not the tokens themselves)
- Content fingerprints (HMAC-SHA256 for dedup, not the content itself)

Even the server operator cannot read your memories.
