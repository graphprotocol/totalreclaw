---
name: totalreclaw
description: "Encrypted memory for Hermes — remember, recall, forget, pin, re-type / re-scope, check status & quota, upgrade, and import the user's saved memories. Trigger on 'install / set up TotalReclaw', 'restore my recovery phrase', or ANY request to remember, recall, forget, search, manage, or report on the user's memory or quota — not just remember / recall."
version: 2.5.0
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes (usage)

> **This is the post-install usage skill.** If `totalreclaw_*` tools are already bound to your toolset, use the guidance below.
>
> **Not installed yet, or the tools are missing?** The install + account-setup flow is a separate document: <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>. Fetch it and follow its agent-instructions. Do NOT improvise install steps from this file — this doc is **usage only**, and the install flow has its own silence rules that live in that guide.

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated account-setup path is `totalreclaw_pair` (the account-setup tool). If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

### Storing memories — when + how

**There are two write paths:**

1. **Auto-extraction (background, every ~3 turns + at session end).** The plugin runs a `post_llm_call` hook that extracts facts from recent conversation and stores them. You don't trigger this; it runs by itself. Look for `TotalReclaw: extracted N memories` lines in the gateway log if you want to confirm a turn was captured.

2. **Explicit `totalreclaw_remember`** — call this only when the user says something the auto-extraction is likely to MISS or when they explicitly say "remember X" / "save that I X":
   - One-off declarative statements outside a natural conversation flow ("my birthday is March 14", "I prefer Postgres over MySQL")
   - Verbatim quotes the user wants preserved exactly ("write down the AWS account ID exactly: 123…")
   - Decisions the user wants ledgered with reasoning ("we picked X because Y")
   - Anything the user marks with imperative language: "remember", "save", "store", "don't forget", "keep this"

**Don't double-call.** If auto-extraction just fired (recent log lines show extraction), skip the manual `totalreclaw_remember` — embedding dedup will silently drop the duplicate, but you waste a write against quota.

**Don't store transient noise.** Skip `totalreclaw_remember` for:
- Casual greetings, banter, acknowledgements
- Tool-output paste-backs the user didn't write themselves
- Commands / instructions the user issued to YOU (not facts about the user) — "open this file" is not a memory
- Repeating things the user just said back to them in a single turn
- Anything the user said while explicitly testing memory ("just to test, remember the word elephant" — store it, but don't store the user's own meta-commentary on the test)

**Memory types (taxonomy v1).** Each stored fact gets a type. The extractor auto-tags; if you call `totalreclaw_remember` manually, pass `type=` when the right one is obvious — saves a `totalreclaw_retype` later:
- `claim` — factual statement about the user / world ("I live in Lisbon")
- `preference` — taste / choice ("I prefer espresso over filter")
- `directive` — durable instruction to the agent ("always summarize in bullets", "never use semicolons")
- `commitment` — promise / obligation ("I'll review the PR by Friday")
- `episode` — notable event with time anchor ("on 2026-03-12 I went to Berlin for a conference")
- `summary` — multi-turn debrief output. Don't write these manually; `totalreclaw_debrief` produces them.

**Scopes.** Each fact also gets a scope (work / personal / health / family / creative / finance / misc / unspecified). The extractor infers; you can pass `scope=` explicitly when the user's intent is clear (a health fact during a work conversation should still be `scope=health`). To re-scope an existing fact: `totalreclaw_recall` for the `fact_id`, then `totalreclaw_set_scope`.

### Summaries — `totalreclaw_debrief`

Auto-fires once per session via the `on_session_end` hook (when the gateway closes the session — `/new`, idle timeout, restart). You almost never call this manually.

**Manual call ONLY when** the user explicitly asks for a session recap mid-conversation: "summarize what we discussed", "give me a debrief on this session", "what's the rolling memory of this chat?". One-shot call, no args needed — the tool walks the current session buffer.

### Recall — when + how

