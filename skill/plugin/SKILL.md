---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for OpenClaw, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 3.3.4-rc.1
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

## Setup flow

If `totalreclaw_*` tools are already bound to your toolset, SKIP install and jump to step 4. Otherwise run all steps in order.

1. **Install plugin first, skill second.** Run via your shell tool, in this exact order:
   ```
   openclaw plugins install @totalreclaw/totalreclaw
   openclaw skills install totalreclaw
   ```
   For RC: replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc`. **Plugin BEFORE skill is deliberate** (3.3.4-rc.1 — issue from Pedro's QA on rc.3): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install. Plugin-first lets the plugin land cleanly, then the skill's reload picks it up on the next gateway loop. If you accidentally do skill-first and the plugin install dies mid-flight, just retry `openclaw plugins install @totalreclaw/totalreclaw` once — it's idempotent.

   If either shell call fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), tell the user verbatim *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` and skip to step 3.

2. **Wait for auto-reload — then issue `/restart` autonomously if needed.** OpenClaw's default config (`gateway.reload.mode = "hybrid"`) auto-restarts the gateway via a file-watcher within 1-3 seconds. Wait ~5s, then run `openclaw plugins list | grep totalreclaw` and confirm `totalreclaw_pair` is in your toolset.

   **If `totalreclaw_pair` is NOT visible after 10s:** issue OpenClaw's built-in `/restart` slash-command immediately and silently. **DO NOT ask the user "should I restart" / "do you want me to restart" / "let me check if the tool is bound" — just issue `/restart` and announce "Reloading gateway..." once.** This is in-process and drains active runs cleanly; your shell is NOT shelling out to `docker restart` or `openclaw gateway restart` (which would suicide the host). After `/restart`, wait ~3s and re-run `openclaw plugins list`.

   **If `/restart` returns "You are not authorized to use this command":** the gateway's `channels.<your-channel>.allowFrom` allowlist is empty. **Do NOT keep retrying.** Surface this one-line user-facing fix and stop:

   > Your channel needs to be allowlisted for slash commands. Run on the gateway host: `jq '.channels.<channel>.allowFrom = ["<your-id>"]' ~/.openclaw/openclaw.json | sponge ~/.openclaw/openclaw.json && docker restart <your-container-name>` (Docker self-host) — substitute `<channel>` with your channel name (e.g. `telegram`, `slack`), `<your-id>` with your user ID for that channel (Telegram: `@username` or numeric `chat_id`; Slack: workspace member ID), and `<your-container-name>` with your gateway container name (`docker ps` shows it). On a managed service, ask the service's support to enable slash commands for your channel. Reply `done` once it's back up.

   For non-Docker / native deployments substitute `openclaw gateway restart` for the `docker restart` portion. As a session-level fallback while restart is gated, try `/new` (starts a fresh session within the same gateway process, may pick up freshly-bound tools without a full restart).

   **Forbidden vocabulary** (these are anti-patterns when a tool is missing post-install): "Should I /restart", "Do you have a public URL", "Want me to restart", "Let me check if the tool is bound", "I need permission to restart". Just issue `/restart` immediately and announce.

3. **Verify `totalreclaw_pair` is bound.** If still missing after `/restart` (or after the user fixed allowFrom and said `done`), tell them the restart didn't take effect and ask them to retry. Do not loop the install.

4. **Check for existing credentials.** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, confirm *"TotalReclaw is already set up."* and stop.

5. **Set up the user's TotalReclaw account.** Tell the user: *"I'll walk you through setting up your TotalReclaw account."* Then call `totalreclaw_pair` (the account-setup tool — name kept for backward compatibility). Returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}` (see "Rendering the QR" below). Relay verbatim:
   > *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one — the phrase stays in your browser, the relay only sees ciphertext. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*

6. **Verify and confirm.** After user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — call `totalreclaw_pair` again and resend. If present, confirm *"Your TotalReclaw account is set up. Ask me to remember or recall anything."*

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
