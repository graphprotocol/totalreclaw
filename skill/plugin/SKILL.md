---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for OpenClaw, then use tr CLI for remember / recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 3.3.9-rc.1
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

## CRITICAL: Relay-based architecture (read before anything else)

**TotalReclaw is RELAY-BASED. All memories require a paired account on the staging or production relay (depending on RC vs stable). There is NO local-only mode. NEVER claim memories are stored locally without an account.**

Forbidden vocabulary — these claims are FALSE and MUST NEVER appear in your responses:
- "local"
- "local-only"
- "stored locally"
- "on disk"
- "without an account"
- "local memory"
- "local storage"
- any phrasing suggesting memories can be stored without pairing a relay account

If you find yourself about to say any of these, STOP. The correct statement is: "TotalReclaw requires a paired relay account to store memories — run `tr pair` to set one up."

## Top-level rule (read this before anything else)

**Emit ONLY the user-visible lines listed below. Do not narrate tool calls. Do not describe what you're doing or about to do. Errors are the only allowed deviation, and only as a single tight line.** The expected user-facing transcript for a healthy install is **4–5 short lines TOTAL**. Anything more is a SKILL violation. Telegram and similar transports edit the bot's messages live as your tool calls progress — every interim "Let me check…" / "Plugin loaded. Let me verify…" line shows up to the user as awkward edit churn, even if you "delete" it. The fix is to never emit it in the first place.

## Hybrid-primary architecture (3.3.9-rc.1)

