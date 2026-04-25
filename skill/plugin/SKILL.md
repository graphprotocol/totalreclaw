---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for OpenClaw, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 3.3.1-rc.11
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

## Fast path — "install totalreclaw" (Approach A, preferred)

ClawHub classifies TotalReclaw as a skill. `openclaw skills install totalreclaw` registers this SKILL.md into your agent context BEFORE the plugin is installed (so you may be reading this with no `totalreclaw_*` tools yet).

If the user says "install totalreclaw" (or any variant) AND no `totalreclaw_*` tools are in your toolset:

1. Run via your shell tool: `openclaw plugins install totalreclaw`. (For RC candidates: `openclaw plugins install @totalreclaw/totalreclaw@rc` — the `rc` dist-tag on npm is the latest candidate.) If the shell call fails with `command not found` / ENOENT / "not authorized" / similar (managed-service or shell-restricted environment): tell the user *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` and skip to step 3.
2. Tell the user: *"Plugin installed. Verifying it loaded..."* OpenClaw's default config (`gateway.reload.mode = "hybrid"`) auto-restarts the gateway via a file-watcher when `plugins.*` config changes; this typically takes 1-3 seconds. Wait ~5 seconds, then run `openclaw plugins list` and confirm `totalreclaw` shows as enabled. If it does NOT appear within 10 seconds, the user has `gateway.reload.mode = off` (or another setup that disables auto-reload) — fall back to the manual-restart path: tell the user verbatim *"Auto-reload didn't fire. Please restart your OpenClaw gateway so the new tools bind. Common patterns: `openclaw gateway restart` (native install), `docker restart <your-container-name>` (Docker self-host — substitute your actual container name; `docker ps` will show it), or use your managed service's restart control if you're on a hosted plan. Once the gateway is back up, reply `done` and I'll continue."* Do NOT invoke any restart command yourself — your shell is running inside the gateway you're restarting (catch-22). Wait for the user's `done` before proceeding.
3. Verify `totalreclaw_pair` is now in your toolset. If not (and you already fell back to manual restart and the user said `done`), tell the user the restart didn't take effect and to retry the restart command. Do not loop the install.
4. Check `~/.totalreclaw/credentials.json` (via `test -s ~/.totalreclaw/credentials.json`). If it exists, confirm *"TotalReclaw is already set up."* and stop.
5. Call `totalreclaw_pair`. Returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}` (see "Rendering the QR on your transport" below for the rc.5+ additions).
6. Relay verbatim: *"Open <url> in your browser. Enter your 12-word recovery phrase (or let the browser generate one). Confirm PIN <pin>. I'll wait for you to say done."*
7. After user says done: verify `~/.totalreclaw/credentials.json`. If missing, call `totalreclaw_pair` again (PIN likely expired) and resend.
8. Confirm: *"TotalReclaw set up ✓. Ask me to remember or recall anything — try `totalreclaw_remember` or ask me to remember something."*

If `totalreclaw_*` tools ARE already in your toolset when the user asks, SKIP the install step and jump to step 4.

## Explicit two-step setup flow (Approach B, fallback)

Used when the user pastes a verbose prompt describing skill install + plugin install + restart + pair (e.g. *"TotalReclaw is a skill at 'totalreclaw' on ClawHub. Install it, then install the plugin, restart the gateway, and walk me through the QR pairing flow"*) OR when a tool returns `onboarding required`.

1. `openclaw plugins install @totalreclaw/totalreclaw` (stable). For RC: `@totalreclaw/totalreclaw@rc`. If the CLI isn't available to your shell (managed service / shell-restricted env): ask the user to install `totalreclaw` from their service's plugins UI and reply `done`.
2. Wait ~5 seconds for OpenClaw's auto-reload to fire (default `gateway.reload.mode = "hybrid"` — file-watcher detects the plugin install and sends SIGUSR1 to the gateway, completing within 1-3s). Verify with `openclaw plugins list`; `totalreclaw` should show enabled. If it does NOT appear within 10s, the user has `gateway.reload.mode = off` (or auto-reload is otherwise disabled) — fall back to manual restart: tell the user verbatim *"Auto-reload didn't fire. Please restart your gateway: `openclaw gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — replace with your actual container name), or your managed service's restart control. Reply `done` once it's back up."* Do NOT run the restart yourself — your shell is inside the gateway. Wait for the user's `done`.
3. Call `totalreclaw_pair`. Returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}` (see "Rendering the QR on your transport" below for the rc.5+ additions).
4. Relay to user verbatim: *"Open <url> in your browser. Enter your 12-word recovery phrase (or let the browser generate one). Confirm PIN <pin>. I'll wait for you to say done."*
5. After user says done: verify `~/.totalreclaw/credentials.json` exists and is non-empty. If missing, call `totalreclaw_pair` again (PIN likely expired) and resend.
6. Confirm: *"TotalReclaw is set up. Ask me to remember or recall anything."*

