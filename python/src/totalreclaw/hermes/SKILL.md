---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext. Python / Hermes edition."
version: 2.3.1rc5
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes Plugin Skill

**This document is directive for agents, not user-facing copy.** Follow the rules in order; every rule is a hard `MUST` unless stated otherwise.

## RULE 0 — Recovery-phrase handling (security boundary, ABSOLUTE)

The recovery phrase is the ONLY key to the user's encrypted memories. A single leaked phrase equals a fully compromised vault: the LLM provider (and anyone with access to its logs) can decrypt every memory the user has ever stored. Phrases do NOT rotate — once leaked, always leaked.

**Absolute rule:** the recovery phrase MUST NEVER cross the LLM context in ANY form — not in your replies, not in tool-call arguments, not in tool-call return values, not in your reasoning, not in shell-tool stdout.

- **NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-generating / phrase-restoring CLI via your shell tool.** Those commands print (or could print, with flags like `--emit-phrase`, or with a silent-save regression) the phrase. Your shell-tool stdout is captured into LLM context. Those CLIs exist for users to run in their OWN terminal, OUTSIDE any agent shell.
- **NEVER pass a recovery phrase as a tool-call argument.** The entire tool-call payload is in LLM context. The `totalreclaw_setup` tool that accepted a `recovery_phrase` parameter in rc.3 has been REMOVED in rc.4 for this reason.
- **NEVER generate a 12-word phrase yourself.** You cannot produce a valid BIP-39 checksum; any phrase you invent is garbage that can't recover real memories.
- **NEVER display a phrase in chat.** LLM providers log transcripts; any phrase in a reply leaks to every hop.
- **NEVER ask the user to paste their phrase.** If they do anyway, tell them immediately: "That was pasted into chat — please delete the message and generate a new wallet via `totalreclaw_pair` with `mode=generate`. The phrase you pasted is now compromised and cannot be un-leaked."

The ONLY agent-facilitated setup path is `totalreclaw_pair` (see RULE 1a). Browser-side crypto keeps the phrase out of the LLM round-trip by construction.

---

## RULE 1 — On plugin load, check setup state

Exactly one of these is true when your session starts:

| State | Detection | Your action |
|---|---|---|
| Setup complete | `totalreclaw.agent.state.is_configured() == True` | Do nothing; memory tools are live. |
| Setup pending | `is_configured() == False` (credentials.json missing/empty) | Proceed to RULE 1a. |

### RULE 1a — Setting up a user's TotalReclaw recovery phrase

**Agent path (default, only secure path): call the `totalreclaw_pair` tool.** It returns `{url, pin, expires_at, qr_png_b64, qr_unicode, mode, instructions}` (the last two rc.5 fields are new — see the QR-rendering section below). Tell the user:

> "Open <URL> in your browser, enter your phrase (or let the browser generate a new one), and confirm PIN <PIN>. Your phrase is encrypted in the browser and uploaded end-to-end-encrypted to this gateway — it never crosses this chat."

The browser does x25519 + ChaCha20-Poly1305 encryption against the gateway's ephemeral key. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json`. The LLM never sees the phrase at any point in the flow.

**After the user confirms "pairing complete" in their browser:** tell them to restart the Hermes gateway so the plugin picks up the new credentials. Plugin routes and hooks only bind on fresh gateway start.

### Rendering the QR on your transport (rc.5+)

When the `totalreclaw_pair` payload arrives, tell the user: "Open the URL below or scan this QR code from your phone. PIN: <pin>." Then render the QR based on your chat transport:

- **Transport supports image attachments** (Telegram, Slack, web chat, any transport with file/image upload): decode `qr_png_b64` from base64 to bytes, save to a tempfile with a `.png` suffix, and attach the tempfile as an image in your reply. Also show the URL + PIN as text so users who prefer clicking can still do so. (Example for Telegram: the reply tool accepts `files=["<path>.png"]`.)
- **Terminal-only transport** (bare SSH shell, non-graphical CLI): emit the `qr_unicode` string inline in your reply. It renders as half-block glyphs in most monospace terminal fonts; the user scans from their phone.
- **Web chat with inline-image support**: embed as `<img src="data:image/png;base64,<qr_png_b64>" alt="Pair QR">` if the chat client renders HTML; otherwise fall back to attachment.

The QR encodes ONLY the pair URL. The 6-digit PIN is a separate out-of-band confirmation — it is deliberately NOT baked into the QR (dual-channel design: scan + type). Never log, print, or re-emit the PIN outside of the user-facing reply. The PIN is a shared secret between you and the user; it must NOT end up in any other tool call, any log file, or any memory store.

**NEVER** invoke `totalreclaw setup`, `hermes setup`, `openclaw totalreclaw onboard`, or any phrase-generating CLI via your shell tool. Those commands are for users running them in their own terminal — not for you. Agent shell stdout is captured into LLM context; running them leaks the phrase.

