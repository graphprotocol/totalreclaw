# TotalReclaw Skill for NanoClaw

End-to-end encrypted memory for NanoClaw agents, powered by the `@totalreclaw/mcp-server`.

## Setup

### Step 1: Recovery phrase

The **recovery phrase** is a 12-word BIP-39 phrase that derives all encryption keys client-side. TotalReclaw never sends it to the server.

- **New user:** Generate a 12-word BIP-39 recovery phrase. Save it securely -- it is the only way to recover your memories.
- **Returning user:** Use your existing recovery phrase to restore memories on a new device.

### Step 2: Configure environment

Set these in your NanoClaw deployment (Docker env, `.env` file, or platform config):

```bash
TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
# Managed service is the default -- set TOTALRECLAW_SELF_HOSTED=true only for self-hosted mode
```

> **Note:** The server URL defaults to `https://api.totalreclaw.xyz` (the managed service). You only need to set `TOTALRECLAW_SERVER_URL` if you are running a self-hosted server.

The NanoClaw agent-runner automatically spawns the MCP server with these variables.

### Step 3: Verify

Ask the agent: *"Do you have access to TotalReclaw memory tools?"* It should confirm access to `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, `totalreclaw_export`, `totalreclaw_status`, `totalreclaw_upgrade`, `totalreclaw_import_from`, and `totalreclaw_consolidate`.

### Step 4: Free tier

After setup, the agent is on the free tier: 500 memories per month, unlimited reads and searches. The skill warns automatically when quota usage exceeds 80%. For unlimited memories, the user can upgrade via the `totalreclaw_upgrade` tool or visit https://totalreclaw.xyz/pricing.

---

## How It Works

All encryption happens inside the MCP server process -- the TotalReclaw server only sees ciphertext and hashed tokens:

- Facts encrypted with XChaCha20-Poly1305 before leaving the container
- Search uses blind indices (SHA-256 hashes), not plaintext
- Recovery phrase derives all keys via BIP-39 seed + HKDF (managed service) or Argon2id + HKDF (self-hosted)
- With the managed service, encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

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

---

### totalreclaw_recall

Search and retrieve relevant memories from long-term storage.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Natural language query to search memories |
| k | integer | No | Number of results to return. Default: 8, Max: 20 |

---

### totalreclaw_forget

Delete a specific fact from memory.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| factId | string | Yes | UUID of the fact to delete |

---

### totalreclaw_export

Export all stored memories in plaintext format.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| format | string | No | Export format: `json` or `markdown`. Default: `json` |

---

### totalreclaw_status

Check subscription status and usage quota.

**Parameters:** None

---

### totalreclaw_upgrade

Upgrade to TotalReclaw Pro for unlimited encrypted memories on Gnosis mainnet.

**Parameters:** None (uses the current wallet address automatically)

**Returns:** A Stripe checkout URL. Share it with the user to complete the upgrade.

---

### totalreclaw_import_from

Import memories from other AI memory tools into TotalReclaw.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| source | string | Yes | Source system: `mem0`, `mcp-memory`, `chatgpt`, `claude` |
| api_key | string | No | API key for the source (Mem0). Used once, never stored. |
| source_user_id | string | No | User or agent ID in the source system |
| content | string | No | File content (JSON, JSONL, or CSV) for file-based sources |
| file_path | string | No | Path to a file on disk for file-based sources |
| namespace | string | No | Target namespace in TotalReclaw. Default: `imported` |
| dry_run | boolean | No | Preview without importing. Default: `false` |

Always run with `dry_run=true` first and show the preview before importing. API keys are used in-memory only and never stored.

---

### totalreclaw_consolidate

Scan all stored memories and merge near-duplicates. Keeps the most important/recent version and removes redundant copies.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| dry_run | boolean | No | Preview consolidation without deleting. Default: `false` |

**Note:** Currently only available in self-hosted mode (not managed service).

---

## Memory Types

TotalReclaw extracts and stores seven types of memories:

| Type | Description | Example |
|------|-------------|---------|
| Fact | Objective information about the user | "Lives in Lisbon, Portugal" |
| Preference | Likes, dislikes, choices | "Prefers dark mode in all applications" |
| Decision | Choices made WITH reasoning | "Chose PostgreSQL because data is relational and needs ACID" |
| Episodic | Notable events or experiences | "Deployed v1.0 to production on March 15" |
| Goal | Objectives or plans | "Wants to launch public beta by end of Q1" |
| Context | Active project/task context | "Working on TotalReclaw v1.2, staging on Base Sepolia" |
| Summary | Key outcomes from discussions | "Agreed to use phased rollout for mainnet migration" |

Decisions and context are treated as high-value memories (importance >= 7) because they provide the most useful information for future conversations.

---

## Hooks

NanoClaw integrates TotalReclaw through three lifecycle hooks in the agent-runner:

| Hook | Description |
|------|-------------|
| `before-agent-start` | Retrieves relevant memories before processing user message. Checks billing status and injects quota warnings if usage exceeds 80%. |
| `agent-end` | Extracts and stores facts every 3 turns (configurable). Handles 403/quota errors by invalidating the billing cache. |
| `pre-compact` | Comprehensive extraction before context truncation. Also handles 403/quota errors. |

---

## Billing and Quota

TotalReclaw has a free tier (500 memories/month, unlimited reads). The skill monitors quota usage automatically:

- At conversation start (`before-agent-start`), billing status is fetched from the relay and cached for 2 hours
- If usage exceeds 80%, a warning is injected into the agent context
- If a write fails with quota exceeded (403), the billing cache is invalidated so the next conversation start re-fetches and warns the user
- Use `totalreclaw_status` when the user asks about their subscription, quota, or billing
- Use `totalreclaw_status` to check current tier and pricing. Use `totalreclaw_upgrade` to generate a Stripe checkout URL for Pro.

---

## Extraction Behavior

The skill automatically extracts facts from conversations using a Mem0-style pattern:

- **Interval:** Every 3 turns (configurable via `TOTALRECLAW_EXTRACT_INTERVAL`)
- **Cap:** Maximum 15 facts per extraction cycle
- **Minimum importance:** 6 (configurable via `TOTALRECLAW_MIN_IMPORTANCE`)
- **Dedup:** LLM-guided ADD/UPDATE/DELETE/NOOP actions to avoid duplicates
- **Pre-compaction:** Full extraction before context truncation (no importance filter)

### Extraction Actions

| Action | Description | When to Use |
|--------|-------------|-------------|
| ADD | Store as new memory | No similar memory exists |
| UPDATE | Modify existing memory | New info refines/clarifies existing |
| DELETE | Remove existing memory | New info contradicts existing |
| NOOP | Do nothing | Already captured or not worth storing |

---

## Instructions for the LLM

### IMPORTANT: Do Not Write Cleartext Memory Files

TotalReclaw handles all memory storage with end-to-end encryption. **Do NOT write facts, preferences, or decisions to MEMORY.md or memory/*.md files.** All memories are stored encrypted and recalled automatically -- writing cleartext files defeats the E2EE guarantee.

If you need to store a memory, use the `totalreclaw_remember` tool. If you need to recall memories, use `totalreclaw_recall`.

### When to Use Each Tool

#### totalreclaw_remember

Use when:
- The user explicitly asks to remember something ("remember that...", "note that...", "don't forget...")
- You detect a significant preference, decision, or fact useful in future conversations
- The user corrects or updates previous information about themselves

Do NOT use for:
- Temporary information relevant only to the current conversation
- Generic knowledge that is not user-specific

#### totalreclaw_recall

Use when:
- The user asks about past preferences, decisions, or history
- You need context about the user's projects, tools, or working style
- The user asks "do you remember..." or "what did I tell you about..."
- Starting a new conversation to load relevant context

Do NOT use for:
- Every single message (use sparingly)
- General knowledge questions unrelated to the user

#### totalreclaw_forget

Use when:
- The user explicitly asks to forget something
- Information is outdated or incorrect and should be removed

#### totalreclaw_upgrade

Use when:
- The user hits their free tier memory limit (403 quota exceeded)
- The user asks about upgrading, pricing, or getting Pro
- After a `totalreclaw_status` call shows the user is on the free tier and they want more

#### totalreclaw_export

Use when:
- The user asks to export, backup, or download their memory data
- The user wants to see everything stored about them
- The user is migrating to another system

#### totalreclaw_import_from

Use when:
- The user mentions migrating from Mem0, MCP Memory Server, ChatGPT, Claude, or another AI memory tool
- The user wants to import their ChatGPT memories or conversations, or Claude memory
- Always run with `dry_run=true` first and show the preview before importing

#### totalreclaw_consolidate

Use when:
- The user asks to clean up or deduplicate their memories
- After a large import to merge near-duplicates
- Always run with `dry_run=true` first to preview

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

## Namespace Mapping

The skill maps NanoClaw's `groupFolder` to TotalReclaw's `namespace`:
- `main` -> `main` namespace
- `work` -> `work` namespace
- `family` -> `family` namespace

This provides memory isolation between different contexts.

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word BIP-39 recovery phrase | Required |
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL (only needed for self-hosted) | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | Set to `true` to use your own server instead of the managed service | `false` |
| `TOTALRECLAW_CREDENTIALS_PATH` | Credential file location | `~/.totalreclaw/credentials.json` |
| `TOTALRECLAW_CACHE_PATH` | Encrypted cache file location | `~/.totalreclaw/cache.enc` |

The v1 env cleanup removed the following user-facing vars: `TOTALRECLAW_CHAIN_ID`
(chain is auto-detected from billing tier), `TOTALRECLAW_EMBEDDING_MODEL`,
`TOTALRECLAW_STORE_DEDUP`, `TOTALRECLAW_LLM_MODEL`,
`TOTALRECLAW_TAXONOMY_VERSION`, `TOTALRECLAW_CLAIM_FORMAT`,
`TOTALRECLAW_DIGEST_MODE`. Tuning knobs like `TOTALRECLAW_EXTRACT_INTERVAL`
and `TOTALRECLAW_MIN_IMPORTANCE` are now delivered via the relay billing
response; env-var fallbacks are still honoured for self-hosted deployments.
See [`docs/guides/env-vars-reference.md`](../docs/guides/env-vars-reference.md)
for the canonical list.

---

## Privacy and Security

- **End-to-End Encrypted**: All encryption happens in the MCP server process. The relay never sees plaintext data.
- **Recovery Phrase**: Never sent to the server. Used only for key derivation.
- **Export Portability**: Full plaintext export available anytime via `totalreclaw_export`.
- **Tombstone Recovery**: Deleted memories can be recovered within 30 days.
- **Secret Sanitization**: The agent-runner strips `TOTALRECLAW_RECOVERY_PHRASE` from Bash subprocess environments.