## Rendering the QR on your transport (rc.5+)

When you call `totalreclaw_pair` and receive the payload, tell the user: *"Open the URL below or scan this QR code from your phone. PIN: <pin>."*

Then render the QR based on your chat transport:

- **Transport supports image attachments** (Telegram, Slack, web chat, any transport with file/image upload): decode `qr_png_b64` from base64 to bytes, save to a tempfile with a `.png` suffix, and attach the tempfile as an image in your reply. Also show the URL + PIN as text so users who prefer clicking can still do so.
- **Terminal-only transport** (OpenClaw native CLI chat, bare SSH shell): emit the `qr_unicode` string inline in your reply. It renders as block characters that display correctly in most terminal fonts; the user scans from their phone.
- **Web chat with inline-image support**: embed as `<img src="data:image/png;base64,<qr_png_b64>" alt="Pair QR">` if the chat client renders HTML; otherwise fall back to attachment.

The QR encodes ONLY the pair URL. The 6-digit PIN is a separate out-of-band confirmation — it is deliberately NOT baked into the QR (dual-channel design: scan + type). Never log, print, or re-emit the PIN outside of the user-facing reply. The PIN is a shared secret between you and the user; it must NOT end up in any other tool call, any log file, or any memory store.

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (the bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated setup paths are (in order of preference): (1) `totalreclaw_pair` tool; (2) if the tool is missing from your tool list — a known gap for some container-based agents, issue #87 — shell out to EITHER `openclaw totalreclaw pair generate --url-pin-only` (single-line JSON `{v,url,pin,expires_at_ms}`) OR `openclaw totalreclaw onboard --pair-only` (single-line JSON `{v,pair_url,pin,expires_at_ms}`, issue #95 — same pair flow, `pair_url` key naming). Both have zero phrase material on stdout (pair-crypto is x25519-only, not BIP-39). If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin / retype / move to <scope>" X → `totalreclaw_recall` first for `factId`, then `totalreclaw_forget` / `_pin` / `_unpin` / `_retype` / `_set_scope`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim). Post-upgrade testnet → mainnet: `totalreclaw_migrate` with `dry_run` first.
- "import / export / consolidate" → `totalreclaw_import_from` (`dry_run=true` first; `_import_batch` for >50 chunks) · `totalreclaw_export` · `totalreclaw_consolidate` (`dry_run` first).

## Diagnostics

- `totalreclaw_*` tools not visible → plugin not installed or auto-reload didn't fire. Verify via `openclaw plugins list | grep totalreclaw` (or, on a managed service that doesn't expose the CLI, ask the user to confirm via their service's plugins UI). If `totalreclaw` is listed but tools are still absent in your toolset, the gateway probably hasn't completed its auto-restart yet (give it 5-10s) OR `gateway.reload.mode = off` is set — instruct manual restart as fallback: `openclaw gateway restart` (native), `docker restart <your-container-name>` (Docker self-host), or the managed service's restart control. If the plugin is listed and the gateway has been restarted but tools are still missing (container-agent gap, issue #87), shell out to `openclaw totalreclaw pair generate --url-pin-only` on the gateway host and hand the user the `url` + `pin` from the JSON payload — OR `openclaw totalreclaw onboard --pair-only` (same flow, returns `pair_url` instead of `url`, issue #95). If your shell doesn't reach the gateway host (managed service), tell the user to install/enable the plugin via their service's plugins UI and restart the gateway via the service's restart control. Do NOT suggest the bare `openclaw totalreclaw onboard` (leaks phrase on stdout — deprecated in rc.18, removed next RC).
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the pair step.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.
- `No LLM available for auto-extraction` at startup → provider key unreachable; check `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or plugin config `extraction.llm`.

## Tool surface

`totalreclaw_pair` (ONLY setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_status` · `_upgrade` · `_migrate` · `_import_from` · `_import_batch` · `_consolidate` · `_onboarding_start` (pointer to local-terminal wizard, for users explicitly rejecting the browser flow) · `_report_qa_bug` (RC only).
