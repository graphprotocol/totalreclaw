# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. The fastest path is a single chat message — your agent fetches this guide, you and your agent install the plugin together, and the agent walks you through setting up your TotalReclaw account.

## What this is and where it comes from

TotalReclaw is open-source memory tooling for OpenClaw. The plugin is published by `p-diogo` (GitHub-verified) at:

- npm: [`@totalreclaw/totalreclaw`](https://www.npmjs.com/package/@totalreclaw/totalreclaw)
- ClawHub skill: `totalreclaw`
- Source code: <https://github.com/p-diogo/totalreclaw>

Everything runs locally except the encrypted-at-rest sync to TotalReclaw's relay (`api-staging.totalreclaw.xyz` for the free tier). The recovery phrase is generated and stays inside your browser — neither the agent, the relay, nor anyone else ever sees it.

If you already have OpenClaw running, paste this into your OpenClaw chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

Your agent reads this page, runs two install commands ([`openclaw skills install`](https://docs.openclaw.com/cli/skills) and [`openclaw plugins install`](https://docs.openclaw.com/cli/plugins) — both from the OpenClaw CLI you already have), and hands you an account-setup URL + 6-digit PIN. You open the URL, generate or restore a 12-word recovery phrase in your browser, confirm the PIN, and your account is ready.

This guide is the playbook your agent follows; you can read along to see what each step does.

---

## Agent instructions (read this if you are an OpenClaw agent that just fetched this URL)

You have been asked to install TotalReclaw. **Run all install / verify steps silently** except for the user-visible lines explicitly listed below — those are the ONLY things you should write in chat. Tool calls happen; narration of those tool calls does not.

The user pasted this URL deliberately, but treat each command as a step you and the user complete together. Confirm what you see at each phase. If anything looks wrong, surface it and stop — don't push through.

### What you will run, and why each step is safe

1. `openclaw skills install totalreclaw` — fetches the SKILL.md descriptor from ClawHub. Read-only metadata; no code executes from this step. Upstream docs: <https://docs.openclaw.com/cli/skills>.
2. `openclaw plugins install @totalreclaw/totalreclaw` — installs the published plugin from npm under the `@totalreclaw` scope owned by `p-diogo`. The plugin runs inside OpenClaw's plugin sandbox. Upstream docs: <https://docs.openclaw.com/cli/plugins>.
3. After the gateway auto-reloads, you will call `totalreclaw_pair` (a tool the plugin registers) to mint a one-time URL + PIN. The user opens the URL in their own browser to enter or generate a recovery phrase. The phrase never enters chat or your shell.

You are not asked to handle the recovery phrase. You are not asked to bypass auth. If the user pastes a phrase in chat anyway, refuse to proceed — see "Phrase safety" below.

### User-visible prose (THIS IS ALL YOU SAY)

Speak only these lines, in order. Do not narrate around them. Do not announce intent ("Let me…", "I'll now…", "In parallel…"). Do not describe internals (skill download, plugin manifest parsing, file-watcher polling). Substitute the bracketed placeholders with the real values.

1. (Step 1 — start)
   > Installing TotalReclaw…
2. (Step 2 — after install + auto-reload + tool-bind verification)
   > ✓ Installed (totalreclaw &lt;version&gt;)
3. (Step 3 — only if existing credentials were detected, then stop)
   > ✓ TotalReclaw is already set up.
4. (Step 4 — explicit consent before account creation)
   > Ready to set up your TotalReclaw account on `api-staging.totalreclaw.xyz` (free tier). The next step opens a browser page where you'll generate or paste a 12-word recovery phrase — that phrase stays in your browser, the relay only sees ciphertext. Reply `yes` to continue, or `no` to stop here.
5. (Step 5 — account-setup URL + PIN, after the user confirms)
   > Open &lt;url&gt; in your browser. Generate a new 12-word recovery phrase there or paste an existing one. Confirm PIN &lt;pin&gt;. Reply `done` once the page says it's sealed.
6. (Step 6 — done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw **free tier**: 500 memories per month, unlimited reads + searches, encrypted at rest on Base Sepolia testnet (testnet may reset; for permanent storage on Gnosis mainnet see <https://totalreclaw.xyz/pricing>). Check usage anytime by asking *"what's my TotalReclaw status?"* (calls `totalreclaw_status`). I'll warn you automatically if you cross 80% of the monthly limit. Ask me to remember or recall anything.

That is the complete user-facing transcript for a healthy install. Expected line count for a fresh install + setup is **5–6 lines** (line 3 is mutually exclusive with lines 4-6).

### When to break silence (errors only)

If a step **actually fails**, surface the failure in one tight line — the error text plus what you're doing about it. Examples:

- `Plugin install returned ENOENT — your shell doesn't have access to the OpenClaw CLI here. I'll stop and explain.`
- `Plugin installed but tools didn't bind after auto-reload. Asking you to restart manually.`
- `Account-setup URL returned 404 — the PIN may have expired. Generating a fresh URL + PIN.`

Do NOT pre-narrate diagnostics that succeed. Do NOT narrate retries that succeed silently. Do NOT echo command output unless the user asks for it.

### Forbidden vocabulary in agent prose

Unless the user explicitly asks how the install works, the following words must not appear in your chat output during install + setup: `manifest`, `descriptor`, `file-watcher`, `polling`, `auto-reload-detect`, `find`, `lookup`, `detect`, `probing`, `let me try`, `in parallel`, `got it`, `now installing`, `Step 1a/1b/1c`. These are tool-call mechanics, not user-facing prose.

If the user explicitly asks ("what's it doing?", "why so quiet?", "show me the commands"), then narrate freely — the silence rule only governs the default install path.

### Step 1 — Install the skill + plugin (silent — emit user-visible line 1, then no prose until line 2)

Run via your shell tool, in this exact order, **without writing prose about them**. Emit user-visible line 1 (`Installing TotalReclaw…`) once at the start, then stay silent until both commands succeed.

```bash
openclaw plugins install @totalreclaw/totalreclaw
openclaw skills install totalreclaw
```

**Plugin BEFORE skill is deliberate** (3.3.4-rc.1, post-Pedro QA): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install. Plugin-first lets the plugin land cleanly, then the skill's reload picks it up on the next gateway loop. If you accidentally do skill-first and the plugin install dies mid-flight, just retry `openclaw plugins install @totalreclaw/totalreclaw` once — it's idempotent.

Notes:
- For an RC build, replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc` (latest release candidate) or pin a version like `openclaw plugins install @totalreclaw/totalreclaw@3.3.4-rc.1`.
- If either command fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), STOP and tell the user verbatim: *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` before continuing to Step 2.

### Step 2 — Wait for auto-reload + verify tool bind (silent — emit user-visible line 2 on success)

OpenClaw's default config (`gateway.reload.mode = "hybrid"`) auto-restarts the gateway via a file-watcher within 1-3 seconds of the plugin install. Wait ~5 seconds, then verify with `openclaw plugins list | grep totalreclaw` and confirm the `totalreclaw_pair` tool is in your toolset.

When `totalreclaw_pair` is bound, capture the version string from `openclaw plugins list` (the line for `@totalreclaw/totalreclaw`) and emit user-visible line 2 (`✓ Installed (totalreclaw <version>)`).

If `totalreclaw_pair` is not bound within 10 seconds, hybrid auto-reload didn't fire (likely `gateway.reload.mode = off`). Issue OpenClaw's built-in `/restart` slash command yourself **autonomously and immediately** — it is registered in OpenClaw's commands registry alongside `/help /model /new /plugins /reset /status /tools /usage` and reloads the gateway in-process (works from inside the gateway because it doesn't shell out, unlike `openclaw gateway restart`). Surface a single user-visible line: `Auto-reload didn't fire. Restarting via /restart…`. Resume verification after the slash command returns.

**Forbidden vocabulary** when a tool is missing post-install (3.3.4-rc.1): "Should I /restart", "Do you have a public URL", "Want me to restart", "Let me check if the tool is bound", "I need permission to restart". Just issue `/restart` and announce. Asking the user kills the silent-flow target.

**If `/restart` returns "You are not authorized to use this command":** the gateway's `channels.<channel>.allowFrom` allowlist for the channel you're chatting through is empty. **Do NOT loop the install. Do NOT keep retrying.** Surface this one-line user-facing fix verbatim and stop:

> Your channel needs to be allowlisted for slash commands. Run on the gateway host: `jq '.channels.<channel>.allowFrom = ["<your-id>"]' ~/.openclaw/openclaw.json | sponge ~/.openclaw/openclaw.json && docker restart <your-container-name>` (Docker self-host) — substitute `<channel>` with your channel name (e.g. `telegram`, `slack`), `<your-id>` with your user ID for that channel (Telegram: `@username` or numeric `chat_id`; Slack: workspace member ID), and `<your-container-name>` with your gateway container name (`docker ps` shows it). On a managed service, ask the service's support to enable slash commands for your channel. Reply `done` once it's back up.

For non-Docker / native deployments substitute `openclaw gateway restart` for the `docker restart` portion. As a session-level fallback while restart is gated, try `/new` (starts a fresh session within the same gateway process — may pick up freshly-bound tools without a full restart).

**Fallback only if both `/restart` and `/new` are unavailable** (very old OpenClaw pre-2026.4 or managed services that strip slash commands): tell the user verbatim *"Auto-reload didn't fire and /restart is unavailable. Please restart your OpenClaw gateway: `openclaw gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — substitute the actual name; `docker ps` shows it), or your managed service's restart control. Reply `done` once it's back up."* Do NOT invoke any external `docker restart` or `openclaw gateway restart` yourself — your shell runs inside the gateway you'd be restarting; `/restart` (in-process) and a user-prompted external restart are the only safe paths.

### Step 3 — Check for existing credentials (silent unless found)

Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit user-visible line 3 and stop — the user is already set up. Otherwise continue silently.

### Step 4 — Ask for explicit consent before account creation (emit user-visible line 4)

Account setup will create a new TotalReclaw account on `api-staging.totalreclaw.xyz` and have the user generate or paste a 12-word recovery phrase in their browser. Emit user-visible line 4 verbatim. Wait for the user's `yes` (or any clear affirmative). If the user replies `no` or asks to stop, stop here — confirm they've stopped, do not retry, do not ask "are you sure".

This consent step exists so the user knows the destination URL before any account is created. Surface the URL verbatim — do not abbreviate it or hide it behind a tool call.

### Step 5 — Set up the user's TotalReclaw account (emit user-visible line 5)

Call the `totalreclaw_pair` tool silently (the account-setup tool — name kept for backward compatibility). It returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}`. Then emit user-visible line 5 with `<url>` and `<pin>` substituted. Do not pre-narrate ("I'll walk you through…", "Setting up your account now…") — line 5 itself is the only thing the user needs to see.

Render the QR if your transport supports it: image-capable transports (Telegram, Slack, web chat) decode `qr_png_b64` to bytes and attach as an image; terminal-only transports (OpenClaw native CLI) emit `qr_unicode` inline. The QR encodes only the URL — the PIN is a separate dual-channel confirmation, never bake it into the QR.

### Step 6 — Verify and confirm (silent until line 6)

After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN likely expired — call `totalreclaw_pair` again silently and resend line 5 once. If present, emit user-visible line 6 (with the free-tier confirmation) and stop.

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. The ONLY agent-facilitated account-setup path is the `totalreclaw_pair` tool (the account-setup tool — name kept for backward compatibility). If the user pastes a phrase anyway: tell them it is compromised and call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

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

# If you have `gateway.reload.mode = off`, restart manually:
# openclaw gateway restart                                   # native install
# docker restart <your-openclaw-container>                   # Docker self-host
```

Then in chat: *"Set up TotalReclaw"* — the agent will call `totalreclaw_pair` and hand you the URL + PIN. Open the URL in your browser to enter or generate your phrase.

> Pin a specific RC with `openclaw plugins install @totalreclaw/totalreclaw@3.3.2-rc.2`. Check what each tag resolves to: `npm view @totalreclaw/totalreclaw dist-tags`. Keep skill and plugin on the same version family (both stable or both RC).

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
- **Agent can't see TotalReclaw tools after install**: under the default config OpenClaw auto-restarts the gateway within 1-3s of `openclaw plugins install` — wait 5-10s and check `openclaw plugins list`. If the plugin is listed but the agent still can't see the tools, your config likely has `gateway.reload.mode = off`. The agent should issue OpenClaw's built-in `/restart` slash command (in-process, drains active runs, works from inside the gateway). Manual fallback only if `/restart` is unavailable — native: `openclaw gateway restart`; Docker self-host: `docker restart <your-openclaw-container>`; managed service: use the service's restart control.
- **Account-setup URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the account-setup page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: paste the canonical message again so the agent re-runs `totalreclaw_pair`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.
- **Agent narrating internals during install** ("let me find…", "in parallel…", "manifest detected…"): the agent missed the silence rule in §"Agent instructions". Reply *"Don't narrate the install internals — just tell me when it's installed and when to set up my account."* and the next session should silence.
- **`openclaw` CLI hangs / exits 124 inside the gateway agent shell** (issue [#184](https://github.com/p-diogo/totalreclaw-internal/issues/184)): on some Docker setups the agent's shell-execution of `openclaw plugins list` / `openclaw plugins install` etc. deadlocks (every subcommand exits 124). When this happens the agent can bypass the CLI wrapper by invoking the plugin's own JS entries directly via `node`. The plugin ships pair-flow + version-probe entries that work without any CLI:
  - **Plugin install probe** — read the plugin's filesystem manifests to confirm the install completed: `cat ~/.openclaw/extensions/totalreclaw/.loaded.json` (issue [#186](https://github.com/p-diogo/totalreclaw-internal/issues/186)) reports `{ loadedAt, tools, version }` if `register()` ran cleanly. `.error.json` reports a structured failure if it didn't. These files are the canonical filesystem signal — they don't depend on the deadlocking CLI.
  - **Pair-flow direct-node fallback** — when `openclaw` CLI hangs during `totalreclaw_pair`, the agent (or a shell helper) can run `node ~/.openclaw/extensions/totalreclaw/dist/pair-cli.js --url-pin-only` directly to obtain the universal-relay pair URL. This bypasses the gateway tool-router entirely and writes the resulting URL to stdout (one JSON line, no prompts). Surface that URL to the user the same way `totalreclaw_pair` would.
  - **Restart fallback** — `/restart` slash command is the autonomous path; manual fallbacks (`openclaw gateway restart`, `docker restart`) require user-side terminal access and are documented above.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

---

## Further reading

- [Feature Comparison](feature-comparison.md)
- [Importing Memories](importing-memories.md)
- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Detailed reference](beta-tester-guide-detailed.md) — env vars, extraction tuning, architecture
- [totalreclaw.xyz](https://totalreclaw.xyz)