- **First-person factual query** ("do I / what's my / where do I / what did I say about / what do you know about me / am I supposed to / when did I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so honestly — DO NOT fabricate from session context. This is non-negotiable: agents MUST call `totalreclaw_recall` even when the answer appears to be in the current context window. Hermes' built-in `USER.md` cache is local; the TotalReclaw vault is canonical + cross-device.
- **Specific-fact lookups** ("what's my AWS account ID?") → `totalreclaw_recall` with a tight query. The reranker (BM25 + cosine + RRF + source-weighted) is sharper on specific queries.

### Mutating existing facts

Pattern is always **recall first → mutate second**, because the mutation tools need `fact_id`:

- `forget X` → `totalreclaw_recall("X")` → pick the right `fact_id` → `totalreclaw_forget(fact_id)`. Tombstones the fact (still retained for audit; recall filters it out).
- `pin X as canonical` → recall → `totalreclaw_pin(fact_id)`. Pinned facts surface in every subsequent recall regardless of query similarity.
- `unpin X` → recall → `totalreclaw_unpin(fact_id)`.
- `change type of X to <type>` → recall → `totalreclaw_retype(fact_id, type)`. Use when the extractor misclassified.
- `change scope of X to <scope>` → recall → `totalreclaw_set_scope(fact_id, scope)`.

### Status / upgrade / import

- "status" / "how am I doing on quota" / "what tier am I on" → `totalreclaw_status` (no args). Returns tier, used / limit, and smart-account address.
- "upgrade" / "go Pro" → `totalreclaw_upgrade` (returns a Stripe checkout URL — paste verbatim, no paraphrase).
- "import from Mem0 / ChatGPT / Claude / Gemini / mcp-memory-server" → `totalreclaw_import_from` with `dry_run=True` first to surface the count + estimated free-tier impact, then ask the user to confirm before the real run.

## Tiers + pricing

**Canonical source for live numbers:** `totalreclaw_status` (returns `tier`, `free_writes_used`, `free_writes_limit` for the current account). **Canonical source for catalogue / prices:** <https://totalreclaw.xyz/pricing>. Don't invent dollar amounts and don't name the underlying network — quote what `totalreclaw_status` returns or point at the URL.

| Tier | Monthly memory cap | Notes |
|---|---|---|
| **Free** | 250 memories/month | Permanent, end-to-end encrypted. No credit card. Cap resets monthly. |
| **Pro** | 1,500 memories/month | Same encryption + ownership. Adds LLM-guided dedup. Stripe checkout via `totalreclaw_upgrade`. |

Both tiers: encryption, ownership, and on-chain durability are identical. Tier only changes the monthly memory cap + Pro's dedup feature.

### Automatic quota signalling

The plugin fetches billing on every `on_session_start` and caches it for 2 hours. It auto-injects warnings into your context at these thresholds — when you see them, surface them to the user:

- **>80% usage** — soft warning. Mention casually: *"You're at `<used>` / `<limit>` of your free-tier quota this month. Upgrade to Pro for 1,500/month — want me to open the upgrade link?"* Wait for confirmation before calling `totalreclaw_upgrade`.
- **403 quota exceeded** on a write → billing cache invalidated, warning re-injects next turn. Surface the error verbatim + offer `totalreclaw_upgrade` immediately.
- **First successful pair** → the setup-flow confirmation already includes tier + limit. Do not re-emit the same info on subsequent first-message-of-session.

Do NOT compute "you have X left" math yourself — `totalreclaw_status` returns `free_writes_used` and `free_writes_limit` already. Quote them verbatim.

### Upgrade flow

User intent → action:

| User signal | Action |
|---|---|
| "How much does it cost?" / "What's the pricing?" / "Is it free?" | Cite the table above + pricing URL. Don't quote a `$` figure unless the user explicitly asked for the current Pro price AND you just called `totalreclaw_status`. |
| User asks to upgrade / asks how to get Pro / hits a 403 | Call `totalreclaw_upgrade` → returns a Stripe checkout URL. Emit ONE user-visible line: *"Open `<url>` in your browser to complete the upgrade. Reply `done` once the payment page confirms."* DO NOT paraphrase the URL. |
| User reports payment succeeded ("done") | Call `totalreclaw_status` once to refresh the cached tier. Emit a single confirmation: *"✓ You're now on TotalReclaw Pro — 1,500 memories/month."* |
| User wants to cancel / downgrade | Direct them to <https://totalreclaw.xyz/pricing> — the Stripe customer portal is the only canonical cancel path. The plugin does not expose a downgrade tool. |

### Forbidden tier claims (deny-list)

These statements are WRONG. Never write any of them — they fabricate a pricing model that doesn't exist:

- "There's a free trial period" — there isn't. The free tier is permanent.
- "Memories expire" — memories are permanent regardless of tier.
- "You need to upgrade to use encryption" — E2E encryption is identical across tiers.
- "Self-hosted is automatically Pro" — self-hosted bypasses the managed relay; tiers / limits only apply to managed users.
- "Pro = unlimited" — Pro is **1,500/month**, NOT unlimited. Saying "unlimited" silently breaks user expectations when they hit the cap.
- **Naming the underlying network / chain to the user** (e.g. "Gnosis", "mainnet", "Base Sepolia", "testnet") — users don't need it and it only confuses. Cite tier + quota via `totalreclaw_status`; never the chain.
- Inventing `$` amounts. The current Pro monthly price is the one returned by `totalreclaw_status` OR shown at the pricing URL — never anywhere else.

## Diagnostics

- `totalreclaw_*` tools not visible → the gateway wasn't restarted after install. This is an **install** problem — follow the install guide (<https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>), which owns the `/restart` procedure.
- User says done but `credentials.json` missing → PIN expired or wrong phrase entered; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the account-setup step in the install guide.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.

## Tool surface

`totalreclaw_pair` (ONLY account-setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_status` · `_upgrade` · `_import_from` · `_import_batch` · `_debrief` · `_report_qa_bug` (RC only).
