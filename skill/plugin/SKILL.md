---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for OpenClaw, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
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

# TotalReclaw — OpenClaw

## Setup flow (run on "install / set up TotalReclaw" OR any tool returning `onboarding required`)

1. `openclaw plugins install @totalreclaw/totalreclaw` (stable). For RC: install `@totalreclaw/totalreclaw@rc` — the `rc` dist-tag on npm is the latest candidate.
2. Restart gateway so plugin routes + tools bind. Docker: `docker restart tr-openclaw`. Native: `openclaw gateway restart`. Wait ~5s; confirm the gateway is back up.
3. Call `totalreclaw_pair`. Returns `{url, pin, qr_ascii, expires_at_ms}`.
4. Relay to user verbatim: *"Open <url> in your browser. Enter your 12-word recovery phrase (or let the browser generate one). Confirm PIN <pin>. I'll wait for you to say done."*
5. After user says done: verify `~/.totalreclaw/credentials.json` exists and is non-empty. If missing, call `totalreclaw_pair` again (PIN likely expired) and resend.
6. Confirm: *"TotalReclaw is set up. Ask me to remember or recall anything."*

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard`, `totalreclaw setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated setup path is `totalreclaw_pair`. If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin / retype / move to <scope>" X → `totalreclaw_recall` first for `factId`, then `totalreclaw_forget` / `_pin` / `_unpin` / `_retype` / `_set_scope`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim). Post-upgrade testnet → mainnet: `totalreclaw_migrate` with `dry_run` first.
- "import / export / consolidate" → `totalreclaw_import_from` (`dry_run=true` first; `_import_batch` for >50 chunks) · `totalreclaw_export` · `totalreclaw_consolidate` (`dry_run` first).

## Diagnostics

- `totalreclaw_*` tools not visible → gateway not restarted after install; redo step 2.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from step 3.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.
- `No LLM available for auto-extraction` at startup → provider key unreachable; check `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or plugin config `extraction.llm`.

## Tool surface

`totalreclaw_pair` (ONLY setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_status` · `_upgrade` · `_migrate` · `_import_from` · `_import_batch` · `_consolidate` · `_onboarding_start` (pointer to local-terminal wizard, for users explicitly rejecting the browser flow) · `_report_qa_bug` (RC only).
