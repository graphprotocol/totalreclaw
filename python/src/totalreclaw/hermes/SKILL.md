---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for Hermes, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 2.3.1rc6
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes

## Setup flow (run on "install / set up TotalReclaw" OR any tool returning `onboarding required`)

1. `pip install --pre totalreclaw` (latest pre-release on PyPI; pin a specific `rcN` from https://pypi.org/project/totalreclaw/#history if asked).
2. Restart gateway so plugin loads. Docker: `docker restart tr-hermes`. Native: `hermes gateway restart`. Wait ~5s; confirm the gateway is back up.
3. Call `totalreclaw_pair`. Returns `{url, pin, expires_at_ms}`.
4. Relay to user verbatim: *"Open <url> in your browser. Enter your 12-word recovery phrase (or let the browser generate one). Confirm PIN <pin>. I'll wait for you to say done."*
5. After user says done: verify `~/.totalreclaw/credentials.json` exists and is non-empty. If missing, call `totalreclaw_pair` again (PIN likely expired) and resend.
6. Confirm: *"TotalReclaw is set up. Ask me to remember or recall anything."*

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated setup path is `totalreclaw_pair`. If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin X" → `totalreclaw_recall` first for `fact_id`, then `totalreclaw_forget` / `_pin` / `_unpin`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim).
- "import from Mem0 / ChatGPT / Claude / Gemini / mcp-memory-server" → `totalreclaw_import_from` with `dry_run=True` first.

## Diagnostics

- `totalreclaw_*` tools not visible → gateway not restarted after install; redo step 2.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from step 3.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.

## Tool surface

`totalreclaw_pair` (ONLY setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_export` · `_status` · `_upgrade` · `_import_from` · `_import_batch` · `_debrief` · `_report_qa_bug` (RC only).
