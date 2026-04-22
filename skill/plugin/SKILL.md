---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext."
version: 3.3.1-rc.2
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

- **Missing, empty, malformed, or no `mnemonic` key** -> NOT set up. You MUST NOT generate a phrase in chat, and you MUST NOT ask the user to paste one. Route:
  - Local gateway: call `totalreclaw_onboarding_start` (or `totalreclaw_onboard` if available); both return a pointer to `openclaw totalreclaw onboard`.
  - Remote gateway (VPS / Docker / no TTY): call `totalreclaw_pair` (v3.3.1+), OR instruct the user to run `openclaw totalreclaw pair generate --json`. Present the returned `{url, pin, qr_ascii}` to the user. The phrase is entered in their browser, never in this conversation.
- **Present with a valid `mnemonic`** -> active. Use the tools below.

## Core rules

1. **Phrase safety (NON-NEGOTIABLE).** The recovery phrase is a secret. Never generate one in chat, never echo one back, never log one in reasoning / tool payloads, never pass one to another tool. Direct users to `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`.
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
| `totalreclaw_onboarding_start` / `totalreclaw_onboard` | (none) — returns CLI pointer |
| `totalreclaw_pair` | optional `mode` (`generate` / `import`) — returns `{url, pin, qr_ascii, expires_at_ms}` |

## Taxonomy

**Types:** `claim` (default) / `preference` / `directive` (reusable rule) / `commitment` (future intent) / `episode` (event) / `summary` (derived synthesis).

**Scopes:** `work` / `personal` (default) / `health` / `family` / `creative` / `finance` / `misc`.

## If a tool fails

- Tell the user plainly. Don't retry blindly.
- "onboarding required" -> route per Setup-state above.
- "No LLM available for auto-extraction" (startup only, v3.3.1+) -> provider key not reachable. Point at `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or the `plugins.entries.totalreclaw.config.extraction.llm` override.
- Silent extraction failures -> suggest `openclaw totalreclaw status` or check `~/.totalreclaw/billing-cache.json` for rate-limit signals.

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
