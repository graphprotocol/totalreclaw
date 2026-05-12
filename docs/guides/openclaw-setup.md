# TotalReclaw for OpenClaw

> **Audience map.** This guide serves three readers — **(a) humans** wanting to understand what their agent is about to do, **(b) the agent itself** that just fetched this URL, and **(c) Pedro / future-Pedro doing QA on a fresh VM**. The lean agent-executable version (~5 KB, no rationale prose) lives at [`openclaw-setup-quickstart.md`](./openclaw-setup-quickstart.md). If you are an agent and the user only pasted this guide URL, prefer the quickstart for execution and use this file for context.

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. The fastest path is a single chat message — your agent fetches this guide, you and your agent install the plugin together, and the agent walks you through setting up your TotalReclaw account.

## What this is and where it comes from

TotalReclaw is open-source memory tooling for OpenClaw. The plugin is published by `p-diogo` (GitHub-verified) at:

- npm: [`@totalreclaw/totalreclaw`](https://www.npmjs.com/package/@totalreclaw/totalreclaw)
- ClawHub skill: `totalreclaw`
- Source code: <https://github.com/p-diogo/totalreclaw>

All cryptographic operations run on your device. Encrypted bundles are forwarded through the TotalReclaw relay (`api.totalreclaw.xyz` by default for both stable and RC builds; set `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` to opt into the staging relay). The recovery phrase is generated and stays inside your browser — neither the agent, the relay, nor anyone else ever sees it.

If you already have OpenClaw running, paste this into your OpenClaw chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

Your agent reads this page, runs two install commands ([`openclaw skills install`](https://docs.openclaw.com/cli/skills) and [`openclaw plugins install`](https://docs.openclaw.com/cli/plugins) — both from the OpenClaw CLI you already have), and hands you an account-setup URL + 6-digit PIN. You open the URL, generate or restore a 12-word recovery phrase in your browser, confirm the PIN, and your account is ready.

This guide is the playbook your agent follows; you can read along to see what each step does.

---

## Agent instructions (read this if you are an OpenClaw agent that just fetched this URL)

You have been asked to install TotalReclaw. Keep the interaction focused — run each step, report progress on the user-visible lines below, and surface errors when they happen.

Treat each command as a step you and the user complete together. Confirm what you see at each phase. If anything looks wrong, surface it and stop — don't push through.

### How to communicate during install

The user-visible lines below are what the user actually needs to see. On transports like Telegram, intermediate narration ("Let me check…", "Plugin loaded. Let me verify…") shows up as visible edit churn as your tool calls progress — often confusing rather than helpful. The default install path should produce **5–6 short lines TOTAL** in chat.

For errors, one tight line is enough: the error text plus what you're doing about it. Don't pre-narrate steps that succeed.

### What you will run, and why each step is safe

1. `openclaw plugins install @totalreclaw/totalreclaw` — installs the published plugin from npm under the `@totalreclaw` scope owned by `p-diogo`. The plugin runs inside OpenClaw's plugin sandbox. Upstream docs: <https://docs.openclaw.com/cli/plugins>.
2. `openclaw skills install totalreclaw` — fetches the SKILL.md descriptor from ClawHub. Read-only metadata; no code executes from this step. Upstream docs: <https://docs.openclaw.com/cli/skills>.
3. After the gateway auto-reloads, you will call `totalreclaw_pair` (a tool the plugin registers) to mint a one-time URL + PIN. The user opens the URL in their own browser to enter or generate a recovery phrase. The phrase never enters chat or your shell.

You are not asked to handle the recovery phrase. You are not asked to bypass auth. Pasting the install URL IS the consent moment for installing the plugin; the URL + PIN you hand back from `totalreclaw_pair` is the real consent moment for account creation, since the user has to open the page in their browser to continue. There is no separate "yes/no" gate before account setup. If the user pastes a phrase in chat anyway, refuse to proceed — see "Phrase safety" below.

### User-visible lines

These are the lines to emit, in order. Substitute the bracketed placeholders with the real values. No prefaces or suffixes needed around them.

1. (Step 1 — start; emit BEFORE any shell tool call so the user knows work is happening)
   > Setting up TotalReclaw — this takes about a minute…
2. (Step 2 — after install + `tr status --json` confirms CLI works, hybrid mode)
   > ✓ Installed (totalreclaw &lt;version&gt;, hybrid mode)
3. (Step 3 — only if existing credentials were detected, then stop)
   > ✓ TotalReclaw is already set up.
4. (Step 4 — account-setup URL + PIN, immediately after `tr pair --json` returns — no consent gate)
   > Open &lt;url&gt; in your browser. Enter PIN &lt;pin&gt;. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
5. (Step 5 — done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.

That is the complete user-facing transcript for a healthy install. Expected line count for a fresh install + setup is **4–5 lines** (line 3 is mutually exclusive with lines 4-5; the `/totalreclaw-restart` cycle is no longer part of the primary path under hybrid mode).

### When to break silence (errors only)

If a step **actually fails**, surface the failure in one tight line — the error text plus what you're doing about it. Examples:

- `Plugin install returned ENOENT — your shell doesn't have access to the OpenClaw CLI here. I'll stop and explain.`
- `Plugin installed but tools didn't bind after auto-reload. Asking you to restart manually.`
- `Account-setup URL returned 404 — the PIN may have expired. Generating a fresh URL + PIN.`

Do NOT pre-narrate diagnostics that succeed. Do NOT narrate retries that succeed silently. Do NOT echo command output unless the user asks for it.

### Actions that break pair flows — avoid these (3.3.10-rc.3 notes from 2026-05-05 QA)

These patterns have broken pair flows in previous QA rounds:

1. **Do NOT issue `/totalreclaw-restart`. Do NOT signal the gateway. Do NOT `docker restart`.** The plugin auto-patches the config and emits its own restart-required warn IF (and only if) one is needed. The gateway will restart itself when its reload pipeline decides to. **You restarting it kills the in-flight `setsid -f` pair subprocess and surfaces as 502 on the browser respond.**

2. **Do NOT write to `~/.openclaw/openclaw.json`** after the install. The plugin's `patchOpenClawConfig()` already wrote `plugins.slots.memory`, `plugins.entries.totalreclaw.hooks.allowConversationAccess`, and `channels.telegram.streaming.mode` for you. Any further config write triggers a fresh deferred reload that eventually fires SIGUSR1 and kills pair. Forbidden tool calls: `openclaw config set …`, any redirect / sed / jq into `openclaw.json`.

3. **Do NOT re-render the QR.** `tr pair --json` returns `qr_ascii` (Unicode) AND the pair page renders a real PNG. Saving the QR to `/tmp/totalreclaw-pair-qr.png` triggers OpenClaw's media-access policy block and Telegram delivery fails with `LocalMediaAccessError`. Just emit the URL + PIN as user-visible line 3 — that is your only QR responsibility.

4. **Do NOT invoke `tr pair --json` in the foreground.** A foreground pair subprocess is killed when your shell-tool exec returns. The pair WS dies → relay returns 502. Step 4 below uses `setsid -f` for exactly this reason — copy that block VERBATIM. Do not "simplify". Do not drop the `setsid -f`. Do not omit `< /dev/null`.

5. **Avoid narrating internals.** Examples from past QAs that broke the experience: "Now let me verify the CLI…" / "Gateway needs a restart, let me restart it" / "Let me find and signal the gateway process" / "The CLI is at the npm path, not the extensions path" / "CLI reports itself as version X (lag)" / "Now let me also add the required post-install config" / "Let me save the QR as a PNG". These are internal details — emit only the 4–5 user-visible lines.

### Phrases to avoid in agent prose

Unless the user explicitly asks how the install works, the following words / patterns tend to surface as edit churn in transports like Telegram during install + setup:

- "Let me check that…"
- "Plugin loaded. Let me verify…"
- "I'll now…"
- "Quick fix…"
- "However, I don't see…"
- "I need to use…"
- "Should I /totalreclaw-restart" / "Want me to restart" / "Let me check if the tool is bound" / "I need permission to restart" / "Do you have a public URL"
- "Now installing…" / "Got it" / "In parallel…" / "Step 1a/1b/1c"
- `manifest`, `descriptor`, `file-watcher`, `polling`, `auto-reload-detect`, `find`, `lookup`, `detect`, `probing`, `let me try`

Substitute every one of those with silence — the next user-visible line in the set, or nothing.

If the user explicitly asks ("what's it doing?", "why so quiet?", "show me the commands"), then narrate freely — the silence rule only governs the default install path.

### Step 1 — Install the skill + plugin (silent — emit user-visible line 1, then no prose until line 2)

Run via your shell tool, in this exact order, **without writing prose about them**. Emit user-visible line 1 (`Installing TotalReclaw…`) once at the start, then stay silent until both commands succeed.

```bash
openclaw plugins install @totalreclaw/totalreclaw
openclaw skills install totalreclaw
```

**Plugin BEFORE skill is deliberate** (3.3.4-rc.1, post-Pedro QA): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install. Plugin-first lets the plugin land cleanly, then the skill's reload picks it up on the next gateway loop. If you accidentally do skill-first and the plugin install dies mid-flight, just retry `openclaw plugins install @totalreclaw/totalreclaw` once — it's idempotent.

**No extra config steps needed.** As of 3.3.9-rc.2 the plugin's `register()` auto-patches `~/.openclaw/openclaw.json` with the keys OpenClaw 2026.5.x looks for (`plugins.slots.memory`, `plugins.entries.totalreclaw.hooks.allowConversationAccess`, `channels.telegram.streaming.mode`, `plugins.bundledDiscovery`, `plugins.allow`, `plugins.installs.totalreclaw`). As of 3.3.12-rc.6 the plugin auto-fires SIGUSR1 to its own PID after the patch, so the gateway in-process restart picks up the new keys without any manual `/totalreclaw-restart` from the user — hooks register cleanly on the first session post-install.

> **Setup is plugin-driven as of 3.3.12-rc.7.** When the plugin loads with no `~/.totalreclaw/credentials.json`, it autonomously opens a relay pair session and writes `{url, pin, sid, expires_at_ms}` to `~/.totalreclaw/.pair-pending.json`. A `before_agent_start` hook then injects the URL+PIN into the agent's context block on every turn. The agent's only job is to read those values from the injected context and surface them verbatim to you — it does NOT need to know how `tr pair --json` works or run any pair CLI itself. This eliminates the "agent hallucinates a pair URL" failure mode seen in rc.6 QA.

> **Auto-extraction safety net (3.3.11-rc.1).** Even when the `agent_end` hook is gated by an upstream policy, the plugin runs a filesystem-polling backup that watches `~/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl` every 60 s and runs the same extraction pipeline (NOT a hook event, so it's never gated). On 3.3.12-rc.6+ the hook fires alongside the poller; offset-based dedup prevents double-extraction.

> **Plugin status caveat — OpenClaw 2026.5.7.** On the freshest OpenClaw releases the install path can occasionally land the plugin as `Status: disabled` after restart (upstream config-strip-on-restart). If `openclaw plugins list` shows totalreclaw as disabled, run `openclaw plugins enable totalreclaw` once and continue. The plugin remains on disk; only the registry flag needs flipping.

Notes:
- For an RC build, replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc` (latest release candidate) or pin a version like `openclaw plugins install @totalreclaw/totalreclaw@3.3.12-rc.9`.
- If install reports `already exists` or non-zero on a re-run (the plugin is already on disk from a previous attempt), add `--force`: `openclaw plugins install @totalreclaw/totalreclaw@rc --force` and `openclaw skills install totalreclaw --force`.
- If either command fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), STOP and tell the user verbatim: *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` before continuing to Step 2.

### Step 2 — Verify CLI works (hybrid-primary, 3.3.9-rc.1+) — emit user-visible line 2 on success

**Do NOT wait for tool binding. Do NOT check whether `totalreclaw_pair` is in your toolset.** Under OpenClaw 2026.5.2+, non-bundled plugin tools may be stripped (issue #223). The `tr` CLI is the primary path and works independently.

Resolve the CLI path once and reuse (3.3.10-rc.2 — modern OpenClaw 2026.5.x installs npm-managed plugins under `~/.openclaw/npm/...`, not `~/.openclaw/extensions/...`):
```bash
TR_CLI="$(ls -d ~/.openclaw/npm/node_modules/@totalreclaw/totalreclaw/dist/tr-cli.js ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js 2>/dev/null | head -1)"
node "$TR_CLI" status --json
```

Parse the JSON. When you receive `{"version": "...", "hybrid_mode": true, ...}`, emit user-visible line 2 (`✓ Installed (totalreclaw <version>, hybrid mode)`) with the version substituted. Then proceed silently to Step 3.

If `tr status` exits non-zero or `$TR_CLI` is empty, wait 5s and retry once (the install may still be completing). If it still fails, tell the user the install didn't land and retry `openclaw plugins install @totalreclaw/totalreclaw@rc`.

**Note on `/totalreclaw-restart`**: this slash command is still registered for environments where users need a manual reload (managed services, special configs). If you need to issue it for some reason, issue the literal slash command `/totalreclaw-restart` — nothing else. But under hybrid-primary, restarting to restore tool binding is no longer needed — `tr` CLI works regardless.

### Step 3 — Check for existing credentials (silent unless found)

Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit user-visible line 4 (`✓ TotalReclaw is already set up.`) and stop — the user is already set up. Otherwise continue silently.

### Step 4 — Set up the user's TotalReclaw account IMMEDIATELY — no consent gate (emit user-visible line 5)

Pasting the install URL was the consent moment for installing the plugin. The URL + PIN you're about to hand the user is the real consent moment for account creation — they have to open the page in their browser to proceed. There is NO separate yes/no gate. Do NOT ask "Would you like to set up an account?" — run pair immediately.

Important (3.3.10-rc.2): always invoke `tr pair --json` via `setsid -f` so the WS-holding subprocess is in its own session and survives any post-install gateway SIGUSR1 reload. A foreground `node tr-cli pair` is killed mid-flight when the gateway fires its deferred restart, surfacing as `Gateway could not finish pairing (502). The agent timed out or the ciphertext failed to decrypt — ask the agent to retry pairing.` on the user's browser.

```bash
PAIR_OUT="/tmp/tr-pair-out-$(date +%s).json"
setsid -f node "$TR_CLI" pair --json > "$PAIR_OUT" 2>&1 < /dev/null
sleep 2
cat "$PAIR_OUT"
```

Parse the JSON `{"v":1,"sid":"...","url":"...","pin":"...","mode":"...","expires_at_ms":...,"qr_ascii":"..."}`. Emit user-visible line 5 (`Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply done once it's sealed.`) with `<url>` and `<pin>` substituted. Do not pre-narrate — line 5 itself is the only thing the user needs to see.

If the cat output is empty after 2s, sleep 2 more seconds and try again (relay handshake can take up to 4s on a cold network). If `tr pair --json` is unavailable (old install path), fall back to the `totalreclaw_pair` native tool or `openclaw totalreclaw pair generate --url-pin-only`.

The pair payload includes a `qr_ascii` field (Unicode block-char QR, ~43 lines × 43 cols) that renders correctly in monospace fonts on every modern transport. Emit it inline in a triple-backticked code block right above user-visible line 5 if your transport can render code blocks. The pair page renders a real PNG QR alongside, so users on a desktop browser see both. Do NOT save QR PNGs locally (`LocalMediaAccessError`). The QR encodes only the URL; the PIN is dual-channel — typed separately.

### Step 5 — Verify and confirm (silent until line 6)

After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN likely expired — re-run `tr pair --json` and resend line 5 once. If present, emit user-visible line 6 (with the free-tier confirmation) and stop.

### Phrase safety

The recovery phrase is the user's only identity in TotalReclaw — it derives all encryption keys and there is no recovery mechanism if lost.

Do not echo, generate, or ask the user to paste a recovery phrase in chat. Do not invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool — their stdout enters LLM context. The only agent-facilitated account-setup path is the `totalreclaw_pair` tool (name kept for backward compatibility). If the user pastes a phrase in chat anyway, let them know it should be considered compromised and call `totalreclaw_pair` with `mode=generate` to create a fresh wallet.

---

## What's happening (for the human reader)

1. `openclaw skills install totalreclaw` — fetches TotalReclaw's SKILL.md from ClawHub and registers it under `~/.openclaw/workspace/skills/totalreclaw/`. The agent picks it up on next config reload.
2. `openclaw plugins install @totalreclaw/totalreclaw` — installs the plugin from npm (the `latest` dist-tag is always current stable; use `@rc` for the latest release candidate).
3. OpenClaw's config-watcher detects the `plugins.*` change in `~/.openclaw/config.yaml` and (under default `gateway.reload.mode = "hybrid"`) triggers a graceful SIGUSR1 restart within 1-3s. The new tools bind automatically.
4. The agent calls `totalreclaw_pair`, which generates an ephemeral x25519 keypair on the gateway and a 6-digit PIN. You get a URL + PIN.
5. You open the URL. The account-setup page has two tabs: **Generate new** (the browser creates a fresh BIP-39 12-word phrase locally using `crypto.getRandomValues`) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, click seal.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives an AES-256-GCM key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey back. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

First real interaction downloads a ~216 MB embedding model (cached locally, one-time).

---

## Prerequisites

- OpenClaw v3.2.0+ with the gateway running
- An up-to-date browser with WebCrypto x25519 + AES-GCM (Safari 17.2+ or Chromium 133+)

---

## Managed OpenClaw service (no terminal, no agent shell)

If you're on a managed / hosted OpenClaw service that doesn't expose host shell to the agent, install via the service's web UI instead:

1. In your service's control panel, find the **Plugins** (or **Skills**) panel and search for `totalreclaw`. Install and enable it.
2. If the service exposes a separate restart control, use it. Many managed services apply plugin changes transparently.
3. Return to chat and paste the same canonical message:

   > **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

   The agent will detect that the plugin is already loaded, skip Steps 1-2, and jump straight to account setup.

The browser-side crypto and account-setup flow are identical to self-hosted setups; only the install step differs.

---

## Fully manual (CLI only — last resort)

If you can't or won't use the chat flow (self-hosted only — managed services don't expose the host shell):

```bash
openclaw plugins install @totalreclaw/totalreclaw            # stable
# Or for an RC: @totalreclaw/totalreclaw@rc
# Under the default config (`gateway.reload.mode = "hybrid"`), OpenClaw's file-watcher
# auto-restarts the gateway within 1-3s of the install — no manual restart needed.
# Verify with: `openclaw plugins list | grep totalreclaw`.
```

The plugin self-restarts the gateway via SIGUSR1 when needed. You should not need to run `openclaw gateway restart` or `docker restart` yourself; if a restart appears stuck, file an issue rather than hand-restarting.

Then in chat: *"Set up TotalReclaw"* — the agent will call `totalreclaw_pair` and hand you the URL + PIN. Open the URL in your browser to enter or generate your phrase.

> Pin a specific RC with `openclaw plugins install @totalreclaw/totalreclaw@3.3.12-rc.9`. Check what each tag resolves to: `npm view @totalreclaw/totalreclaw dist-tags`. Keep skill and plugin on the same version family (both stable or both RC).

<details>
<summary>From-source install (for plugin development — self-host only)</summary>

```bash
git clone https://github.com/p-diogo/totalreclaw.git
openclaw plugins install ./totalreclaw/skill/plugin
```

Requires terminal access on the gateway host. Not available on managed services.

</details>

---

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

---

## What happens automatically

| Hook | What it does |
|------|-------------|
| **Auto-recall** | Searches your vault before every message, injects relevant memories into context. |
| **Auto-extract** | Every 3 turns, extracts important facts (preferences, decisions, context) and stores them encrypted. |
| **Pre-compaction flush** | Before the context window is compacted, all pending facts are extracted and saved. |
| **Session debrief** | At session end, captures up to 5 session-level summaries. |
| **Quota warning at ≥80%** | When monthly free-tier writes cross 80%, a one-line warning is injected at conversation start so you know before you hit the limit. |

---

## Explicit tools

Ask the agent naturally; the plugin picks the right tool.

| Tool | Example prompt |
|------|---------------|
| **Remember** | "Remember that I prefer PostgreSQL over MySQL" |
| **Recall** | "What do you remember about my database choices?" |
| **Forget** | "Forget what you know about my old email address" |
| **Pin / Unpin** | "Pin that -- it's important" / "Unpin the note about my old editor" |
| **Retype** | "That should be a preference, not a fact" (types: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`) |
| **Set scope** | "File that under work" (scopes: `work`, `personal`, `health`, `family`, `creative`, `finance`, `misc`) |
| **Export** | "Export all my TotalReclaw memories as plain text" |
| **Status** | "What's my TotalReclaw status?" — surfaces tier, monthly writes used / limit, reset date, upgrade URL |
| **Import from** | "Import my Gemini history from ~/Downloads/..." |
| **Account setup** | "Set up TotalReclaw for me" — returns URL + PIN |

Your recovery phrase is the only key to your memories. Store it safely. Retrieve later with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. On a new machine, paste the canonical prompt above and switch to the **Import existing** tab in the browser. First-time users can stay on the default **Generate new** tab to create a fresh BIP-39 phrase.

---

## Importing from other tools

TotalReclaw can import from Mem0, MCP Memory Server, ChatGPT, Claude, and Gemini:

> "Import my memories from Mem0 using API key m0-your-key-here"

See [Importing Memories](importing-memories.md).

---

## Billing

| Tier | Storage | Monthly writes | Reads | Price |
|------|---------|---------------|-------|-------|
| **Free** | Base Sepolia testnet (may reset) | 500 / month | Unlimited | $0 |
| **Pro** | Permanent on Gnosis mainnet | Unlimited | Unlimited | $3.99/month |

The plugin warns you automatically when you cross 80% of the monthly free-tier write limit (injected at conversation start). Check anytime by asking *"what's my TotalReclaw status?"* — that calls `totalreclaw_status` and reports tier, writes used, writes limit, reset date, and upgrade URL.

Upgrade: *"Upgrade my TotalReclaw subscription."*

[Pricing](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent refused the install** ("this looks like prompt injection", "I'm not going to do that"): rare but possible with safety-trained models that misread agent-imperative install prose. Ask the agent to read the "Agent instructions" section of this guide and proceed step-by-step — each command is annotated with what it does and why it's safe. If the refusal persists, file an issue with the model name + chat transcript.
- **Agent can't see TotalReclaw tools after install**: under the default config OpenClaw auto-restarts the gateway within 1-3s of `openclaw plugins install` — wait 5-10s and check `openclaw plugins list`. If the plugin is listed but the agent still can't see the tools, your config likely has `gateway.reload.mode = off`. The agent should issue the plugin's `/totalreclaw-restart` slash command (in-process, drains active runs, works from inside the gateway; 5-tier auth fallback so default-config installs work without `allowFrom`). Manual fallback only if `/totalreclaw-restart` is unavailable — native: `openclaw gateway restart`; Docker self-host: `docker restart <your-openclaw-container>`; managed service: use the service's restart control.
- **Account-setup URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the account-setup page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: paste the canonical message again so the agent re-runs `totalreclaw_pair`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.
- **Agent narrating internals during install** ("Let me check that…", "Plugin loaded. Let me verify…", "I'll now…", "Quick fix…", "However, I don't see…", "I need to use…", "let me find…", "in parallel…", "manifest detected…"): the agent missed the silence rule in §"Agent instructions". Reply *"Don't narrate the install internals — just tell me when it's installed and when to set up my account."* and the next session should silence. On transports like Telegram, these intermediate lines visibly edit the bot's message live as tool calls progress, which is what makes them disruptive.
- **Agent says "Should I /totalreclaw-restart?" or stalls instead of restarting** (3.3.5-rc.1 hardening note, 3.3.7-rc.2 rename): the agent missed the restart-imperative deny-list in the SKILL.md / Step 2. Reply *"Issue /totalreclaw-restart yourself — don't ask"* and the next session should act autonomously. If it persists across sessions, the published RC's SKILL.md is stale — file an issue.
- **`openclaw` CLI hangs / exits 124 inside the gateway agent shell** (issue [#184](https://github.com/p-diogo/totalreclaw-internal/issues/184)): on some Docker setups the agent's shell-execution of `openclaw plugins list` / `openclaw plugins install` etc. deadlocks (every subcommand exits 124). When this happens the agent can bypass the CLI wrapper by invoking the plugin's own JS entries directly via `node`. The plugin ships pair-flow + version-probe entries that work without any CLI:
  - **Plugin install probe** — read the plugin's filesystem manifests to confirm the install completed: `cat ~/.openclaw/extensions/totalreclaw/.loaded.json` (issue [#186](https://github.com/p-diogo/totalreclaw-internal/issues/186)) reports `{ loadedAt, tools, version }` if `register()` ran cleanly. `.error.json` reports a structured failure if it didn't. These files are the canonical filesystem signal — they don't depend on the deadlocking CLI.
  - **Pair-flow direct-node fallback** — when `openclaw` CLI hangs during `totalreclaw_pair`, the agent (or a shell helper) can run `node ~/.openclaw/extensions/totalreclaw/dist/pair-cli.js --url-pin-only` directly to obtain the universal-relay pair URL. This bypasses the gateway tool-router entirely and writes the resulting URL to stdout (one JSON line, no prompts). Surface that URL to the user the same way `totalreclaw_pair` would.
  - **Restart fallback** — `/totalreclaw-restart` slash command is the autonomous path; manual fallbacks (`openclaw gateway restart`, `docker restart`) require user-side terminal access and are documented above.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

---

## Threat model

After a successful pair, your recovery phrase is stored in plaintext at `~/.totalreclaw/credentials.json` with mode `0600` (owner read/write only). Understanding what this does and doesn't protect is important.

**`chmod 600` protects against:**

- Other Unix user accounts on the same host reading your phrase directly.
- Web server processes, CI runners, or container daemons running as different OS users.

**`chmod 600` does NOT protect against:**

- Root / sudo — system administrators can read any file.
- Same-user processes — any process running as your Unix user can read the file. This includes every shell session, script, and agent running as you.
- Physical disk access (stolen drive, VM snapshot, cloud-provider disk clone).
- Tmpfs / shared-volume mounts — if `credentials.json` is on `/tmp/`, `/dev/shm/`, or similar, it may be readable by other processes in the same container namespace or lost on reboot. The plugin warns on startup if this is detected.
- Cloud-backup leakage — if `~/.totalreclaw/` is inside a folder synced to iCloud, Dropbox, Backblaze, or similar, add it to your backup exclude list.

**Fail-closed enforcement:** The plugin refuses to load if `credentials.json` is found with permissions broader than `0600`. If you see a startup error about insecure permissions, fix it with:

```bash
chmod 600 ~/.totalreclaw/credentials.json
# Then restart your OpenClaw gateway.
```

For the full threat model, mitigations, and the credentials-at-rest roadmap, see [SECURITY.md](../SECURITY.md).

---

## Further reading

- [Feature Comparison](feature-comparison.md)
- [Importing Memories](importing-memories.md)
- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Detailed reference](beta-tester-guide-detailed.md) — env vars, extraction tuning, architecture
- [Security](../SECURITY.md) — threat model and at-rest credential protection
- [totalreclaw.xyz](https://totalreclaw.xyz)
