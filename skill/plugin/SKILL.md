---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for OpenClaw, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 3.3.5-rc.1
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

> The canonical install flow lives at <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>. If a user pastes that URL in chat, fetch it and follow its agent-instructions section. This SKILL.md mirrors the same steps so you can act even if the URL is unreachable.

## Top-level rule (read this before anything else)

**Emit ONLY the user-visible lines listed below. Do not narrate tool calls. Do not describe what you're doing or about to do. Errors are the only allowed deviation, and only as a single tight line.** The expected user-facing transcript for a healthy install is **5–6 short lines TOTAL** (lines 1, 2, 5, 6 always; lines 3 and 4 only if the corresponding condition fires). Anything more is a SKILL violation. Telegram and similar transports edit the bot's messages live as your tool calls progress — every interim "Let me check…" / "Plugin loaded. Let me verify…" line shows up to the user as awkward edit churn, even if you "delete" it. The fix is to never emit it in the first place.

## User-visible line set (THIS IS ALL YOU SAY)

Speak only these lines, in order. Substitute the bracketed placeholders with the real values. No prefaces. No suffixes. No narration.

1. (start of install)
   > Installing TotalReclaw…
2. (after install + auto-reload + `totalreclaw_pair` is bound)
   > ✓ Installed (totalreclaw &lt;version&gt;)