OpenClaw 2026.5.2 has a tool-policy-pipeline bug (issue #223) that strips non-bundled plugin tools before they reach the agent toolset. **3.3.9-rc.1 makes the `tr` CLI the PRIMARY path for all memory operations.** Native tools (`totalreclaw_*`) are kept for back-compat with older OpenClaw versions where they work, but the agent MUST NOT depend on them. Use `tr` from your shell tool for everything.

**The `tr` binary is installed at:** `~/.openclaw/extensions/totalreclaw/dist/tr-cli.js`
**Always invoke via:** `node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js <cmd> [--json]`
**Never rely on `totalreclaw_*` tool binding** — it may not be available due to issue #223.

## User-visible line set (THIS IS ALL YOU SAY)

Speak only these lines, in order. Substitute the bracketed placeholders with the real values. No prefaces. No suffixes. No narration.

1. (start of install)
   > Installing TotalReclaw…
2. (after install + CLI verify — `tr status` works)
   > ✓ Installed (totalreclaw &lt;version&gt;, hybrid mode)
3. (account-setup URL + PIN, IMMEDIATELY after `tr pair --json` returns — no consent gate)
   > Open &lt;url&gt; in your browser. Enter PIN &lt;pin&gt;. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
4. (done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.

If existing credentials are detected (step "Check for existing credentials" below), emit ONLY this line and stop:
   > ✓ TotalReclaw is already set up.

## Setup flow (hybrid-primary)

1. **Install plugin first, skill second (silent — emit user-visible line 1 once at the start, then no prose until line 2).** Run via your shell tool, in this exact order:
   ```
   openclaw plugins install @totalreclaw/totalreclaw
   openclaw skills install totalreclaw
   ```
   For RC: replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc`. **Plugin BEFORE skill is deliberate** (3.3.4-rc.1 — issue from Pedro's QA on rc.3): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install.

   If either shell call fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), tell the user verbatim *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` and skip to step 3.

2. **Verify CLI works — emit user-visible line 2 when successful.** Run:
   ```
   node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js status --json
   ```
   Parse the JSON. When you get `{"version": "...", "hybrid_mode": true, ...}`, emit user-visible line 2 with the version substituted. **Do NOT wait for tool binding. Do NOT issue `/totalreclaw-restart`. Do NOT check whether `totalreclaw_pair` is in your toolset.** The CLI works independently of tool binding. If `tr status` exits non-zero or the file doesn't exist, wait 5s and retry once — it may still be installing.

3. **Check for existing credentials (silent unless found).** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit *"✓ TotalReclaw is already set up."* and stop.

4. **Set up the user's TotalReclaw account IMMEDIATELY — no consent gate.** As soon as `tr status --json` returns and credentials are not present, run:
   ```
   node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js pair --json
   ```
   This is UNCONDITIONAL. Do NOT ask "Would you like to set up an account?" or "Want me to pair now?". The user installed TotalReclaw — setup is the expected next step. The URL + PIN itself is the consent moment (the user can simply not open the page to opt out). Parse the JSON response `{"url": "...", "pin": "...", "expires_at": "..."}` and emit user-visible line 3. If `tr pair --json` is not available (old install), fall back to `totalreclaw_pair` tool or `openclaw totalreclaw pair generate --url-pin-only`.

5. **Verify and confirm (silent until line 4).** After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — run `tr pair --json` again and resend line 3 once. If present, emit user-visible line 4 and stop.

## `tr` CLI reference (hybrid-primary commands)

Always use `--json` flag when running from shell for machine-parseable output. Plain text mode is for direct user invocation only.

```bash
# Status check (parse JSON to confirm install + onboarding state)
node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js status --json
# Returns: {"version":"3.3.9-rc.1","onboarded":false,"next_step":"pair","tool_count":17,"hybrid_mode":true}

# Pair / account setup
node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js pair --json
# Returns: {"url":"https://...","pin":"123456","expires_at":"2026-05-05T..."}

# Remember a fact
node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js remember --json "I work at Graph Foundation"
# Returns: {"ok":true,"id":"...","claim_count":N}

# Recall memories
node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js recall --json "where does the user work" --limit 5
# Returns: {"results":[{"text":"...","score":0.8},...]}
```

**Note on tool names**: `totalreclaw_remember`, `totalreclaw_recall`, etc. may or may not appear in your toolset depending on OpenClaw version. Under hybrid-primary, you use the `tr` CLI instead. If native tools ARE available (older OpenClaw or future OC fix), they work too — but always prefer `tr` for reliability.

## Rendering the QR on your transport (rc.5+)

When you receive the `totalreclaw_pair` payload (from tool OR `tr pair --json`), render the QR based on your chat transport:

- **Transport supports image attachments** (Telegram, Slack, web chat, any transport with file/image upload): decode `qr_png_b64` from base64 to bytes, save to a tempfile with a `.png` suffix, and attach the tempfile as an image. Also show the URL + PIN as text so users who prefer clicking can still do so.
- **Terminal-only transport** (OpenClaw native CLI chat, bare SSH shell): emit `qr_unicode` inline in your reply. It renders as block characters in most terminal fonts; the user scans from their phone.
- **Web chat with inline-image support**: embed as `<img src="data:image/png;base64,<qr_png_b64>" alt="TotalReclaw account-setup QR">` if the chat client renders HTML; otherwise fall back to attachment.

The QR encodes ONLY the account-setup URL. The 6-digit PIN is a separate out-of-band confirmation — deliberately NOT baked into the QR (dual-channel design: scan + type). Never log, print, or re-emit the PIN outside the user-facing reply. The PIN must NOT end up in any other tool call, log file, or memory store.

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated account-setup paths are (in order of preference): (1) `tr pair --json`; (2) the `totalreclaw_pair` account-setup tool; (3) if both are missing — shell out to EITHER `openclaw totalreclaw pair generate --url-pin-only` (single-line JSON `{v,url,pin,expires_at_ms}`) OR `openclaw totalreclaw onboard --pair-only` (single-line JSON `{v,pair_url,pin,expires_at_ms}`). Both have zero phrase material on stdout (account-setup crypto is x25519-only, not BIP-39). If the user pastes a phrase anyway: tell them it is compromised, re-run `tr pair --json` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X":
  ```
  node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js remember --json "<fact>"
  ```
- First-person factual query ("do I / what's my / where do I…"):
  ```
  node ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js recall --json "<query>" --limit 5
  ```
  Recall FIRST, then answer from returned facts. If 0 results, say so.
- For forget / pin / unpin / retype / set_scope — use native tools if available, or ask user to run `openclaw totalreclaw` subcommand.
- "status" → `tr status --json` (parse version + onboarded + next_step).
- "upgrade" → `totalreclaw_upgrade` native tool (paste Stripe URL verbatim). Post-upgrade testnet → mainnet: `totalreclaw_migrate` with `dry_run` first.
- "import / export / consolidate" → `totalreclaw_import_from` · `totalreclaw_export` · `totalreclaw_consolidate` native tools.

**Forbidden vocabulary** (these are anti-patterns that violate the silence rule):
- "Let me check that…"
- "Plugin loaded. Let me verify…"
- "I'll now…"
- "Quick fix…"
- "However, I don't see…"
- "I need to use…"
- "Should I /totalreclaw-restart" / "Do you have a public URL" / "Want me to restart" / "Let me check if the tool is bound" / "I need permission to restart"
- "Now installing…" / "Got it" / "In parallel…" / "Step 1a/1b/1c"
- "local" / "local-only" / "stored locally" / "on disk" / "without an account" / "local memory" / "local storage"

## Diagnostics

- `tr` CLI not found → plugin not installed or install still in progress. Wait 5s, retry. If still missing, retry `openclaw plugins install @totalreclaw/totalreclaw@rc` once.
- `totalreclaw_*` tools not visible → EXPECTED under OpenClaw 2026.5.2 (issue #223). Use `tr` CLI instead. Do not restart, do not issue `/totalreclaw-restart`, do not issue `/new`.
- If `/totalreclaw-restart` is needed for another reason, issue the literal slash command `/totalreclaw-restart` — nothing else. Do not narrate.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; re-run `tr pair --json` and resend line 3 once.
- `onboarding required` → credentials missing; redo from the account-setup step.
- `quota exceeded` → `tr status --json` or `totalreclaw_status`, then offer `totalreclaw_upgrade`.
- `No LLM available for auto-extraction` at startup → provider key unreachable; check `~/.openclaw/agents/<agent>/agent/auth-profiles.json` or plugin config `extraction.llm`.

## Required post-install config (OpenClaw 2026.5.2+)

Add to `~/.openclaw/openclaw.json` after installing:
```json
{
  "plugins": {
    "allow": ["totalreclaw"],
    "entries": {
      "totalreclaw": {
        "hooks": { "allowConversationAccess": true }
      }
    }
  }
}
```

`plugins.allow` suppresses the "plugins.allow is empty" warning. `hooks.allowConversationAccess` unlocks typed hooks (`agent_end`, etc.) for auto-extraction.

## Tool surface

Hybrid-primary: `tr remember` · `tr recall` · `tr pair` · `tr status` (primary path for all agent ops)
Native fallback (when available): `totalreclaw_pair` · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_status` · `_upgrade` · `_migrate` · `_import_from` · `_import_batch` · `_consolidate` · `_onboarding_start` · `_report_qa_bug` (RC only)
