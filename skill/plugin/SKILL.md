---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext."
version: 3.3.1-rc.6
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

# TotalReclaw — Agent Guide

You are operating inside a host that has the TotalReclaw memory plugin loaded. This file tells you WHAT to call and WHEN. Every sentence is a rule.

## Setup state

Before any memory tool, check `~/.totalreclaw/credentials.json`:

- **Missing, empty, malformed, or no `mnemonic` key** -> NOT set up. Follow the "Setting up a user's TotalReclaw recovery phrase" section below. You MUST NOT generate a phrase in chat, and you MUST NOT ask the user to paste one.
- **Present with a valid `mnemonic`** -> active. Use the tools below.

## Setting up a user's TotalReclaw recovery phrase

**Agent path (default, only secure path): call the `totalreclaw_pair` tool.** It returns a URL + 6-digit PIN. Tell the user:

> "Open <URL> in your browser, enter your phrase (or let the browser generate one), and confirm PIN <PIN>. Your phrase is encrypted in the browser and uploaded end-to-end-encrypted to this gateway — it never crosses this chat."

Browser-side crypto handles the phrase; this chat never sees it. After the user confirms "pairing complete" in their browser, the credentials are already on disk — no gateway restart needed for OpenClaw because routes are registered on plugin install and the pair HTTP handler writes credentials in-process.

**NEVER** invoke `openclaw totalreclaw onboard`, `totalreclaw setup`, or any phrase-generating CLI via your shell tool. Those commands are for users to run in their own terminal — not for you. Agent shell stdout is captured into LLM context; running them leaks the phrase.

**NEVER** ask the user to paste their phrase into chat. If they do, immediately tell them to rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`; the pasted phrase is compromised.

**If the user explicitly prefers local-terminal setup** (no browser, no QR URL to open), point them at `totalreclaw_onboarding_start` — which returns a pointer to the CLI wizard they run THEMSELVES. You do not run it.

## Core rules

1. **Phrase safety (NON-NEGOTIABLE, ABSOLUTE).** The recovery phrase is a secret. Never generate one in chat, never echo one back, never log one in reasoning / tool payloads, never pass one to another tool, never invoke a phrase-generating CLI via your shell tool. A leaked phrase = compromised vault, no rotation possible. Governed by `project_phrase_safety_rule.md` (memory file in p-diogo/totalreclaw-internal).
2. **Use the tools, not inline state.** `totalreclaw_remember` stores; `totalreclaw_recall` retrieves. Don't re-ask the user for past facts; don't paraphrase memories as your own recollection.
3. **Gateway restart is required after install.** If tools fail right after `openclaw plugins install @totalreclaw/totalreclaw`, tell the user to run `openclaw restart` or `docker restart openclaw-qa`.

## Decision tree — which tool, when

- **Fact-shaped content** (preferences, identity, commitments, decisions, directives, episodes): `totalreclaw_remember`. Don't ask permission for obvious user facts.
- **Question about the past** ("what do you remember about…", "do I like…"): `totalreclaw_recall`.
- **"Pin that" / "remember permanently"**: `totalreclaw_pin`. **"Unpin" / "can forget that"**: `totalreclaw_unpin`.
- **"Forget X" / "delete that memory"**: `totalreclaw_forget` with the fact's id.
- **"That's a preference, not a claim"**: `totalreclaw_retype`.
- **"Put that under work"**: `totalreclaw_set_scope`.
- **"Export my memories"**: `totalreclaw_export`.
- **"What's my TotalReclaw status?"**: `totalreclaw_status`.
- **"Set up TotalReclaw"** (no credentials): route per the Setup-state section above.
- **"Import my Mem0 / ChatGPT / Claude / Gemini history"**: `totalreclaw_import_from` with `dry_run=true` first. Show the estimate, confirm, then run without `dry_run`. For >50 chunks, use `totalreclaw_import_batch` and report progress.
- **"Upgrade" / "I want Pro"**: `totalreclaw_upgrade` returns a Stripe URL. After upgrade, offer `totalreclaw_migrate` (dry-run first) to move testnet memories to mainnet.

### First-person queries — ALWAYS call `totalreclaw_recall` first

Any user message that references THEIR OWN facts triggers a recall call BEFORE you answer. Triggers (non-exhaustive — err on the side of calling recall):

- "where do I live / work" / "what's my address / city"
- "what do I prefer / like / hate / use"
- "do I have / own / know"
- "when did I / have I ever"
- "who is my / my [relation/role]"
- "what was my / my [object/preference]"
- any question pattern containing "my / I / me" + a fact-shaped noun (address, job, favourite, project, partner, pet, etc.)

Call `totalreclaw_recall(query=<semantic version of the question>)` FIRST, THEN answer based on returned facts. Do NOT answer from memory or invent; if recall returns 0 results, say "I don't have anything about that yet." rc.2 QA debug found 5/5 failures to call recall on "where do I live?" — the phrasing was enough to make agents skip the tool. This rule is hard: first-person factual queries are a recall trigger, full stop.

## Tool surface

Tools work only when credentials are active AND the gateway has been restarted post-install. If a tool returns "onboarding required", route back to onboarding.

| Tool | Key params |
|------|------------|
| `totalreclaw_remember` | `text`, optional `type` (default `claim`), `importance` |
| `totalreclaw_recall` | `query`, optional `k` (default 8, max 20) |
| `totalreclaw_forget` | `factId` |
| `totalreclaw_pin` / `totalreclaw_unpin` | `factId`, optional `reason` |
| `totalreclaw_retype` | `factId`, `newType` |
| `totalreclaw_set_scope` | `factId`, `scope` |
| `totalreclaw_export` | optional `format` (`json` / `markdown`) |
| `totalreclaw_status` | (none) |
| `totalreclaw_upgrade` | (none) |
| `totalreclaw_migrate` | optional `confirm` (dry-run by default) |
| `totalreclaw_import_from` / `totalreclaw_import_batch` | `source`, `file_path` or `content`, `dry_run` |
| `totalreclaw_consolidate` | optional `dry_run` |
| `totalreclaw_onboarding_start` | (none) — returns CLI pointer for users who prefer local-terminal setup |
| `totalreclaw_pair` | optional `mode` (`generate` / `import`) — returns `{url, pin, qr_ascii, expires_at_ms}`. CANONICAL setup surface |

## Taxonomy

**Types:** `claim` (default) / `preference` / `directive` (reusable rule) / `commitment` (future intent) / `episode` (event) / `summary` (derived synthesis).

**Scopes:** `work` / `personal` (default) / `health` / `family` / `creative` / `finance` / `misc`.

## If a tool fails

- Tell the user plainly. Don't retry blindly.
- "onboarding required" -> route per Setup-state above.
- "No LLM available for auto-extraction" (startup only, v3.3.1+) -> provider key not reachable. Point at `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or the `plugins.entries.totalreclaw.config.extraction.llm` override.
- Silent extraction failures -> suggest `openclaw totalreclaw status` or check `~/.totalreclaw/billing-cache.json` for rate-limit signals.

