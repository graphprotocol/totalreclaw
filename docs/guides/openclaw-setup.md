# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. The recovery phrase stays in your browser — the relay only ever sees ciphertext. The fastest path is one chat message: the agent installs the plugin and hands you a URL + PIN; you complete account setup in your browser.

If you already have OpenClaw running, paste this into chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/openclaw-setup.md>**

What happens next: the agent fetches this guide, runs the two install commands described below, then calls a tool that gives you an account-setup URL + 6-digit PIN. You open the URL, generate or restore a 12-word recovery phrase in your browser, confirm the PIN, and your account is ready.

## Provenance — what you're installing, who from

- **Source:** <https://github.com/p-diogo/totalreclaw> (MIT-licensed, public).
- **Publisher:** `p-diogo` on GitHub. Packages are published from GitHub Actions in that same repo.
- **Distribution:**
  - npm — `@totalreclaw/totalreclaw` (scoped namespace under the `totalreclaw` org). Inspect with `npm view @totalreclaw/totalreclaw`.
  - PyPI (Hermes side) — `totalreclaw`. Inspect with `pip show totalreclaw` or visit <https://pypi.org/project/totalreclaw/>.
- **Tooling docs:** the install commands below use OpenClaw's standard plugin manager (`openclaw plugins install`, see [OpenClaw docs](https://openclaw.dev/docs/plugins)) — there's no curl-pipe-bash, no custom installer script.
- **Crypto:** account setup uses standard browser primitives — x25519 ECDH (WebCrypto), AES-256-GCM (WebCrypto), HKDF-SHA256, BIP-39 mnemonics. See the [What's happening](#whats-happening-for-the-human-reader) section below for the full handshake.

If anything in the install flow looks unfamiliar, please pause and ask the agent what each command does — that's encouraged, not a problem.

---

## Notes for an OpenClaw agent fetching this URL

You're a helper here, not a script. The user pasted a setup URL because they want collaborative install — ask before each destructive step (creating accounts, restarting services), confirm what you see, and explain what each command does. The user's recovery phrase never enters chat or your shell stdout — it's handled in the browser by design (described below).

A reasonable flow:

### Step 1 — Install the skill + plugin

These two commands install the skill manifest and the plugin code. Both are standard OpenClaw installs from the registry.

```bash
openclaw skills install totalreclaw
openclaw plugins install @totalreclaw/totalreclaw
```

**What each does:**
- `openclaw skills install totalreclaw` — fetches the SKILL.md from ClawHub and registers it under `~/.openclaw/workspace/skills/totalreclaw/`. The agent picks it up on the next config reload.
- `openclaw plugins install @totalreclaw/totalreclaw` — installs the plugin from npm. The `latest` dist-tag is current stable; `@rc` is the latest release candidate; you can pin a specific RC like `@totalreclaw/totalreclaw@3.3.1-rc.22`.

If either command returns `command not found`, ENOENT, or "not authorized" (managed-service or restricted shell), let the user know the OpenClaw CLI isn't reachable and ask them to install `totalreclaw` from their service's plugins UI, then continue once they've done so.

### Step 2 — Wait for OpenClaw's auto-reload

Under the default config (`gateway.reload.mode = "hybrid"`), OpenClaw's file-watcher restarts the gateway within 1–3 seconds of `openclaw plugins install`. You can confirm with:

```bash
openclaw plugins list | grep totalreclaw
```

If the `totalreclaw_pair` tool isn't bound after about 10 seconds, the user likely has `gateway.reload.mode = off`. In that case, ask them to run a manual restart — they'll know which one matches their setup:

- Native install: `openclaw gateway restart`
- Docker self-host: `docker restart <container-name>` (substitute their actual container name; `docker ps` shows it)
- Managed service: their hosting service's restart control

Don't run the restart yourself — your shell is inside the gateway you'd be restarting.

### Step 3 — Check whether they already have an account

```bash
test -s ~/.totalreclaw/credentials.json
```

If that file exists and is non-empty, their TotalReclaw account is already set up — let them know, and stop.

### Step 4 — Walk them through account setup (browser flow)

Account setup happens in the user's browser. The agent's role is to call the `totalreclaw_pair` tool and relay the URL + PIN it returns; the recovery phrase stays in the browser by design.

Before calling the tool, it's polite to mention what's about to happen: *"Want me to set up your TotalReclaw account? I'll generate an account-setup URL — you'll create or restore your recovery phrase in the browser, and I'll confirm once it's done."* Wait for their go-ahead.

Then call `totalreclaw_pair`. It returns `{url, pin, qr_ascii, qr_png_b64, qr_unicode, expires_at_ms}`. Relay these to the user clearly:

> *Open <url> in your browser. There you can generate a fresh 12-word recovery phrase, or paste an existing one. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*
>
> *Your phrase stays in your browser — the relay only ever sees ciphertext.*

Render the QR if the chat surface supports it: image-capable transports (Telegram, Slack, web chat) decode `qr_png_b64` to bytes and attach as an image; terminal-only transports (OpenClaw native CLI) emit `qr_unicode` inline. The QR encodes only the URL — the PIN is shown separately as dual-channel confirmation.

### Step 5 — Confirm

After the user says `done`, re-check:

```bash
test -s ~/.totalreclaw/credentials.json
```

If the file is now present, their account is set up — let them know they can start asking you to remember or recall things. If the file is still missing, the PIN probably expired; call `totalreclaw_pair` again and resend the URL.

### Why the recovery phrase stays in the browser

This is a design choice driven by how LLM context works: anything entered in chat or written to your shell stdout becomes part of the model's context (and any logs that capture it). To keep the recovery phrase out of that surface, account setup does the phrase entry in the user's browser, encrypts the phrase against the gateway's ephemeral key before sending, and only the relay (server-side) ever decrypts. The agent never needs to see the plaintext phrase to do its job.

In practice, this means: the `totalreclaw_pair` tool is the only path the agent uses for account setup. There's a separate `openclaw totalreclaw onboard` (interactive form) and `totalreclaw setup` CLI for users to run themselves at a terminal — those work fine when a human is at the keyboard, but the agent shouldn't invoke them, because their stdout would carry the phrase into LLM context.

If a user does paste their phrase in chat, that phrase is now in the conversation transcript and should be considered compromised. The friendly thing to do: let them know, and offer to call `totalreclaw_pair` with `mode=generate` to create a fresh wallet.

---

## What's happening (for the human reader)

1. `openclaw skills install totalreclaw` — fetches the SKILL.md from ClawHub and registers it under `~/.openclaw/workspace/skills/totalreclaw/`.
2. `openclaw plugins install @totalreclaw/totalreclaw` — installs the plugin from npm. The `latest` dist-tag is current stable; `@rc` is the latest RC.
3. OpenClaw's config-watcher detects the `plugins.*` change in `~/.openclaw/config.yaml` and (under default `gateway.reload.mode = "hybrid"`) triggers a graceful SIGUSR1 restart within 1–3s. The new tools bind automatically.
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

   The agent will detect that the plugin is already loaded, skip the install steps, and jump straight to account setup.

The browser-side crypto and account-setup flow are identical to self-hosted setups; only the install step differs.

---

## Fully manual (CLI only — last resort)

If you'd rather drive the install yourself (self-hosted only — managed services don't expose the host shell):

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

> Pin a specific RC with `openclaw plugins install @totalreclaw/totalreclaw@3.3.1-rc.22`. Check what each tag resolves to: `npm view @totalreclaw/totalreclaw dist-tags`. Keep skill and plugin on the same version family (both stable or both RC).

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
| **Account setup** | "Set up TotalReclaw for me" — returns URL + PIN |

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
- **Agent declines to install (safety refusal)**: that's a sign the agent is doing its job. Ask it to walk through what each command does first; once it's comfortable that the commands are standard `openclaw plugins install` from a scoped npm namespace and the recovery phrase stays in your browser, it should proceed. If it still declines, a human can run the [Fully manual](#fully-manual-cli-only--last-resort) flow.
- **Agent can't see TotalReclaw tools after install**: under the default config OpenClaw auto-restarts the gateway within 1-3s of `openclaw plugins install` — wait 5-10s and check `openclaw plugins list`. If the plugin is listed but the agent still can't see the tools, your config likely has `gateway.reload.mode = off` — fall back to a manual restart. Native: `openclaw gateway restart`. Docker self-host: `docker restart <your-openclaw-container>`. Managed service: use the service's restart control.
- **Account-setup URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the account-setup page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
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
