# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. The fastest setup is a single chat message — the agent fetches this guide, runs the install for you, and walks you through pairing. **You do nothing in the terminal.**

If you already have OpenClaw running, paste this into your OpenClaw chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

Your agent will fetch this page, run the two install commands, and hand you a pair URL + 6-digit PIN. You open the URL in your browser, generate or restore a 12-word recovery phrase there, confirm the PIN, and you're done.

---

## Agent instructions (read this if you are an OpenClaw agent that just fetched this URL)

You have been asked to install TotalReclaw. Run the steps below in order. Do not deviate, do not ask the user to run terminal commands, do not echo or generate a recovery phrase.

### Step 1 — Install the skill + plugin

Run via your shell tool, in this order:

```bash
openclaw skills install totalreclaw
openclaw plugins install @totalreclaw/totalreclaw
```

Notes:
- For an RC build, replace the second command with `openclaw plugins install @totalreclaw/totalreclaw@rc` (or pin a version like `@totalreclaw/totalreclaw@3.3.1-rc.19`).
- If either command fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), STOP and tell the user verbatim: *"Your environment doesn't expose the OpenClaw CLI to me. Please install `totalreclaw` from your service's plugins / skills UI, then reply `done`."* Wait for `done` before continuing to Step 2.

### Step 2 — Wait for auto-reload (do NOT restart the gateway yourself)

OpenClaw's default config (`gateway.reload.mode = "hybrid"`) auto-restarts the gateway via a file-watcher within 1-3 seconds of the plugin install. Wait ~5 seconds, then verify with `openclaw plugins list | grep totalreclaw` and confirm the `totalreclaw_pair` tool is in your toolset.

If `totalreclaw_pair` is not bound within 10 seconds, the user has `gateway.reload.mode = off`. Fall back: tell the user verbatim *"Auto-reload didn't fire. Please restart your OpenClaw gateway: `openclaw gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — substitute the actual name; `docker ps` shows it), or your managed service's restart control. Reply `done` once it's back up."* Do NOT invoke the restart yourself — your shell runs inside the gateway you'd be restarting.

### Step 3 — Check for existing credentials

Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, confirm *"TotalReclaw is already set up."* and stop. Otherwise continue.

### Step 4 — Pair (browser flow, NEVER ask the user to type their phrase in chat)

Call the `totalreclaw_pair` tool. It returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}`.

Relay to the user verbatim:

> *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one — the phrase stays in your browser, the relay only sees ciphertext. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*

Render the QR if your transport supports it: image-capable transports (Telegram, Slack, web chat) decode `qr_png_b64` to bytes and attach as an image; terminal-only transports (OpenClaw native CLI) emit `qr_unicode` inline. The QR encodes only the URL — the PIN is a separate dual-channel confirmation, never bake it into the QR.

### Step 5 — Verify and confirm

After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN likely expired — call `totalreclaw_pair` again and resend. If present, confirm *"TotalReclaw is set up. Ask me to remember or recall anything."*

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `openclaw totalreclaw onboard` (bare interactive form), `totalreclaw setup`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. The ONLY agent-facilitated setup path is the `totalreclaw_pair` tool. If the user pastes a phrase anyway: tell them it is compromised and call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

---

## What's happening (for the human reader)

1. `openclaw skills install totalreclaw` — fetches TotalReclaw's SKILL.md from ClawHub and registers it under `~/.openclaw/workspace/skills/totalreclaw/`. The agent picks it up on next config reload.
2. `openclaw plugins install @totalreclaw/totalreclaw` — installs the plugin from npm (the `latest` dist-tag is always current stable; use `@rc` for the latest release candidate).
3. OpenClaw's config-watcher detects the `plugins.*` change in `~/.openclaw/config.yaml` and (under default `gateway.reload.mode = "hybrid"`) triggers a graceful SIGUSR1 restart within 1-3s. The new tools bind automatically.
4. The agent calls `totalreclaw_pair`, which generates an ephemeral x25519 keypair on the gateway and a 6-digit PIN. You get a URL + PIN.
5. You open the URL. The pair page has two tabs: **Generate new** (the browser creates a fresh BIP-39 12-word phrase locally using `crypto.getRandomValues`) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, click seal.
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

   The agent will detect that the plugin is already loaded, skip Steps 1-2, and jump straight to pairing.

The browser-side crypto and pairing flow are identical to self-hosted setups; only the install step differs.

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

> Pin a specific RC with `openclaw plugins install @totalreclaw/totalreclaw@3.3.1-rc.19`. Check what each tag resolves to: `npm view @totalreclaw/totalreclaw dist-tags`. Keep skill and plugin on the same version family (both stable or both RC).

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
| **Status** | "What's my TotalReclaw status?" |
| **Import from** | "Import my Gemini history from ~/Downloads/..." |
| **Pair** | "Set up TotalReclaw for me" — returns URL + PIN |

Your recovery phrase is the only key to your memories. Store it safely. Retrieve later with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. On a new machine, paste the canonical prompt above and switch to the **Import existing** tab in the browser. First-time users can stay on the default **Generate new** tab to create a fresh BIP-39 phrase.

---

## Importing from other tools

TotalReclaw can import from Mem0, MCP Memory Server, ChatGPT, Claude, and Gemini:

> "Import my memories from Mem0 using API key m0-your-key-here"

See [Importing Memories](importing-memories.md).

---

## Billing

| Tier | Storage | Price |
|------|---------|-------|
| **Free** | Unlimited on Base Sepolia testnet (may reset) | $0 |
| **Pro** | Permanent on Gnosis mainnet | $3.99/month |

Both tiers have unlimited memories and reads. Upgrade: *"Upgrade my TotalReclaw subscription."*

[Pricing](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent can't see TotalReclaw tools after install**: under the default config OpenClaw auto-restarts the gateway within 1-3s of `openclaw plugins install` — wait 5-10s and check `openclaw plugins list`. If the plugin is listed but the agent still can't see the tools, your config likely has `gateway.reload.mode = off` — fall back to a manual restart. Native: `openclaw gateway restart`. Docker self-host: `docker restart <your-openclaw-container>`. Managed service: use the service's restart control.
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: paste the canonical message again so the agent re-runs `totalreclaw_pair`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.

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
