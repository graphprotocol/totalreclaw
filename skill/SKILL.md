---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext."
version: 1.6.0
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
metadata:
  openclaw:
    requires:
      env: []
      bins: []
    emoji: "🧠"
    os: ["macos", "linux", "windows"]
    keywords:
      - memory
      - e2ee
      - e2e-encryption
      - encryption
      - privacy
      - agent-memory
      - persistent-context
---

# TotalReclaw Skill

> Canonical install + account-setup flow lives at <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md> (OpenClaw) and <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md> (Hermes). When a user pastes one of those URLs in chat, fetch it and follow its agent-instructions section verbatim. The "Post-Install Setup" section below mirrors the same account-setup flow as a fallback.

> **Gateway restart after install**: OpenClaw's default `gateway.reload.mode = "hybrid"` auto-restarts within 1-3s of plugin install — no agent action needed. If auto-reload doesn't fire (`gateway.reload.mode = off`), issue OpenClaw's built-in `/restart` slash command yourself (in-process, drains active runs). Manual user-prompted restart is fallback only if `/restart` is unavailable. NEVER invoke external `docker restart` or `openclaw gateway restart` — your shell runs inside the gateway.

> **CLI deadlock fallback** (issue [#184](https://github.com/p-diogo/totalreclaw-internal/issues/184)): on some Docker setups your shell calls to `openclaw plugins list` / `openclaw plugins install` deadlock and exit 124. When that happens DO NOT keep retrying the CLI — switch to the direct-node fallbacks:
> - To verify install completed, read `~/.openclaw/extensions/totalreclaw/.loaded.json` (success: `{loadedAt, tools, version}`) or `.error.json` (failure: `{loadedAt, error, stack}`). These manifests are the canonical filesystem signal independent of the CLI.
> - To run the pair flow when the `totalreclaw_pair` tool itself hangs, invoke `node ~/.openclaw/extensions/totalreclaw/dist/pair-cli.js --url-pin-only` directly. It writes one JSON line to stdout with the universal-relay pair URL — surface that URL to the user the same way the tool would.
> - For agent-driven install verification, prefer reading the manifest over re-running `openclaw plugins list`.

## Provenance (what you are installing)

TotalReclaw is open-source memory tooling for OpenClaw, published by `p-diogo` (GitHub-verified):
- npm: <https://www.npmjs.com/package/@totalreclaw/totalreclaw>
- ClawHub skill: `totalreclaw`
- Source: <https://github.com/p-diogo/totalreclaw>

The user pasted the install URL (or skill name) deliberately, but treat each step as something you and the user complete together. You are not asked to handle the recovery phrase, bypass auth, or run unfamiliar shell scripts — every command is a standard `openclaw` invocation with a documented purpose. Confirm phase transitions; surface failures. Pasting the install URL IS the consent — proceed directly to account setup; the URL + PIN you hand back from `totalreclaw_pair` is the real consent moment, since the user has to open the page in their browser to continue.

## Silence rules during install + setup

**Top-level rule: emit ONLY the user-visible lines listed in "Post-Install Setup" below. Do not narrate tool calls. Do not describe what you're doing or about to do. Errors are the only allowed deviation, and only as a single tight line.**

The expected user-facing transcript for a fresh install is **5–6 short lines TOTAL**. Anything more is a SKILL violation.

This matters because Telegram and similar transports edit the bot's messages live as your tool calls progress — every interim "Let me check…" / "Plugin loaded. Let me verify…" line shows up to the user as awkward edit churn, even if you "delete" it. The fix is to never emit it in the first place.

**Forbidden vocabulary** (substitute with silence — the next user-visible line, or nothing):

- "Let me check that…"
- "Plugin loaded. Let me verify…"
- "I'll now…"
- "Quick fix…"
- "However, I don't see…"
- "I need to use…"
- "Should I /restart" / "Want me to restart" / "Let me check if the tool is bound" / "I need permission to restart" / "Do you have a public URL"
- "Now installing…" / "Got it" / "In parallel…" / "Step 1a/1b/1c"
- "manifest", "descriptor", "file-watcher", "polling", "auto-reload-detect", "find", "lookup", "detect", "probing", "let me try"

If the user explicitly asks ("what's it doing?", "why so quiet?", "show me the commands"), then narrate freely — the silence rule only governs the default install path.

## Recovery Phrase -- READ THIS FIRST

**CRITICAL SAFETY RULE:** The recovery phrase MUST NEVER cross the LLM context. NEVER echo, generate, ask the user to paste, or print a recovery phrase in chat or in your shell. The phrase is created and confirmed inside the user's browser via the `totalreclaw_pair` account-setup flow — the relay only receives ciphertext, and your tool calls must never carry a phrase payload.

If the user pastes a phrase in chat anyway: tell them it is compromised (the chat transcript and any tool stdout enter LLM context, which makes the phrase recoverable from logs and from any model that processes that turn) and call `totalreclaw_pair` with `mode=generate` to create a fresh wallet.

The recovery phrase is the user's ONLY identity in TotalReclaw. It derives all encryption keys. There is NO password reset, NO recovery mechanism, and NO support channel that can help if it is lost. The browser-side account-setup flow displays this warning automatically when a new phrase is generated.

**Do NOT** import a phrase the user already uses for a funded crypto wallet. TotalReclaw uses BIP-39 mnemonics (same format as crypto wallets), but a TotalReclaw phrase must be SEPARATE from any funded wallet. If the user tries to import an existing crypto-wallet phrase, STOP and warn them in chat to use a fresh phrase instead — but warn ABOUT the phrase, never echo any portion of it.

---

## Tools

### totalreclaw_remember

Store a new fact or preference in long-term memory.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| text | string | Yes | The fact or information to remember |
| type | string | No | Type of memory: `fact`, `preference`, `decision`, `episodic`, `goal`, `context`, or `summary`. Default: `fact` |
| importance | integer | No | Importance score 1-10. Default: auto-detected by LLM |

**Example:**
```json
{
  "text": "User prefers TypeScript over JavaScript for new projects",
  "type": "preference",
  "importance": 7
}
```

**Returns:**
```json
{
  "factId": "01234567-89ab-cdef-0123-456789abcdef",
  "status": "stored",
  "importance": 7,
  "encrypted": true
}
```

---

### totalreclaw_recall

Search and retrieve relevant memories from long-term storage.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Natural language query to search memories |
| k | integer | No | Number of results to return. Default: 8, Max: 20 |

**Example:**
```json
{
  "query": "What programming languages does the user prefer?",
  "k": 5
}
```

**Returns:**
```json
{
  "memories": [
    {
      "factId": "01234567-89ab-cdef-0123-456789abcdef",
      "factText": "User prefers TypeScript over JavaScript for new projects",
      "type": "preference",
      "importance": 7,
      "timestamp": "2026-02-22T10:30:00Z",
      "relevanceScore": 0.95
    }
  ],
  "totalCandidates": 47,
  "searchLatencyMs": 42
}
```

---

### totalreclaw_forget

Delete a specific fact from memory.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| factId | string | Yes | UUID of the fact to delete |

**Example:**
```json
{
  "factId": "01234567-89ab-cdef-0123-456789abcdef"
}
```

**Returns:**
```json
{
  "status": "deleted",
  "factId": "01234567-89ab-cdef-0123-456789abcdef",
  "tombstoneExpiry": "2026-03-24T00:00:00Z"
}
```

---

### totalreclaw_export

Export all stored memories in plaintext format.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| format | string | No | Export format: `json` or `markdown`. Default: `json` |

**Example:**
```json
{
  "format": "json"
}
```

**Returns (JSON format):**
```json
{
  "exportVersion": "0.3",
  "exportedAt": "2026-02-22T10:30:00Z",
  "totalFacts": 127,
  "facts": [
    {
      "id": "...",
      "factText": "...",
      "type": "preference",
      "importance": 7,
      "timestamp": "...",
      "entities": [...],
      "relations": [...]
    }
  ],
  "graph": {
    "entities": {...},
    "relations": [...]
  }
}
```

**Returns (Markdown format):**
```markdown
# TotalReclaw Export
Exported: 2026-02-22T10:30:00Z
Total Facts: 127

## Preferences
- User prefers TypeScript over JavaScript for new projects (importance: 7)

## Decisions
- User decided to use PostgreSQL for the main database (importance: 8)

...
```

---

### totalreclaw_status

Check subscription status and usage quota.

**Parameters:** None

**Example:**
```json
{}
```

**Returns:**
```json
{
  "tier": "Free",
  "writesUsed": 42,
  "writesLimit": 250,
  "resetsAt": "2026-04-01",
  "pricingUrl": "https://totalreclaw.xyz/pricing"
}
```

---

### totalreclaw_consolidate

Scan all stored memories and merge near-duplicates. Keeps the most important/recent version and removes redundant copies.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| dry_run | boolean | No | Preview consolidation without deleting. Default: `false` |

**Example:**
```json
{
  "dry_run": true
}
```

**Returns:**
```
Scanned 247 memories.
Found 12 cluster(s) with 18 duplicate(s).

Cluster 1: KEEP "User prefers TypeScript over JavaScript for new projects..."
  - REMOVE "User likes TypeScript more than JavaScript..." (ID: abc123)
Cluster 2: KEEP "Project uses PostgreSQL as the main database..."
  - REMOVE "The main database is PostgreSQL..." (ID: def456)
...

DRY RUN -- no memories were deleted. Run without dry_run to apply.
```

**Note:** Currently only available in centralized mode (not subgraph mode).

---

### totalreclaw_upgrade

Upgrade to TotalReclaw Pro for unlimited encrypted memories on Gnosis mainnet.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| *(none)* | | | The tool automatically uses the current wallet address |

**Example:**
```json
{}
```

**Returns:**
```json
{
  "checkout_url": "https://checkout.stripe.com/c/pay/...",
  "message": "Open this URL to upgrade to Pro: https://checkout.stripe.com/c/pay/..."
}
```

---

### totalreclaw_migrate

Migrate memories from testnet (Base Sepolia) to mainnet (Gnosis) after upgrading to Pro.

**When to use:** After a user successfully upgrades to Pro. Their memories are on the free-tier testnet and need to be copied to permanent mainnet storage.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| confirm | boolean | No | Set to `true` to execute the migration. Without it, returns a dry-run preview. Default: `false` |

**Example (dry-run):**
```json
{}
```

**Example (execute):**
```json
{
  "confirm": true
}
```

**Returns (dry-run):**
```json
{
  "mode": "dry_run",
  "testnet_facts": 47,
  "already_on_mainnet": 0,
  "to_migrate": 47,
  "message": "Found 47 facts to migrate from testnet to Gnosis mainnet. Call with confirm=true to proceed."
}
```

**Returns (executed):**
```json
{
  "mode": "executed",
  "testnet_facts": 47,
  "migrated": 47,
  "failed_batches": 0,
  "message": "Successfully migrated 47 memories from testnet to Gnosis mainnet."
}
```

**Safety:**
- Dry-run by default: call without `confirm=true` to preview what will be migrated
- Idempotent: re-running skips facts that already exist on mainnet (by content fingerprint)
- Testnet facts are never deleted (they remain as a backup)
- Handles partial failures: if a batch fails, re-run to retry (only unmigrated facts are sent)

---

### totalreclaw_import_from

Import memories from other AI memory tools into TotalReclaw.

**When to use:** User mentions migrating from Mem0, MCP Memory Server, ChatGPT, Claude, Gemini, or wants to import memories from another tool.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| source | string | Yes | Source system: `mem0`, `mcp-memory`, `chatgpt`, `claude`, `gemini`. Post-MVP: `memoclaw`, `generic-json`, `generic-csv` |
| api_key | string | No | API key for the source (Mem0). Used once, never stored. |
| source_user_id | string | No | User or agent ID in the source system |
| content | string | No | File content (JSON, JSONL, or CSV) -- for file-based sources |
| file_path | string | No | Path to a file on disk -- for file-based sources |
| namespace | string | No | Target namespace in TotalReclaw. Default: `imported` |
| dry_run | boolean | No | Preview without importing. Default: `false` |

**Best practice:** Always use `dry_run=true` first to preview, then confirm with the user before importing.

**Example -- import from Mem0 (API):**
```json
{
  "source": "mem0",
  "api_key": "m0-xxx",
  "source_user_id": "user-123",
  "dry_run": true
}
```

**Example -- import from MCP Memory Server (file):**
```json
{
  "source": "mcp-memory",
  "file_path": "~/.mcp-memory/memory.jsonl",
  "dry_run": true
}
```

**Example -- import from ChatGPT (memories text):**
```json
{
  "source": "chatgpt",
  "content": "User prefers dark mode\nUser works at Google\nUser lives in SF",
  "dry_run": true
}
```

**Example -- import from ChatGPT (conversations.json):**
```json
{
  "source": "chatgpt",
  "file_path": "~/Downloads/chatgpt-export/conversations.json",
  "dry_run": true
}
```

**Example -- import from Gemini (Google Takeout HTML):**
```json
{
  "source": "gemini",
  "file_path": "~/Downloads/Takeout/My Activity/Gemini Apps/My Activity.html",
  "dry_run": true
}
```

**Example -- import from Claude (memories text):**
```json
{
  "source": "claude",
  "content": "[2026-03-15] - User prefers TypeScript\nUser works at a startup in Berlin",
  "dry_run": true
}
```

**Returns (dry run):**
```json
{
  "success": true,
  "source": "mem0",
  "total_found": 47,
  "imported": 0,
  "skipped_duplicate": 0,
  "warnings": ["DRY RUN — no facts were imported."],
  "import_id": "import-1710000000000-abc1234"
}
```

**Returns (actual import):**
```json
{
  "success": true,
  "source": "mem0",
  "total_found": 47,
  "imported": 45,
  "skipped_duplicate": 2,
  "skipped_invalid": 0,
  "errors": [],
  "import_id": "import-1710000000000-abc1234",
  "duration_ms": 3200
}
```

Imported facts are tagged with `import_source:{source}` for easy filtering. The import is idempotent -- running it twice will not create duplicates (content fingerprint dedup).

### totalreclaw_import_batch

Process one batch of a large conversation import. Call repeatedly with increasing offset.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| source | string | Yes | `gemini`, `chatgpt`, or `claude` |
| file_path | string | No | Path to the source file |
| content | string | No | File content (for text sources) |
| offset | number | No | Starting chunk index (default 0) |
| batch_size | number | No | Chunks per call (default 25) |

**When to use:** For large imports (>50 chunks as reported by dry-run). For smaller imports, `totalreclaw_import_from` handles everything in one call.

**Import workflow (for the agent):**

When the user asks to import conversation history:

1. ALWAYS call `totalreclaw_import_from` with `dry_run=true` first
2. Show the estimate to the user (conversations, estimated facts, time)
3. Ask for confirmation
4. If confirmed AND total_chunks <= 50: call `totalreclaw_import_from` without dry_run
5. If confirmed AND total_chunks > 50: spawn a background worker:

   Use `sessions_spawn` with this task:
   ```
   sessions_spawn({
     task: "You are a TotalReclaw import worker. Do NOT call totalreclaw_setup — credentials are already configured. Just process the file at [FILE_PATH] using totalreclaw_import_batch with source=[SOURCE]. Start at offset=0 with batch_size=25. After each batch completes, immediately call the next batch with offset incremented by 25. Continue until the response contains is_complete=true. After all batches complete, report the total facts stored.",
     label: "[SOURCE] Import",
     runTimeoutSeconds: 0
   })
   ```

   Then tell the user: "I'm importing your [SOURCE] history in the background.
   You can keep chatting normally. Ask 'how's my import going?' for a status update."

6. When the user asks about import progress: use `sessions_history` on the
   spawned session to read the latest batch result and report progress.

---

## Post-Install Setup

If `totalreclaw_*` tools are bound to your toolset, run these steps to set up the user's TotalReclaw account. If they are NOT bound, the plugin isn't installed yet — follow the URL-driven install flow at <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md> first. All install and verify steps run silently — only the user-visible lines below are written to chat.

### User-visible prose (THIS IS ALL YOU SAY)

Speak only these lines, in order. Substitute the bracketed placeholders. The user already consented by pasting "Install TotalReclaw" + the canonical URL — proceed straight to account setup. The URL + PIN handed back from `totalreclaw_pair` is the real consent moment: surfacing them verbatim is what gives the user the chance to opt out by simply not opening the page.

1. (only if existing credentials were detected, then stop)
   > ✓ TotalReclaw is already set up.
2. (account-setup URL + PIN, immediately after `totalreclaw_pair` returns)
   > Open &lt;url&gt; in your browser. Enter PIN &lt;pin&gt;. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
3. (done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.

### Steps

1. **Check for existing credentials (silent unless found).** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit user-visible line 1 and stop.

2. **Set up the user's TotalReclaw account via browser (emit user-visible line 2).** Call `totalreclaw_pair` silently. It returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}`. Then emit user-visible line 2 with `<url>` and `<pin>` substituted. Do not pre-narrate ("I'll walk you through…", "Setting up your account now…", "Ready to set up your account…") — line 2 itself is the only thing the user needs to see.

   Render the QR if your transport supports image attachments (decode `qr_png_b64` to bytes and attach), or emit `qr_unicode` inline for terminal-only transports. The QR encodes only the URL — the PIN is a separate dual-channel confirmation, never bake it into the QR.

3. **Verify and confirm (silent until line 3).** After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — call `totalreclaw_pair` again silently and resend line 2 once. If present, emit user-visible line 3 and stop.

**Never** call `totalreclaw_setup` or any phrase-touching CLI from your shell — those leak the phrase into LLM context. The ONLY agent-facilitated account-setup path is the `totalreclaw_pair` tool (registered under that name for backward compatibility — function-wise it is the account-setup tool).

---

## Description

TotalReclaw is an end-to-end encrypted memory vault for AI agents. Think of it as a "password manager for AI memory" that provides:

1. **End-to-End Encryption** - All memories are encrypted client-side using XChaCha20-Poly1305. The server never sees plaintext data.

2. **Portable Memory** - Export all data as plaintext with one command. No vendor lock-in.

3. **Universal Compatibility** - Works across any MCP-compatible AI agent, not just OpenClaw.

4. **Intelligent Extraction** - Automatically extracts facts, preferences, decisions with reasoning, project context, conversation summaries, and goals from conversations.

5. **Smart Decay** - Important memories persist; trivial ones fade over time using a decay algorithm.

6. **Graph-Based** - Maintains entity relationships for multi-hop reasoning.

---

## Memory Types

TotalReclaw extracts and stores seven types of memories:

| Type | Description | Example |
|------|-------------|---------|
| Fact | Objective information about you | "Lives in Lisbon, Portugal" |
| Preference | Your likes, dislikes, choices | "Prefers dark mode in all applications" |
| Decision | Choices you made WITH reasoning | "Chose PostgreSQL because data is relational and needs ACID" |
| Episodic | Notable events or experiences | "Deployed v1.0 to production on March 15" |
| Goal | Your objectives or plans | "Wants to launch public beta by end of Q1" |
| Context | Active project/task context | "Working on TotalReclaw v1.2, staging on Base Sepolia" |
| Summary | Key outcomes from discussions | "Agreed to use phased rollout for mainnet migration" |

Decisions and context are treated as high-value memories (importance >= 7) because they provide the most useful information for future conversations.

---

## Instructions for the LLM

### IMPORTANT: Do Not Write Cleartext Memory Files

TotalReclaw handles all memory storage with end-to-end encryption. **Do NOT write facts, preferences, or decisions to MEMORY.md or memory/*.md files.** All memories are stored encrypted on-chain and recalled automatically — writing cleartext files defeats the E2EE guarantee.

If you need to store a memory, use the `totalreclaw_remember` tool. If you need to recall memories, use `totalreclaw_recall`.

### Billing Awareness

TotalReclaw has a free tier (500 memories/month, unlimited reads). The plugin monitors quota usage automatically:
- If usage exceeds 80%, a warning is injected into your context at conversation start
- If a write fails with quota exceeded (403), inform the user and suggest visiting https://totalreclaw.xyz/pricing
- Use `totalreclaw_status` when the user asks about their subscription, quota, or billing
- After upgrading, the new tier features may take up to 2 hours to fully activate on your client due to billing cache. If you experience issues, restart your agent.

### When to Use Each Tool

#### totalreclaw_remember

Use when:
- The user explicitly asks you to remember something ("remember that...", "note that...", "don't forget...")
- You detect a significant preference, decision, or fact that will be useful in future conversations
- The user corrects or updates previous information about themselves
- You observe important context about the user's work, projects, or preferences

Do NOT use for:
- Temporary information relevant only to the current conversation
- Information the user explicitly says is temporary
- Generic knowledge that isn't user-specific

#### totalreclaw_recall

Use when:
- The user asks about their past preferences, decisions, or history
- You need context about the user's projects, tools, or working style
- The user asks "do you remember..." or "what did I tell you about..."
- You're unsure about a user preference and want to check before making assumptions
- Starting a new conversation to load relevant context

Do NOT use for:
- Every single message (use sparingly, max once per conversation start or when explicitly relevant)
- General knowledge questions unrelated to the user

#### totalreclaw_forget

Use when:
- The user explicitly asks you to forget something ("forget that...", "delete that memory...")
- The user indicates information is outdated or incorrect and should be removed
- The user requests a clean slate for a specific topic

#### totalreclaw_upgrade

Use when:
- The user hits their free tier memory limit (403 quota exceeded)
- The user asks about upgrading, pricing, or getting Pro
- After a `totalreclaw_status` call shows the user is on the free tier and they want more

#### totalreclaw_migrate

Use when:
- The user has just upgraded to Pro and their memories are still on testnet
- The user asks about migrating testnet memories to mainnet
- After a successful `totalreclaw_upgrade`, proactively offer migration

Always do a dry-run first (call without `confirm=true`), show the preview, then ask the user to confirm before executing.

#### totalreclaw_export

Use when:
- The user asks to export, backup, or download their memory data
- The user wants to see everything you know about them
- The user is migrating to another system

#### totalreclaw_import_from

Use when:
- The user mentions importing from Gemini, ChatGPT, Claude, or another AI tool
- The user provides an export file or mentions Google Takeout
- The user asks about migrating memories from another service
- The user mentions migrating from Mem0, MCP Memory Server, or another AI memory tool
- The user wants to import memories from a file or API
- The user asks to consolidate memories from multiple tools

Always run with `dry_run=true` first and show the preview before importing. API keys are used in-memory only and never stored.

#### totalreclaw_consolidate

Use when:
- The user asks to clean up or deduplicate their memories
- The user mentions having too many similar memories
- After a large import to merge near-duplicates

Always run with `dry_run=true` first to preview which memories will be merged, then confirm with the user before running without dry_run.

---

### Best Practices

1. **Atomic Facts Only**: Each memory should be a single, atomic piece of information.
   - Good: "User prefers dark mode in all editors"
   - Bad: "User likes dark mode, uses VS Code, and works at Google"

2. **Importance Scoring**:
   - 1-3: Trivial, unlikely to matter (small talk, pleasantries)
   - 4-6: Useful context (tool preferences, working style)
   - 7-8: Important (key decisions with reasoning, project context, major preferences)
   - 9-10: Critical (core values, non-negotiables, safety info)

3. **Search Before Storing**: Always recall similar memories before storing new ones to avoid duplicates.

4. **Respect User Privacy**: Never store sensitive information (passwords, API keys, personal secrets) even if requested.

5. **Prefer NOOP**: When in doubt about whether to store something, prefer not storing it. Memory pollution is worse than missing a minor fact.

---

## Extraction Prompts (Mem0-Style)

TotalReclaw uses a Mem0-style extraction pattern with four possible actions:

### Actions

| Action | Description | When to Use |
|--------|-------------|-------------|
| ADD | Store as new memory | No similar memory exists |
| UPDATE | Modify existing memory | New info refines/clarifies existing |
| DELETE | Remove existing memory | New info contradicts existing |
| NOOP | Do nothing | Already captured or not worth storing |

---

### Pre-Compaction Extraction

Triggered before OpenClaw's context compaction (typically every few hours in long sessions).

**System Prompt:**

```
You are a memory extraction engine for an AI assistant. Your job is to analyze conversations and extract structured, atomic facts that should be remembered long-term.

## Extraction Guidelines

1. **Atomicity**: Each fact should be a single, self-contained piece of information
   - GOOD: "User chose PostgreSQL because the data model is relational and needs ACID"
   - BAD: "User likes TypeScript, uses VS Code, and works at Google"

2. **Types**:
   - **fact**: Objective information about the user/world
   - **preference**: User's likes, dislikes, or preferences
   - **decision**: Choices WITH reasoning ("chose X because Y")
   - **episodic**: Event-based memories (what happened when)
   - **goal**: User's objectives or targets
   - **context**: Active project/task context (what the user is working on, versions, environments)
   - **summary**: Key outcome or conclusion from a discussion

3. **Importance Scoring (1-10)**:
   - 1-3: Trivial, unlikely to matter (small talk, pleasantries)
   - 4-6: Useful context (tool preferences, working style)
   - 7-8: Important (key decisions with reasoning, project context, major preferences)
   - 9-10: Critical (core values, non-negotiables, safety info)

4. **Confidence (0-1)**:
   - How certain are you that this is accurate and worth storing?

5. **Extraction quality cues**:
   - Decisions: ALWAYS include reasoning. "Chose X" alone is low value.
   - Context: Include version numbers, environments, status ("v1.2", "staging", "private beta")
   - Summaries: Only when a conversation reaches a clear conclusion or agreement
   - Facts: Prefer specific over vague

6. **Entities**: Extract named entities (people, projects, tools, concepts)
   - Use stable IDs: hash of name+type (e.g., "typescript-tool")
   - Types: person, project, tool, preference, concept, location, etc.

7. **Relations**: Extract relationships between entities
   - Common predicates: prefers, uses, works_on, decided_to_use, dislikes, etc.

8. **Actions (Mem0 pattern)**:
   - **ADD**: New fact, no conflict with existing memories
   - **UPDATE**: Modifies or refines an existing fact (provide existingFactId)
   - **DELETE**: Contradicts and replaces an existing fact
   - **NOOP**: Not worth storing or already captured
```

**User Prompt Template:**

```
## Task: Pre-Compaction Memory Extraction

You are reviewing the last 20 turns of conversation before they are compacted. Extract ALL valuable long-term memories.

## Conversation History (last 20 turns):
{{CONVERSATION_HISTORY}}

## Existing Memories (for deduplication):
{{EXISTING_MEMORIES}}

## Instructions:
1. Review each turn carefully for extractable information
2. Extract facts, preferences, decisions (with reasoning), episodic memories, goals, project context, and conversation summaries
3. For each fact, determine if it's NEW (ADD), modifies existing (UPDATE), contradicts existing (DELETE), or is redundant (NOOP)
4. Score importance based on long-term relevance
5. Extract entities and relations

## Output Format:
Return a JSON object with:
{
  "facts": [
    {
      "factText": "string (max 512 chars)",
      "type": "fact|preference|decision|episodic|goal|context|summary",
      "importance": 1-10,
      "confidence": 0-1,
      "action": "ADD|UPDATE|DELETE|NOOP",
      "existingFactId": "string (if UPDATE/DELETE)",
      "entities": [{"id": "...", "name": "...", "type": "..."}],
      "relations": [{"subjectId": "...", "predicate": "...", "objectId": "...", "confidence": 0-1}]
    }
  ]
}

Focus on quality over quantity. Better to have 5 highly accurate facts than 20 noisy ones.
```

---

### Post-Turn Extraction

Triggered every N turns (configurable, default: 5) for lightweight extraction.

**User Prompt Template:**

```
## Task: Quick Turn Extraction

You are doing a lightweight extraction after a few turns. Focus ONLY on high-importance items.

## Recent Turns (last 3):
{{CONVERSATION_HISTORY}}

## Existing Memories (top matches):
{{EXISTING_MEMORIES}}

## Instructions:
1. Extract ONLY items with importance >= 7 (critical preferences, key decisions)
2. Skip trivial information - this is a quick pass
3. Use ADD/UPDATE/DELETE/NOOP appropriately
4. Be aggressive about NOOP for low-value content

## Output Format:
Return a JSON object matching the extraction schema.

Remember: Less is more. Only extract what truly matters.
```

---

### Explicit Command Detection

Detect when the user explicitly requests memory storage.

**Trigger Patterns (regex + LLM classification):**

```
# Explicit memory commands
"remember that..."
"don't forget..."
"note that..."
"I prefer..."
"for future reference..."
"make a note..."
"store this..."
"keep in mind..."

# Explicit forget commands
"forget about..."
"delete that memory..."
"remove that from memory..."
"stop remembering..."
```

**User Prompt Template:**

```
## Task: Explicit Memory Storage

The user has explicitly requested to remember something. This is a HIGH PRIORITY extraction.

## User's Explicit Request:
{{USER_REQUEST}}

## Conversation Context:
{{CONVERSATION_CONTEXT}}

## Instructions:
1. Parse what the user wants remembered
2. Boost importance by +1 (explicit requests matter more)
3. Extract as atomic fact(s) with appropriate type
4. Check against existing memories for UPDATE/DELETE
5. Set confidence HIGH (user explicitly wants this stored)

## Output Format:
Return a JSON object matching the extraction schema.

This is user-initiated storage - ensure accuracy and capture their intent precisely.
```

---

### Deduplication Judge

Used to determine ADD vs UPDATE vs DELETE vs NOOP for each extracted fact.

**System Prompt:**

```
You are a memory deduplication judge. Your job is to determine if a new fact should be added as new, update an existing fact, delete/replace an existing fact, or be ignored as redundant.

## Decision Rules:

1. **ADD**: The fact is genuinely new information not covered by existing memories
2. **UPDATE**: The fact refines, clarifies, or partially modifies an existing fact
3. **DELETE**: The fact directly contradicts an existing fact and should replace it
4. **NOOP**: The fact is already fully captured by existing memories

Be strict about NOOP - if the information is essentially the same, mark it as NOOP.
```

**User Prompt Template:**

```
## New Fact to Evaluate:
{{NEW_FACT}}

## Similar Existing Facts:
{{EXISTING_FACTS}}

## Instructions:
1. Compare the new fact against each existing fact
2. Determine the appropriate action (ADD/UPDATE/DELETE/NOOP)
3. If UPDATE or DELETE, identify which existing fact to modify
4. Provide your confidence (0-1) and reasoning

## Output Format:
{
  "decision": "ADD|UPDATE|DELETE|NOOP",
  "existingFactId": "string (if UPDATE/DELETE)",
  "confidence": 0-1,
  "reasoning": "string"
}
```

---

## Configuration

Default configuration values:

| Key | Default | Description |
|-----|---------|-------------|
| `serverUrl` | `https://api.totalreclaw.xyz` (stable releases) / `https://api-staging.totalreclaw.xyz` (RC releases) | TotalReclaw server URL. Stable artifacts bake the production URL; RC artifacts bake the staging URL (no SLA, may be wiped). User env `TOTALRECLAW_SERVER_URL=...` always wins. Do not change unless self-hosting. (See PR #165 for the codified RC=staging / stable=production rule.) |
| `autoExtractEveryTurns` | `3` | Turns between automatic extractions |
| `minImportanceForAutoStore` | `6` | Minimum importance to auto-store |
| `maxMemoriesInContext` | `8` | Maximum memories to inject into context |
| `forgetThreshold` | `0.3` | Decay score threshold for eviction |
| `decayHalfLifeDays` | `30` | Memory decay half-life in days |

### Memory Consolidation Configuration

Environment variables for controlling near-duplicate detection and consolidation:

| Env Var | Default | Description |
|---------|---------|-------------|
| `TOTALRECLAW_STORE_DEDUP` | `true` | Enable/disable store-time near-duplicate detection |
| `TOTALRECLAW_STORE_DEDUP_THRESHOLD` | `0.85` | Cosine similarity threshold for store-time dedup (0-1) |
| `TOTALRECLAW_CONSOLIDATION_THRESHOLD` | `0.88` | Cosine similarity threshold for bulk consolidation (0-1) |

---

## Privacy & Security

- **End-to-End Encrypted**: All encryption happens client-side. The server never sees your data.
- **Recovery Phrase**: Never sent to the server. Used only for key derivation (Argon2id).
- **Export Portability**: Full plaintext export available anytime.
- **Tombstone Recovery**: Deleted memories can be recovered within 30 days.

---

## Lifecycle Hooks

TotalReclaw integrates with OpenClaw through three lifecycle hooks:

| Hook | Priority | Description |
|------|----------|-------------|
| `before_agent_start` | 10 | Retrieve relevant memories before agent processes message |
| `agent_end` | 90 | Extract and store facts after agent completes turn |
| `pre_compaction` | 5 | Full memory flush before context compaction |

---

## Example Usage

### Storing a preference

```json
// Tool call
{
  "tool": "totalreclaw_remember",
  "params": {
    "text": "User prefers functional programming over OOP",
    "type": "preference",
    "importance": 6
  }
}

// Response
{
  "factId": "abc123",
  "status": "stored"
}
```

### Recalling memories

```json
// Tool call
{
  "tool": "totalreclaw_recall",
  "params": {
    "query": "programming preferences",
    "k": 5
  }
}

// Response
{
  "memories": [
    {
      "factId": "abc123",
      "factText": "User prefers functional programming over OOP",
      "type": "preference",
      "importance": 6,
      "relevanceScore": 0.92
    }
  ]
}
```

### Forgetting a memory

```json
// Tool call
{
  "tool": "totalreclaw_forget",
  "params": {
    "factId": "abc123"
  }
}

// Response
{
  "status": "deleted",
  "factId": "abc123"
}
```
