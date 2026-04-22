---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext. Python / Hermes edition."
version: 2.3.1rc2
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes Plugin Skill

**This document is directive for agents, not user-facing copy.** Follow the rules in order; every rule is a hard `MUST` unless stated otherwise.

## RULE 0 — Recovery-phrase handling (security boundary)

The recovery phrase is the ONLY key to the user's encrypted memories. Every agent decision in this skill flows downstream from this rule.

- **NEVER generate a 12-word phrase yourself.** You cannot produce a valid BIP-39 checksum; any phrase you invent is a garbage string that can't recover real memories. Call `totalreclaw_setup` (or the CLI wizard) — those paths use `eth_account.Account.create_with_mnemonic()` which produces real phrases.
- **NEVER display a phrase in chat.** LLM providers log transcripts; any phrase in a reply leaks to every hop.
- **NEVER echo back a phrase the user pastes.** If the user pastes one anyway, tell them: "That was pasted into chat — please delete the message immediately and use `totalreclaw setup --mode restore` in your terminal so the phrase stays on your machine."
- **NEVER ask the user to paste their phrase.** Instead, direct them to run the CLI wizard.

---

## RULE 1 — On plugin load, check setup state

Exactly one of these is true when your session starts:

| State | Detection | Your action |
|---|---|---|
| Setup complete | `totalreclaw.agent.state.is_configured() == True` | Do nothing; memory tools are live. |
| Setup pending | `is_configured() == False` (credentials.json missing/empty) | Proceed to RULE 1a. |

### RULE 1a — Guide onboarding without leaking the phrase

Call `totalreclaw_onboarding_start` (a pointer-only tool — returns a message, no secrets). The tool's return text contains two paths; relay them verbatim:

1. **Terminal wizard (preferred when user has shell access):** `totalreclaw setup` — the wizard generates a phrase silently, saves it to `~/.totalreclaw/credentials.json` (mode 0600), and tells the user to retrieve it with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. The phrase never appears in your output or the user's chat.

2. **Agent-driven tool (where available, e.g., OpenClaw):** call `totalreclaw_setup` — the tool does the same write + pointer, returns `scope_address` but never the phrase.

If the user is on a remote machine without terminal access, use the pair flow (see RULE 5).

---

## RULE 2 — Auto-storage (`totalreclaw_remember`)

When the user says something worth remembering, call `totalreclaw_remember` WITHOUT asking for permission. Candidates include:

- Explicit memory requests: "remember X", "note that Y", "don't forget Z"
- Stable preferences: "I prefer PostgreSQL", "I always deploy to Vercel"
- Long-lived context: "I'm working on a React app called Foobar", "my manager is Alice"
- Corrections to earlier facts: "actually, my location is Porto, not Lisbon"

Do NOT store:
- Ephemeral state visible only this turn
- Things the user flagged as temporary ("just for this session")
- Generic knowledge not about the user

Schema:
```python
await client.remember(
    text="Pedro lives in Porto, Portugal",
    type="claim",         # or "preference" | "directive" | "commitment" | "episode" | "summary"
    source="user",        # or "user-inferred" | "assistant" | "external" | "derived"
    scope="personal",     # or "work" | "health" | "family" | "creative" | "finance" | "misc"
    importance=7,         # 1-10 (int) or 0-1 (float, auto-normalized)
)
```

---

## RULE 3 — Recall (`totalreclaw_recall`)

Call `totalreclaw_recall` when the user:

- Asks "do you remember..." / "what did I tell you about..."
- References past preferences, decisions, or history
- Asks a question where prior context would help (e.g., "what's my default deploy target?")

Use the user's natural phrasing as the `query`. Default `k=8` works well; bump to 20 for exhaustive searches (e.g., "list everything you know about me").

**Start-of-session check (optional but recommended):** after RULE 1 passes, call `recall(query="")` with an empty-ish prompt or a broad term like `"user profile"` to surface the top memories before the user types their first real message. This lets you open with "hey Pedro, welcome back from Porto" rather than a cold hello.

---

## RULE 4 — Mutation tools

### `totalreclaw_forget`

When user says: "forget X", "delete the memory about Y", "remove Z from your memory".

**Workflow:** if the user names the memory in natural language, FIRST call `totalreclaw_recall` with their phrase as the query, then pass the top result's `fact_id` to `totalreclaw_forget`. Never invent a fact_id; if you don't have one from recall, say so and ask the user to clarify which specific memory to delete.