**NEVER** ask the user to paste their phrase into chat. If they do, immediately tell them to rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`; the pasted phrase is compromised and there is no rotation after-the-fact.

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

### RULE 3a — First-person queries ALWAYS trigger recall

Any user message that references THEIR OWN facts triggers a recall call BEFORE you answer. Triggers (non-exhaustive — err on the side of calling recall):

- "where do I live / work" / "what's my address / city"
- "what do I prefer / like / hate / use"
- "do I have / own / know"
- "when did I / have I ever"
- "who is my / my [relation/role]"
- "what was my / my [object/preference]"
- any question pattern containing "my / I / me" + a fact-shaped noun (address, job, favourite, project, partner, pet, etc.)

Call `totalreclaw_recall(query=<semantic version of the question>)` FIRST, THEN answer based on returned facts. Do NOT answer from memory or invent. If recall returns 0 results, say "I don't have anything about that yet." rc.2 QA debug found 5/5 failures to call recall on "where do I live?" — the phrasing was enough to make agents skip the tool. This rule is hard: first-person factual queries are a recall trigger, full stop.

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

## RULE 5 — QR pair is the canonical setup flow (RULE 1a applies to ALL users)

RULE 1a's `totalreclaw_pair` flow is canonical for EVERY user, regardless of whether they have terminal access. Local users, remote users, Docker users, VPS users — all go through `totalreclaw_pair`. The browser-side crypto is what keeps the phrase out of the LLM context, and that protection matters whether the user is on the same machine or on a phone halfway around the world.

If a user explicitly says they prefer to set up entirely in their own terminal (no browser, no URL to open), point them at the CLI `totalreclaw setup` — but tell them to run it IN THEIR OWN TERMINAL, not through you. Do NOT call that CLI via your shell tool. Your shell-tool stdout is captured into LLM context.

Hermes Python 2.3.1rc4 ships a native `totalreclaw_pair` implementation (x25519 + ChaCha20-Poly1305). Use it directly — no fallback to external tools needed.

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

### zai provider configuration (2.3.1rc3+)

zai exposes two endpoints:
- **Coding plan (subscription)**: `https://api.z.ai/api/coding/paas/v4` — default.
- **PAYG**: `https://api.z.ai/api/paas/v4` — for pay-as-you-go balances.

A coding-plan key hitting the PAYG endpoint (or vice-versa) returns `Insufficient balance or no resource package. Please recharge.` rc.3 auto-detects this and flips to the other endpoint on one retry per call, but users can avoid the first-call tax by setting `ZAI_BASE_URL` in their `~/.hermes/.env` or environment:

- GLM Coding Plan users: leave `ZAI_BASE_URL` unset, or set to `https://api.z.ai/api/coding/paas/v4`.
- PAYG users: set `ZAI_BASE_URL=https://api.z.ai/api/paas/v4`.

Retry budget: the extraction LLM retries up to 5 attempts with 2s→4s→8s→16s→32s backoff (total ~62s). Configurable via `TOTALRECLAW_LLM_RETRY_BUDGET_MS` (default 60000ms).

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

---

## RULE 10 — Filing QA bugs (RC builds only)

If the `totalreclaw_report_qa_bug` tool is registered (this means you're running an RC build — stable users never see the tool), OFFER to file a bug when ANY of these triggers fire. Never auto-file; always ask the user once per issue.

Triggers:
1. A tool call fails 2+ times in a row with the same error signature.
2. User expresses friction: "this doesn't work" / "error" / "stuck" / "broken" / "not what I expected" / "wrong version" / explicit "file a bug".
3. Setup flow hits an error that you can't resolve via the docs.
4. Docs don't match reality (user guide says X; actual behavior is Y).

Offer: "This looks worth reporting so the maintainer can fix it. Want me to file a QA bug? I'll capture the symptom + repro."

On user yes → call `totalreclaw_report_qa_bug` with the redacted details. Required fields: `integration` (plugin/hermes/nanoclaw/mcp/relay/clawhub/docs/other), `rc_version` (exact version string, e.g. `2.3.1rc3`), `severity` (blocker/high/medium/low), `title` (<60 chars), `symptom`, `expected`, `repro`, `logs`, `environment`.

On user no / ambiguous → proceed without filing.

Do NOT offer the same bug twice in a session. Do NOT include secrets (recovery phrases, API keys, Telegram bot tokens, bearer tokens) in any field — the tool redacts automatically, but don't pass raw values anyway. The tool requires `TOTALRECLAW_QA_GITHUB_TOKEN` (or `GITHUB_TOKEN`) to be set on the host; if the tool returns a missing-token error, tell the user the operator needs to export one with `repo` scope.