3. (optional — only if auto-reload didn't fire and you need to issue `/restart`)
   > Restarting OpenClaw to load the plugin…
4. (optional — only if you successfully disabled the built-in memory tool to prevent dual-write)
   > Disabled built-in memory tool.
5. (account-setup URL + PIN, immediately after `totalreclaw_pair` returns)
   > Open &lt;url&gt; in your browser. Enter PIN &lt;pin&gt;. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
6. (done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.

If existing credentials are detected (step "Check for existing credentials" below), emit ONLY this line and stop:
   > ✓ TotalReclaw is already set up.

## Setup flow

If `totalreclaw_*` tools are already bound to your toolset, SKIP install and jump to step 4. Otherwise run all steps in order.

1. **Install plugin first, skill second (silent — emit user-visible line 1 once at the start, then no prose until line 2).** Run via your shell tool, in this exact order:
   ```
   openclaw plugins install @totalreclaw/totalreclaw
   openclaw skills install totalreclaw
   ```
   For RC: replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc`. **Plugin BEFORE skill is deliberate** (3.3.4-rc.1 — issue from Pedro's QA on rc.3): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install. Plugin-first lets the plugin land cleanly, then the skill's reload picks it up on the next gateway loop. If you accidentally do skill-first and the plugin install dies mid-flight, just retry `openclaw plugins install @totalreclaw/totalreclaw` once — it's idempotent.

   If either shell call fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), tell the user verbatim *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` and skip to step 3.

2. **Wait for auto-reload — then issue `/restart` autonomously if needed (silent — emit user-visible line 2 on success, line 3 only if you had to restart).** OpenClaw's default config (`gateway.reload.mode = "hybrid"`) auto-restarts the gateway via a file-watcher within 1-3 seconds. Wait ~5s, then run `openclaw plugins list | grep totalreclaw` and confirm `totalreclaw_pair` is in your toolset. When bound, emit user-visible line 2 and proceed.

   **If `totalreclaw_pair` is NOT visible after 10s — restart imperative (read carefully):**

   > Your IMMEDIATE next message must be the literal slash command `/restart` — nothing else. Do not narrate. Do not ask permission. Do not describe what `/restart` does. Do not propose alternatives. Just send `/restart`. You may emit user-visible line 3 (`Restarting OpenClaw to load the plugin…`) ONCE in the same turn, then issue `/restart`. Nothing else.

   `/restart` is in-process — it drains active runs cleanly; your shell is NOT shelling out to `docker restart` or `openclaw gateway restart` (those would suicide the host). After `/restart`, wait ~3s and re-run `openclaw plugins list`.

   **If `/restart` returns "You are not authorized to use this command":** surface this verbatim and stop. Do NOT loop `/restart` calls. Do NOT propose alternatives.

   > Your Telegram channel needs `allowFrom` configured. Run on your host: `jq '.channels.<channel>.allowFrom = ["<your-id>"]' ~/.openclaw/openclaw.json | sponge ~/.openclaw/openclaw.json && docker restart <your-container-name>`. Substitute `<channel>` (e.g. `telegram`, `slack`), `<your-id>` (Telegram: `@username` or numeric `chat_id`; Slack: workspace member ID), and `<your-container-name>` (`docker ps` shows it). For non-Docker installs substitute `openclaw gateway restart` for the `docker restart` portion. On a managed service, ask the service's support to enable slash commands. Reply `done` after.

   Then wait for `done`. Do NOT retry the install. Do NOT keep restarting. As a last-resort session-level fallback while restart is gated, try `/new` once (fresh session within the same gateway process, may pick up freshly-bound tools without a full restart).

   **Forbidden vocabulary** (these are anti-patterns — they show up to the user even if your transport edits them later, and they violate the silence rule):
   - "Let me check that…"
   - "Plugin loaded. Let me verify…"
   - "I'll now…"
   - "Quick fix…"
   - "However, I don't see…"
   - "I need to use…"
   - "Should I /restart" / "Do you have a public URL" / "Want me to restart" / "Let me check if the tool is bound" / "I need permission to restart"
   - "Now installing…" / "Got it" / "In parallel…" / "Step 1a/1b/1c"

   Substitute all of these with silence — the next user-visible line in the set, or nothing.

3. **Verify `totalreclaw_pair` is bound.** If still missing after `/restart` (or after the user fixed allowFrom and said `done`), tell them the restart didn't take effect and ask them to retry. Do not loop the install.

4. **Check for existing credentials (silent unless found).** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit *"✓ TotalReclaw is already set up."* and stop.

5. **Set up the user's TotalReclaw account (emit user-visible line 5 once, after `totalreclaw_pair` returns).** Call `totalreclaw_pair` silently (the account-setup tool — name kept for backward compatibility). It returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}` (see "Rendering the QR" below). Then emit user-visible line 5 with `<url>` and `<pin>` substituted. Do not pre-narrate ("I'll walk you through…", "Setting up your account now…") — line 5 itself is the only thing the user needs to see. The URL + PIN itself is the real consent moment: surfacing them verbatim is what gives the user the chance to opt out by simply not opening the page.

6. **Verify and confirm (silent until line 6).** After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — call `totalreclaw_pair` again silently and resend line 5 once. If present, emit user-visible line 6 and stop.

## Rendering the QR on your transport (rc.5+)

When you receive the `totalreclaw_pair` payload, render the QR based on your chat transport:

- **Transport supports image attachments** (Telegram, Slack, web chat, any transport with file/image upload): decode `qr_png_b64` from base64 to bytes, save to a tempfile with a `.png` suffix, and attach the tempfile as an image. Also show the URL + PIN as text so users who prefer clicking can still do so.
- **Terminal-only transport** (OpenClaw native CLI chat, bare SSH shell): emit `qr_unicode` inline in your reply. It renders as block characters in most terminal fonts; the user scans from their phone.
- **Web chat with inline-image support**: embed as `<img src="data:image/png;base64,<qr_png_b64>" alt="TotalReclaw account-setup QR">` if the chat client renders HTML; otherwise fall back to attachment.

The QR encodes ONLY the account-setup URL. The 6-digit PIN is a separate out-of-band confirmation — deliberately NOT baked into the QR (dual-channel design: scan + type). Never log, print, or re-emit the PIN outside the user-facing reply. The PIN must NOT end up in any other tool call, log file, or memory store.

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated account-setup paths are (in order of preference): (1) the `totalreclaw_pair` account-setup tool; (2) if the tool is missing from your tool list — a known gap for some container-based agents, issue #87 — shell out to EITHER `openclaw totalreclaw pair generate --url-pin-only` (single-line JSON `{v,url,pin,expires_at_ms}`) OR `openclaw totalreclaw onboard --pair-only` (single-line JSON `{v,pair_url,pin,expires_at_ms}`, issue #95 — same account-setup flow, `pair_url` key naming). Both have zero phrase material on stdout (account-setup crypto is x25519-only, not BIP-39). If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin / retype / move to <scope>" X → `totalreclaw_recall` first for `factId`, then `totalreclaw_forget` / `_pin` / `_unpin` / `_retype` / `_set_scope`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim). Post-upgrade testnet → mainnet: `totalreclaw_migrate` with `dry_run` first.
- "import / export / consolidate" → `totalreclaw_import_from` (`dry_run=true` first; `_import_batch` for >50 chunks) · `totalreclaw_export` · `totalreclaw_consolidate` (`dry_run` first).

## Diagnostics

- `totalreclaw_*` tools not visible → plugin not installed or auto-reload didn't fire. Verify via `openclaw plugins list | grep totalreclaw` (or, on a managed service that doesn't expose the CLI, ask the user to confirm via their service's plugins UI). If `totalreclaw` is listed but tools are still absent, the gateway probably hasn't completed its auto-restart yet (give it 5-10s) OR `gateway.reload.mode = off` is set — instruct manual restart as fallback: `openclaw gateway restart` (native), `docker restart <your-container-name>` (Docker self-host), or the managed service's restart control. If the plugin is listed and the gateway has been restarted but tools are still missing (container-agent gap, issue #87), shell out to `openclaw totalreclaw pair generate --url-pin-only` on the gateway host and hand the user the `url` + `pin` from the JSON payload — OR `openclaw totalreclaw onboard --pair-only` (same flow, returns `pair_url` instead of `url`, issue #95). If your shell doesn't reach the gateway host (managed service), tell the user to install/enable the plugin via their service's plugins UI and restart the gateway via the service's restart control. Do NOT suggest the bare `openclaw totalreclaw onboard` (leaks phrase on stdout — deprecated in rc.18, removed next RC).
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the account-setup step.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.
- `No LLM available for auto-extraction` at startup → provider key unreachable; check `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or plugin config `extraction.llm`.

## Tool surface

`totalreclaw_pair` (ONLY account-setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_status` · `_upgrade` · `_migrate` · `_import_from` · `_import_batch` · `_consolidate` · `_onboarding_start` (pointer to local-terminal wizard, for users explicitly rejecting the browser flow) · `_report_qa_bug` (RC only).