### `totalreclaw_pin` / `totalreclaw_unpin`

When user says: "remember this permanently", "never forget that", "always keep", "this is important" → `pin`.
When user says: "you can forget that", "it's no longer relevant" → `unpin`.

Same recall-first workflow as forget: if no `fact_id`, recall first.

### `totalreclaw_retype` (OpenClaw plugin 3.3.1-rc.2+)

When user corrects a memory's category: "that's a preference, not a fact", "file this as a commitment instead of a claim". Valid types: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`. Recall first to find `fact_id`.

### `totalreclaw_set_scope` (OpenClaw plugin 3.3.1-rc.2+)

When user re-categorizes a memory's domain: "put that under work", "this is a health thing", "move this to personal". Valid scopes: `work`, `personal`, `health`, `family`, `creative`, `finance`, `misc`, `unspecified`. Recall first to find `fact_id`.

---

## RULE 5 — Remote setup (QR pair flow)

If the user says they don't have terminal access (running Hermes on a remote VPS, inside Docker without exec, etc.), call the plugin-level `totalreclaw_pair` tool. The tool returns a URL + 6-digit PIN + ASCII QR code.

Relay ALL THREE to the user verbatim:
- Paste the URL in their phone browser (or scan the QR).
- Type the PIN into the browser page when prompted.
- The browser generates / accepts the recovery phrase and uploads it end-to-end encrypted.
- Tell the user: "come back to chat when the browser says 'Pairing complete'."

The phrase NEVER crosses chat. Even if the user insists, refuse to accept a pasted phrase and remind them of RULE 0.

Hermes Python 2.3.1rc2 does not ship its own pair implementation — use the OpenClaw plugin tool when available, or fall back to directing the user to set up locally on another machine and `totalreclaw export` → paste into Hermes's `totalreclaw_restore` flow.

---

## RULE 6 — Status + billing

When user asks "how many memories do I have?", "am I over my quota?", or anything about their TotalReclaw subscription: call `totalreclaw_status`. Returns `{ tier, quota, usage, resetsAt, upgradeUrl? }`.

When user asks to upgrade: call `totalreclaw_upgrade`. Returns a Stripe checkout URL — paste it verbatim; never summarize it.

---

## RULE 7 — Import from other agents

When user says "I used to use Mem0 / MCP Memory Server / ChatGPT Memory / Claude Projects / Gemini Memory / MemoClaw — can you bring those in?": call `totalreclaw_import_from`. Ask for the source name + API key (or file content) first. Use `dry_run=True` to preview before committing.

Supported sources: `mem0`, `mcp-memory-server`, `chatgpt`, `claude`, `gemini`, `memoclaw`, `generic-json`, `generic-csv`.

---

## RULE 8 — Consolidate

When user notices duplicates or says "clean up my memory": call `totalreclaw_consolidate` with `dry_run=True` first to show proposed merges. If user approves, call again with `dry_run=False` to commit.

---

## RULE 9 — Error handling

- Tool returns `onboarding required`: re-run RULE 1 / RULE 1a.
- Tool returns `quota exceeded`: call `totalreclaw_status` to confirm, then offer `totalreclaw_upgrade`.
- Tool returns a generic error: surface the message, don't retry blindly — the backoff is inside the tool, not you.

---

## Memory Taxonomy v1 reference

The on-chain v1 contract (as of plugin 3.0.0 / Hermes 2.3.0 / core 2.2.0):

| Field | Required | Values |
|---|---|---|
| `text` | yes | 5-512 UTF-8 chars |
| `type` | yes | `claim` / `preference` / `directive` / `commitment` / `episode` / `summary` |
| `source` | yes | `user` / `user-inferred` / `assistant` / `external` / `derived` |
| `scope` | no | `work` / `personal` / `health` / `family` / `creative` / `finance` / `misc` / `unspecified` |
| `volatility` | no | `stable` / `updatable` / `ephemeral` |
| `importance` | no | 1-10 (int) or 0-1 (float) |

Retrieval v2 Tier 1 ranks user-sourced facts above assistant-sourced facts on tied BM25 + cosine scores. Use `source="user"` for verbatim user statements and `source="user-inferred"` when you're stating something the user implied but didn't say outright.