## zai provider configuration (3.3.1-rc.3+)

zai exposes two endpoints:
- **Coding plan (subscription)**: `https://api.z.ai/api/coding/paas/v4` — default.
- **PAYG**: `https://api.z.ai/api/paas/v4` — for pay-as-you-go balances.

A coding-plan key hitting the PAYG endpoint (or vice-versa) returns `Insufficient balance or no resource package. Please recharge.` rc.3 auto-detects this and flips to the other endpoint on one retry per call, but users can avoid the first-call tax by setting `ZAI_BASE_URL` explicitly:

- GLM Coding Plan users: leave `ZAI_BASE_URL` unset, or set to `https://api.z.ai/api/coding/paas/v4`.
- PAYG users: set `ZAI_BASE_URL=https://api.z.ai/api/paas/v4`.

Retry budget: the extraction LLM retries up to 5 attempts with 2s→4s→8s→16s→32s backoff (total ~62s). Configurable via `TOTALRECLAW_LLM_RETRY_BUDGET_MS` (default 60000).

## Minimum viable interaction pattern

```
User: "I live in Porto and prefer PostgreSQL."
-> totalreclaw_remember({text: "User lives in Porto", type: "claim"})
-> totalreclaw_remember({text: "User prefers PostgreSQL over MySQL", type: "preference"})
-> respond naturally, don't list what you just saved.

User: "What do you remember about me?"
-> totalreclaw_recall({query: "user facts preferences identity"})
-> summarize returned facts in your reply.

User: "Set me up for TotalReclaw."
-> check ~/.totalreclaw/credentials.json. If missing:
   local:  totalreclaw_onboarding_start (or totalreclaw_onboard)
   remote: totalreclaw_pair -> present URL + PIN + QR
-> follow the tool's instructions. Never invent a phrase.
```

## What NOT to do

- Do NOT write memories to `MEMORY.md` or any cleartext file — that defeats E2EE.
- Do NOT call `totalreclaw_remember` for transient in-session context.
- Do NOT paste recovery phrases or API keys into chat.
- Do NOT run `npx @totalreclaw/mcp-server setup` — deprecated path that corrupts credentials.

## Filing QA bugs (RC builds only)

If `totalreclaw_report_qa_bug` tool is available (this means you're in an RC build — stable users never see the tool), OFFER to file a bug when ANY of these triggers fire. Never auto-file; always ask the user once per issue.

Triggers:
1. A tool call fails 2+ times in a row with the same error signature.
2. User expresses friction: "this doesn't work" / "error" / "stuck" / "broken" / "not what I expected" / "wrong version" / explicit "file a bug".
3. Setup flow hits an error that you can't resolve via the docs.
4. Docs don't match reality (user guide says X; actual behavior is Y).

Offer: "This looks worth reporting so the maintainer can fix it. Want me to file a QA bug? I'll capture the symptom + repro."

On user yes → call `totalreclaw_report_qa_bug` with the redacted details. Required fields: `integration` (plugin/hermes/nanoclaw/mcp/relay/clawhub/docs/other), `rc_version` (exact version string), `severity` (blocker/high/medium/low), `title` (<60 chars), `symptom`, `expected`, `repro`, `logs`, `environment`.

On user no / ambiguous → proceed without filing.

Do NOT offer the same bug twice in a session. Do NOT include secrets (recovery phrases, API keys, bot tokens) in any field — the tool redacts automatically, but don't pass raw values anyway. The tool requires `TOTALRECLAW_QA_GITHUB_TOKEN` (or `GITHUB_TOKEN`) to be set on the host; if the tool returns a missing-token error, tell the user the operator needs to export one with `repo` scope.
