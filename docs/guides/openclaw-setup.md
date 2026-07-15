# TotalReclaw for OpenClaw

> **Audience map.** This guide serves three readers — **(a) humans** wanting to understand what their agent is about to do, **(b) the agent itself** that just fetched this URL, and **(c) Pedro / future-Pedro doing QA on a fresh VM**. The lean agent-executable version (~5 KB, no rationale prose) lives at [`openclaw-setup-quickstart.md`](./openclaw-setup-quickstart.md). If you are an agent and the user only pasted this guide URL, prefer the quickstart for execution and use this file for context.

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. The fastest path is a single chat message — your agent fetches this guide, you and your agent install the plugin together, and the agent walks you through setting up your TotalReclaw account.

## Install prompts

The user pastes one of these three prompts. The shape is fixed; the agent extracts intent from the shape, not from arbitrary phrasing. (Mirrors `hermes-setup.md`'s prompt section.)

### Stable (default)

```
Install TotalReclaw. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md
```

The agent installs the stable channel (`@totalreclaw/totalreclaw` from npm `latest`) and proceeds to setup.

To see what the channels currently resolve to: `npm view @totalreclaw/totalreclaw dist-tags` (e.g. `latest: 3.3.13`, `rc: 3.3.13-rc.1` as of 2026-07-15).

### RC (user specifies a version)

```
Install TotalReclaw RC <version>. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md
```

`<version>` is a semver RC string the user picks (e.g. `3.3.13-rc.1`). When the user omits `<version>` and writes only `Install TotalReclaw RC`, the agent installs the **newest release candidate** via `openclaw plugins install @totalreclaw/totalreclaw@rc` (no pin — always the latest cut).

### RC against the staging relay (QA only)

```
Install TotalReclaw RC <version> against the staging relay (TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz). See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md
```

The agent exports `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` in the install / setup shell before any TotalReclaw command. Without this env var, both stable AND RC installs default to the production relay `api.totalreclaw.xyz`.

## What this is and where it comes from

TotalReclaw is open-source memory tooling for OpenClaw. The plugin is published by `p-diogo` (GitHub-verified) at:

- npm: [`@totalreclaw/totalreclaw`](https://www.npmjs.com/package/@totalreclaw/totalreclaw)
- ClawHub skill: `totalreclaw`
- Source code: <https://github.com/p-diogo/totalreclaw>

All cryptographic operations run on your device. Encrypted bundles are forwarded through the TotalReclaw relay (`api.totalreclaw.xyz` by default for both stable and RC builds; set `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` to opt into the staging relay). The recovery phrase is generated and stays inside your browser — neither the agent, the relay, nor anyone else ever sees it.

This guide is the playbook your agent follows; you can read along to see what each step does.

---

## Agent instructions (read this if you are an OpenClaw agent that just fetched this URL)

You have been asked to install TotalReclaw. TotalReclaw is OpenClaw's **native `kind:"memory"` provider**: recall runs through the conventional `memory_search` / `memory_get` tools, and facts are captured automatically in the background — you do **not** call a remember tool per fact. The only tools you drive during setup are the `tr` CLI (for pair / status) and `/totalreclaw-restart` (autonomous reload if memory tools don't bind).

**Run all install / verify steps silently** except for the user-visible lines explicitly listed below — those are the ONLY things you should write in chat. Tool calls happen; narration of those tool calls does not.

The user pasted this URL deliberately, but treat each command as a step you and the user complete together. Confirm what you see at each phase. If anything looks wrong, surface it and stop — don't push through.

### Top-level rule (read this before anything else)

**Emit ONLY the user-visible lines listed below. Do not narrate tool calls. Do not describe what you're doing or about to do. Errors are the only allowed deviation, and only as a single tight line.** Telegram and similar transports edit the bot's messages live as your tool calls progress — every interim "Let me check…" / "Plugin loaded. Let me verify…" line shows up to the user as awkward edit churn, even if your transport later "deletes" or rewrites it. The fix is to never emit it in the first place. The expected user-facing transcript for a healthy install is **5–6 short lines TOTAL**. Anything more is a SKILL violation.

### What you will run, and why each step is safe

1. `openclaw plugins install @totalreclaw/totalreclaw` — installs the published plugin from npm under the `@totalreclaw` scope owned by `p-diogo`. The plugin runs inside OpenClaw's plugin sandbox. Upstream docs: <https://docs.openclaw.com/cli/plugins>.
2. `openclaw skills install totalreclaw` — fetches the SKILL.md descriptor from ClawHub. Read-only metadata; no code executes from this step. Upstream docs: <https://docs.openclaw.com/cli/skills>.
3. When no `~/.totalreclaw/credentials.json` exists, you trigger pairing via the plugin's in-process HTTP route (`curl http://localhost:18789/plugin/totalreclaw/pair/init`), which mints a one-time URL + PIN. The user opens the URL in their own browser to enter or generate a recovery phrase. The phrase never enters chat or your shell.

You are not asked to handle the recovery phrase. You are not asked to bypass auth. Pasting the install prompt IS the consent moment for installing the plugin; the URL + PIN you hand back from `tr pair --json` is the real consent moment for account creation, since the user has to open the page in their browser to continue. There is no separate "yes/no" gate before account setup. If the user pastes a phrase in chat anyway, refuse to proceed — see "Phrase safety" below.

### User-visible prose (THIS IS ALL YOU SAY)

Speak only these lines, in order. Substitute the bracketed placeholders with the real values. No prefaces. No suffixes. No narration around them.

1. (Step 1 — start; emit BEFORE any shell tool call so the user knows work is happening)
   > Setting up TotalReclaw — this takes about a minute…
2. (Step 2 — after install + `tr status --json` confirms CLI works)
   > ✓ Installed (totalreclaw &lt;version&gt;)
3. (Step 3 — only if existing credentials were detected, then stop)
   > ✓ TotalReclaw is already set up.
4. (Step 4 — account-setup URL + PIN, immediately after `tr pair --json` returns — no consent gate)
   > Open &lt;url&gt; in your browser. Enter PIN &lt;pin&gt;. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
5. (Step 5 — done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw free tier — 250 memories/month on Gnosis mainnet, E2E encrypted. https://totalreclaw.xyz/pricing. Ask me to remember anything.

That is the complete user-facing transcript for a healthy install. Expected line count for a fresh install + setup is **4–5 lines** (line 3 is mutually exclusive with lines 4-5).

### When to break silence (errors only)

If a step **actually fails**, surface the failure in one tight line — the error text plus what you're doing about it. Examples:

- `Plugin install returned ENOENT — your shell doesn't have access to the OpenClaw CLI here. I'll stop and explain.`
- `Memory tools didn't bind after install. Issuing /totalreclaw-restart now.`
- `Account-setup URL returned 404 — the PIN may have expired. Generating a fresh URL + PIN.`

Do NOT pre-narrate diagnostics that succeed. Do NOT narrate retries that succeed silently. Do NOT echo command output unless the user asks for it.

### Restart is agent-driven, never user-manual (read before Step 4)

The plugin self-reloads: after install it writes the gateway config it needs and signals `SIGUSR1` to its own PID so the gateway picks it up without a manual restart. **If `memory_search` / `memory_get` are not bound** (tools didn't take effect on first load), issue the literal slash command `/totalreclaw-restart` **autonomously** — announce it in one line, never ask the user to authorize it, never stall, never `kill` / `docker restart` / prompt the user to restart. The user should never perform a manual restart. (The legacy `totalreclaw_pair` tool, `hybrid-primary` framing, `.pair-pending.json`, `before_agent_start` injection, and `tr recall` CLI are all retired — do not invoke them. `tr pair --json` is the pair path; `memory_search` is recall.)

### ⛔ FORBIDDEN actions during install + pair (hard rules from Pedro's QA history)

These freelancing patterns have broken pair flows. They are FORBIDDEN regardless of how clever the workaround feels:

1. **Do NOT prompt the user to restart, and do NOT ask permission to restart.** If `memory_search` / `memory_get` aren't bound, issue `/totalreclaw-restart` autonomously (one user-visible line) and continue. Never `kill`, `docker restart`, or ask the user to restart.

2. **Do NOT hand-write `~/.openclaw/openclaw.json`** after the install. The plugin's `register()` already wrote the two host keys it owns (`plugins.entries.totalreclaw.hooks.allowConversationAccess`, `channels.telegram.streaming.mode`) and auto-fired SIGUSR1 to its own PID; OpenClaw's native install owns the slot/allowlist/discovery keys. Any manual config write (redirect / sed / jq into `openclaw.json`, `openclaw config set …`) triggers a fresh deferred reload that can kill an in-flight pair subprocess and is FORBIDDEN. The one sanctioned config action is the `openclaw plugins enable totalreclaw` command (Step 1) — that is OpenClaw's own slot-selection path, not a hand-write, and it is safe.

3. **Do NOT re-render the QR.** `tr pair --json` returns `qr_ascii` (Unicode) AND the pair page renders a real PNG. Saving the QR to `/tmp/totalreclaw-pair-qr.png` triggers OpenClaw's media-access policy block and Telegram delivery fails with `LocalMediaAccessError`. Just emit the URL + PIN as user-visible line 4 as **plain text on its own line** (the `#pk=` URL fragment breaks if wrapped in markdown/backticks) — that is your only QR responsibility.

4. **Do NOT invoke `tr pair --json` in the foreground.** A foreground pair subprocess is killed when your shell-tool exec returns. The pair WS dies → relay returns 502. **Prefer the in-process HTTP route `curl -s http://localhost:18789/plugin/totalreclaw/pair/init` (3.3.12+) instead — it opens the relay WS in the gateway process and needs no detachment.** If you must fall back to the CLI path (older plugin, or HTTP route unreachable), Step 4's `setsid -f` block is mandatory — copy it VERBATIM. Do not "simplify". Do not drop the `setsid -f`. Do not omit `< /dev/null`.

5. **Do NOT narrate.** Examples that have appeared in past QAs and MUST NEVER appear again: "Now let me verify the CLI…" / "Gateway needs a restart, let me restart it" / "Let me find and signal the gateway process" / "The CLI is at the npm path, not the extensions path" / "CLI reports itself as version X (lag)" / "Now let me also add the required post-install config" / "Let me save the QR as a PNG". All silent — these are internal details. Emit only the 4-5 user-visible lines.

### Forbidden vocabulary in agent prose

Unless the user explicitly asks how the install works, the following words / patterns must not appear in your chat output during install + setup. These are the patterns that surface as edit churn in transports like Telegram:

- "Let me check that…"
- "Plugin loaded. Let me verify…"
- "I'll now…"
- "Quick fix…"
- "However, I don't see…"
- "I need to use…"
- "Should I /totalreclaw-restart" / "Want me to restart" / "Let me check if the tool is bound" / "I need permission to restart" / "Do you have a public URL"
- "Now installing…" / "Got it" / "In parallel…" / "Step 1a/1b/1c"
- `manifest`, `descriptor`, `file-watcher`, `polling`, `auto-reload-detect`, `find`, `lookup`, `detect`, `probing`, `let me try`
- retired flow names: `hybrid mode`, `hybrid-primary`, `hybrid_mode`, `auto-pair`, `totalreclaw_pair`, `tr recall`, `.pair-pending`

Substitute every one of those with silence — the next user-visible line in the set, or nothing.

If the user explicitly asks ("what's it doing?", "why so quiet?", "show me the commands"), then narrate freely — the silence rule only governs the default install path.

### Step 1 — Install the skill + plugin (silent — emit user-visible line 1, then no prose until line 2)

Run via your shell tool, in this exact order, **without writing prose about them**. Emit user-visible line 1 (`Setting up TotalReclaw — this takes about a minute…`) once at the start, then stay silent until both commands succeed.

```bash
openclaw plugins install @totalreclaw/totalreclaw
openclaw skills install totalreclaw
```

**Plugin BEFORE skill is deliberate** (post-Pedro QA): the skill install triggers a config-driven SIGUSR1 gateway reload that can race a concurrent plugin install. Plugin-first lets the plugin land cleanly, then the skill's reload picks it up on the next gateway loop. If you accidentally do skill-first and the plugin install dies mid-flight, just retry `openclaw plugins install @totalreclaw/totalreclaw` once — it's idempotent.

**Bind the memory slot (idempotent):**

```bash
openclaw plugins enable totalreclaw
```

Run this right after the install commands, always. On a **clean** install OpenClaw's native slot-selection already set `plugins.slots.memory = "totalreclaw"`, so `enable` is a harmless no-op. On a **reinstall over leftover config** from a prior version (stale `plugins.allow` / `plugins.bundledDiscovery` / `plugins.entries` keys), that native slot-selection can silently skip — leaving the plugin installed but `Status: disabled` with the slot stuck at `memory-core`. A disabled plugin's `register()` never runs, so the memory tools and the pair HTTP routes are never served and pairing later fails with a 502. `openclaw plugins enable totalreclaw` re-runs the exclusive slot-selection and flips the slot (`Exclusive slot "memory" switched from "memory-core" to "totalreclaw"`), so it makes both cases correct with one idempotent command. See the reinstall note below if `enable` itself reports the plugin isn't found.

**What the plugin patches for you (and what it does NOT).** On load the plugin's `register()` writes only the two host keys it owns — `plugins.entries.totalreclaw.hooks.allowConversationAccess = true` (Fix #2) and `channels.telegram.streaming.mode = "off"` if unset (Fix #3) — then fires `SIGUSR1` to its own PID so the gateway in-process restart picks them up without any manual restart. As of the native-memory line (rc.20, #402) it no longer writes `plugins.slots.memory`, `plugins.allow`, `plugins.bundledDiscovery`, or `plugins.installs` — OpenClaw's native install owns the slot + allowlist + discovery. That is why the `openclaw plugins enable totalreclaw` step above exists: the slot is the host's job now, and the one time it doesn't bind on its own is a reinstall over stale config.

> **Setup is a user-initiated pair (native memory integration).** The plugin does **not** auto-pair on load. When no `~/.totalreclaw/credentials.json` exists, the agent triggers pairing via the plugin's in-process HTTP route (`GET http://localhost:18789/plugin/totalreclaw/pair/init`, 3.3.12+) — which opens the relay pair WebSocket directly in the gateway process (immune to the 30s shell-tool subprocess timeout that killed the CLI path's WS → relay 502) — or falls back to `tr pair --json` via `setsid -f`. Either path surfaces the returned `url` + `pin` verbatim, and you complete the flow in your browser (generate/import your 12-word recovery phrase browser-side — it is encrypted client-side and never enters the chat or the agent's context). On completion `credentials.json` is written and `memory_search`/`memory_get` work immediately — no manual gateway restart (the plugin self-applies its config + signals `SIGUSR1` to reload; if the memory tools don't bind, the agent issues `/totalreclaw-restart` autonomously). Recall is native (`memory_search`/`memory_get`) and facts are captured automatically in the background — you don't call a remember tool per fact.

> **Auto-extraction safety net.** Even when the `agent_end` hook is gated by an upstream policy, the plugin runs a filesystem-polling backup that watches `~/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl` every 60 s and runs the same extraction pipeline (NOT a hook event, so it's never gated). The hook fires alongside the poller; offset-based dedup prevents double-extraction.

> **If the plugin lands `Status: disabled` (memory slot stuck at `memory-core`).** This is the reinstall failure mode the Step 1 `openclaw plugins enable totalreclaw` step prevents. If you skipped it, or `openclaw plugins inspect totalreclaw` shows `Status: disabled` / `Error: memory slot set to "memory-core"`, run `openclaw plugins enable totalreclaw` once — it re-runs OpenClaw's exclusive slot-selection and flips the slot to `totalreclaw`. The plugin is already on disk; only the slot binding needs fixing. Symptom if left unfixed: `openclaw totalreclaw status` reports `unknown command`, the boot log shows only 2 plugins (no totalreclaw), and pairing fails with a 502 because the plugin's pair HTTP routes never registered.

Notes:
- For an RC build, replace the first command with `openclaw plugins install @totalreclaw/totalreclaw@rc` (latest release candidate) or pin a version like `openclaw plugins install @totalreclaw/totalreclaw@3.3.13-rc.1`.
- **OpenClaw exec-surface compatibility (2026.6.11+).** OpenClaw **2026.6.11 and later** moved shell exec off the gateway and onto **paired node hosts**, so the agent can only run `openclaw plugins install` (and the other setup commands) when it has an **exec-capable surface** (a paired node host). Agent-driven install over chat works out of the box on **≤ 2026.6.8** gateway-exec setups; on 2026.6.11+ without a paired exec host, the agent cannot shell out — install through the service's **Plugins** UI instead (see "Managed OpenClaw service" below) and then paste an **Install prompt** so the agent picks up the already-loaded plugin.
- **Always run the install — never skip it because a totalreclaw "appears present".** A leftover install from a prior version (e.g. a stale `@totalreclaw/mcp-server`) is NOT the `@totalreclaw/totalreclaw` plugin and must be replaced.
- **If install reports `already exists` / `delete it first` (reinstall over a prior install), do NOT use `--force`.** `--force` writes an install record with `version=None`, which OpenClaw's installs-map schema rejects — the plugin lands broken and disabled. The reason for the block is that `openclaw plugins uninstall` removes the config/slot/record but leaves the package files behind in `~/.openclaw/npm/projects/<hash>/`, and the new install refuses to overwrite them. Clean it and reinstall:

  ```bash
  openclaw plugins uninstall totalreclaw
  rm -rf ~/.openclaw/npm/projects/*totalreclaw-totalreclaw*   # orphan package dir left by uninstall
  openclaw plugins install @totalreclaw/totalreclaw           # (append @rc for a release candidate)
  openclaw plugins enable totalreclaw                          # bind the memory slot (see Step 1)
  ```

  The `rm -rf` targets only the plugin's npm package project dir. It does **not** touch `~/.totalreclaw/credentials.json` (your keys live there, in a different tree) — never read, list, or delete anything under `~/.totalreclaw*`. Then verify the version in Step 2 — it must match the requested build.
- If either command fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), STOP and tell the user verbatim: *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` before continuing to Step 2.

### Step 2 — Verify the plugin loaded — emit user-visible line 2 on success

Run the **registered** `openclaw totalreclaw` subcommand (the plugin registers it via `api.registerCli` during `register()`). This is the canonical verification path — it both confirms the plugin loaded AND that its CLI surface is bound:

```bash
openclaw totalreclaw status
```

A successful run (exit `0`, human-readable status printed) is your verification. Emit user-visible line 2 (`✓ Installed (totalreclaw <version>)`) with the version the `openclaw plugins install` step reported (or read it from `openclaw plugins list | grep totalreclaw`). Then proceed silently to Step 3.

If `openclaw totalreclaw status` exits non-zero or reports `unknown command`, wait 5s and retry once (the install / SIGUSR1 reload may still be completing). If it still fails, tell the user the install didn't land and retry the install command you originally ran (stable: `openclaw plugins install @totalreclaw/totalreclaw`; RC: append `@rc`).

> ⛔ **DO NOT use `npx` to run TotalReclaw commands.** `npx` fails inside OpenClaw's sandbox (no network egress for the package fetch, and the resolved binary path is wrong for a plugin that's already installed). Always use the **registered subcommand** `openclaw totalreclaw <command>` (e.g. `openclaw totalreclaw status`, `openclaw totalreclaw onboard --pair-only`) or the **native tool** `memory_search`. The only place a raw `node .../dist/cli/tr-cli.js` invocation is acceptable is the explicit fallback blocks below (the pair CLI fallback and the issue-#184 deadlock workaround), which define the path inline — never invent a resolver of your own and never reach for `npx`.

### Step 3 — Check for existing credentials (silent unless found)

Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit user-visible line 3 (`✓ TotalReclaw is already set up.`) and stop — the user is already set up. Otherwise continue silently.

### Step 4 — Set up the user's TotalReclaw account IMMEDIATELY — no consent gate (emit user-visible line 4)

Pasting the install prompt was the consent moment for installing the plugin. The URL + PIN you're about to hand the user is the real consent moment for account creation — they have to open the page in their browser to proceed. There is NO separate yes/no gate. Do NOT ask "Would you like to set up an account?" — run pair immediately. Pair is UNCONDITIONAL when no credentials exist; the user installing/requesting setup IS the consent.

**PRIMARY pair path (3.3.12+) — the in-process HTTP route.** This is the resilient path: the plugin opens the relay pair WebSocket directly in the gateway process via `GET /plugin/totalreclaw/pair/init`, so the WS is immune to the 30s shell-tool subprocess timeout (which killed the CLI's pair WS mid-pair → relay 502 on `/pair/respond`). No `setsid -f`, no subprocess detachment, no foreground-kill risk — the WS lives in the gateway process itself and survives shell-tool timeouts, retries, and SIGUSR1 reloads.

```bash
curl -s http://localhost:18789/plugin/totalreclaw/pair/init
```

Parse the JSON `{"v":1,"sid":"...","url":"...","pin":"...","mode":"...","expires_at_ms":...}`. The route returns immediately with the user-facing URL + PIN; the gateway holds the WebSocket open in-process and completes pairing in the background once the browser uploads the encrypted phrase. Emit user-visible line 4 (`Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply done once it's sealed.`) with `<url>` and `<pin>` substituted VERBATIM from the JSON — never invent or modify values. Do not pre-narrate — line 4 itself is the only thing the user needs to see.

**FALLBACK pair path — `tr pair --json` via `setsid -f`.** Use ONLY if the HTTP route is unreachable (older plugin without `/pair/init`, or the gateway HTTP server isn't bound on `localhost:18789`). The CLI path opens the same relay WS but from a subprocess, so it MUST be detached with `setsid -f` to survive the post-install SIGUSR1 reload. A foreground `node tr-cli pair` is killed mid-flight when the gateway fires its deferred restart, surfacing as `Gateway could not finish pairing (502). The agent timed out or the ciphertext failed to decrypt — ask the agent to retry pairing.` on the user's browser.

```bash
PAIR_OUT="/tmp/tr-pair-out-$(date +%s).json"
# Resolve the installed plugin's standalone CLI directly (this fallback only).
TR_CLI="$(ls -d ~/.openclaw/npm/node_modules/@totalreclaw/totalreclaw/dist/cli/tr-cli.js ~/.openclaw/extensions/totalreclaw/dist/cli/tr-cli.js 2>/dev/null | head -1)"
setsid -f node "$TR_CLI" pair --json > "$PAIR_OUT" 2>&1 < /dev/null
sleep 2
cat "$PAIR_OUT"
```

If the HTTP route returns connection-refused AND the CLI fallback also fails, retry once after 5s (the install may still be completing). There is no other agent-facilitated pair path under the native flow (the legacy `totalreclaw_pair` plugin tool is retired).

**Emit the URL as PLAIN TEXT on its OWN LINE — no markdown, no backticks, no `[text](url)` wrapping, no line-splitting.** The URL contains a `#pk=` fragment that breaks (unclickable link / unscannable QR) if wrapped in markdown or split across lines. Plain text only. (The HTTP `/pair/init` payload does not include `qr_ascii`; only the CLI payload does. The pair page served at `/pair/finish` renders a real PNG QR in the browser regardless of which path opened the session, so users on a desktop browser see the QR. Do NOT save QR PNGs locally — `LocalMediaAccessError`.)

If the cat output is empty after 2s, sleep 2 more seconds and try again (relay handshake can take up to 4s on a cold network — applies to the CLI fallback; the HTTP route blocks until the relay `opened` frame lands, so its response is never empty).

### Step 5 — Verify and confirm (silent until line 5)

After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN likely expired — re-run `tr pair --json` and resend line 4 once. If present, emit user-visible line 5 (with the free-tier confirmation) and stop.

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. NEVER `cat ~/.totalreclaw/credentials.json` or read the `mnemonic` / `recovery_phrase` field from any file. The ONLY agent-facilitated account-setup path is `tr pair --json`, which returns a URL + PIN — the phrase itself is generated/imported in the browser, never in chat. If the user pastes a phrase anyway: tell them it is compromised and re-run `tr pair --json` for a fresh wallet.

---

## What's happening (for the human reader)

1. `openclaw skills install totalreclaw` — fetches TotalReclaw's SKILL.md from ClawHub and registers it under `~/.openclaw/workspace/skills/totalreclaw/`. The agent picks it up on next config reload.
2. `openclaw plugins install @totalreclaw/totalreclaw` — installs the plugin from npm (the `latest` dist-tag is always current stable; use `@rc` for the latest release candidate).
3. The plugin's `register()` patches `~/.openclaw/openclaw.json` with the memory slot + hook keys OpenClaw looks for, then signals `SIGUSR1` to its own PID so the gateway picks up the new keys in-process — no manual restart.
4. When no credentials exist, the plugin does **not** auto-open a pair session — pairing is user-triggered. On load with no `~/.totalreclaw/credentials.json`, the plugin logs a single hint and waits:
   ```
   TotalReclaw: no credentials found. Run `tr pair` (or ask the agent to) to complete setup.
   ```
   The agent then completes setup via one of the registered `openclaw totalreclaw` subcommands (bound during `register()` via `api.registerCli`) or the standalone `tr` binary — all equivalent:
   - `openclaw totalreclaw pair` (remote/browser QR + PIN flow — the agent-facing path) or the in-process HTTP route `GET /plugin/totalreclaw/pair/init` above.
   - `openclaw totalreclaw onboard` (local, at your own terminal — never driven by the agent; see "Phrase safety").
   - `openclaw totalreclaw status` to verify.

   Whichever pair path runs generates an ephemeral x25519 keypair on the gateway and a 6-digit PIN. You get a URL + PIN.
5. You open the URL. The account-setup page has two tabs: **Generate new** (the browser creates a fresh BIP-39 12-word phrase locally using `crypto.getRandomValues`) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, click seal.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives an AES-256-GCM key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey back. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. From the next session onward, recall runs natively through `memory_search` / `memory_get` (the conventional memory contract), and facts are captured automatically in the background — you don't call a remember tool per fact.
8. The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

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
3. Return to chat and paste one of the **Install prompts** at the top of this guide (e.g. the Stable prompt). The agent will detect that the plugin is already loaded, skip Steps 1-2, and jump straight to account setup.

The browser-side crypto and account-setup flow are identical to self-hosted setups; only the install step differs.

---

## Fully manual (CLI only — last resort)

If you can't or won't use the chat flow (self-hosted only — managed services don't expose the host shell):

```bash
openclaw plugins install @totalreclaw/totalreclaw            # stable
# Or for an RC: @totalreclaw/totalreclaw@rc
# The plugin self-restarts the gateway via SIGUSR1 when needed. You should not need
# to run `openclaw gateway restart` or `docker restart` yourself; if a restart appears
# stuck, file an issue rather than hand-restarting.
# Verify with: `openclaw plugins list | grep totalreclaw`.
```

Then in chat: *"Set up TotalReclaw"* — the agent will run `tr pair --json` and hand you the URL + PIN. Open the URL in your browser to enter or generate your phrase.

> Pin a specific RC with `openclaw plugins install @totalreclaw/totalreclaw@3.3.13-rc.1`. Check what each tag resolves to: `npm view @totalreclaw/totalreclaw dist-tags`. Keep skill and plugin on the same version family (both stable or both RC).

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
| **Quota warning at ≥80%** | When monthly free-tier memories cross 80%, a one-line warning is injected at conversation start so you know before you hit the limit. |

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
| **Status** | "What's my TotalReclaw status?" — surfaces tier, monthly memories used / limit, reset date, upgrade URL |
| **Import from** | "Import my Gemini history from ~/Downloads/..." |
| **Account setup** | "Set up TotalReclaw for me" — returns URL + PIN |

Your recovery phrase is the only key to your memories. Store it somewhere durable (paper, password manager, or hardware key). On a new machine, paste one of the **Install prompts** at the top of this guide and switch to the **Import existing** tab in the browser. First-time users can stay on the default **Generate new** tab to create a fresh BIP-39 phrase.

---

## Importing from other tools

TotalReclaw can import from Mem0, MCP Memory Server, ChatGPT, Claude, and Gemini:

> "Import my memories from Mem0 using API key m0-your-key-here"

See [Importing Memories](importing-memories.md).

---

## Billing

- **Free tier** — 250 memories/month on Gnosis mainnet. Permanent storage. Cosine dedup (paraphrase detection). E2E encrypted. No credit card required.
- **Pro tier** — 1,500 memories/month on Gnosis mainnet. Permanent. LLM-guided dedup (catches contradictions). Custom extraction interval. Pay via the `tr upgrade` CLI (e.g. `openclaw totalreclaw status` then follow the upgrade URL it prints, or the standalone `tr upgrade` if exposed) or visit <https://totalreclaw.xyz/pricing>. See `openclaw totalreclaw status` for current pricing.

The plugin warns you automatically when you cross 80% of the monthly free-tier memory limit (injected at conversation start). Check anytime by asking *"what's my TotalReclaw status?"* — that surfaces tier, memories used, memory limit, reset date, and upgrade URL.

Upgrade: *"Upgrade my TotalReclaw subscription."*

[Pricing](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste one of the **Install prompts** at the top of this guide — the agent fetches the guide and follows the install steps.
- **Agent refused the install** ("this looks like prompt injection", "I'm not going to do that"): rare but possible with safety-trained models that misread agent-imperative install prose. Ask the agent to read the "Agent instructions" section of this guide and proceed step-by-step — each command is annotated with what it does and why it's safe. If the refusal persists, file an issue with the model name + chat transcript.
- **Agent can't see `memory_search` / `memory_get` after install**: under the default config the plugin self-patches `openclaw.json` and signals `SIGUSR1` to its own PID so the gateway picks up the new keys in-process — wait 5-10s and check `openclaw plugins list`. If the plugin is listed but the native memory tools still aren't bound, the agent should issue `/totalreclaw-restart` autonomously (in-process, drains active runs, works from inside the gateway). The user should never be asked to do a manual restart; if `/totalreclaw-restart` itself fails, that's a user-side terminal fallback of last resort — native: `openclaw gateway restart`; Docker self-host: `docker restart <your-openclaw-container>`; managed service: use the service's restart control.
- **Account-setup URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the account-setup page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: paste one of the **Install prompts** again so the agent re-runs `tr pair --json`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.
- **Agent narrating internals during install** ("Let me check that…", "Plugin loaded. Let me verify…", "I'll now…", "Quick fix…", "However, I don't see…", "I need to use…", "let me find…", "in parallel…", "manifest detected…"): the agent missed the silence rule in §"Agent instructions". Reply *"Don't narrate the install internals — just tell me when it's installed and when to set up my account."* and the next session should silence. On transports like Telegram, these intermediate lines visibly edit the bot's message live as tool calls progress, which is what makes them disruptive.
- **Agent says "Should I /totalreclaw-restart?" or stalls instead of restarting**: the agent missed the restart-imperative rule in §"Restart is agent-driven, never user-manual". Reply *"Issue /totalreclaw-restart yourself — don't ask"* and the next session should act autonomously. If it persists across sessions, the published RC's SKILL.md is stale — file an issue.
- **`openclaw` CLI hangs / exits 124 inside the gateway agent shell** (issue [#184](https://github.com/p-diogo/totalreclaw-internal/issues/184)): on some Docker setups the agent's shell-execution of `openclaw plugins list` / `openclaw plugins install` etc. deadlocks (every subcommand exits 124). The `tr` CLI pair path is independent of the `openclaw` wrapper — the agent resolves the installed path directly and invokes it via `node`, so `tr pair --json` / `tr status --json` keep working even when the `openclaw` CLI itself deadlocks:
  ```bash
  TR_CLI="$(ls -d ~/.openclaw/npm/node_modules/@totalreclaw/totalreclaw/dist/cli/tr-cli.js ~/.openclaw/extensions/totalreclaw/dist/cli/tr-cli.js 2>/dev/null | head -1)"
  node "$TR_CLI" pair --json
  node "$TR_CLI" status --json
  ```
  - **Restart fallback** — `/totalreclaw-restart` slash command is the autonomous path; manual fallbacks (`openclaw gateway restart`, `docker restart`) require user-side terminal access and are documented above.

---

## Canonical prompt (matches the QA harness scenario contracts)

The canonical install prompts live in the **Install prompts** section at the top of this guide (Stable / RC / RC-staging). The default stable prompt is:

> **Install TotalReclaw. See <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

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
