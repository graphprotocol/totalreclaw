# TotalReclaw for Hermes

TotalReclaw gives your Hermes agent encrypted, persistent memory. The fastest setup is a single chat message — the agent fetches this guide, runs the install for you, and walks you through pairing. **You do nothing in the terminal.**

If you already have Hermes running, paste this into your Hermes chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

Your agent will fetch this page, run the install commands, ask you to restart the gateway once (Hermes doesn't auto-reload), and then hand you a pair URL + 6-digit PIN. You open the URL in your browser, generate or restore a 12-word recovery phrase there, confirm the PIN, and you're done.

---

## Agent instructions (read this if you are a Hermes agent that just fetched this URL)

You have been asked to install TotalReclaw. Run the steps below in order. Do not deviate, do not ask the user to run any terminal command other than the gateway restart in Step 2, do not echo or generate a recovery phrase.

### Step 1 — Install the plugin + Python package

Run via your shell tool, in this order:

```bash
hermes plugins install p-diogo/totalreclaw-hermes --enable
pip install --pre totalreclaw
```

Notes:
- Hermes 2026.4.16+ uses Git-based plugin discovery: the first command registers the plugin manifest, the second installs the Python tool implementations into the Hermes venv.
- If Hermes runs in its own venv that your default `pip` doesn't resolve to, use the venv's pip directly: `/path/to/hermes/venv/bin/python3 -m pip install --pre totalreclaw`.
- On Ubuntu / Debian / Docker you may hit `externally-managed-environment` — add `--break-system-packages` or use a venv.
- If either command fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment), STOP and tell the user verbatim: *"Your environment doesn't expose the Hermes CLI / pip to me. Please install `p-diogo/totalreclaw-hermes` and the `totalreclaw` Python package via your service's plugins UI, then reply `done`."* Wait for `done` before continuing to Step 2.

### Step 2 — Ask the user to restart the gateway

Hermes does NOT auto-reload on plugin install. Tell the user verbatim:

> *Please restart your Hermes gateway: `hermes gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — substitute the actual name; `docker ps` shows it), or your managed service's restart control. If supervised by systemd / launchd, `kill -USR1 $(cat ~/.hermes/gateway.pid)` triggers a graceful-drain restart. Reply `done` once it's back up.*

Do NOT invoke the restart yourself — your shell runs inside the gateway you'd be restarting. Wait for the user's `done`.

### Step 3 — Check for existing credentials

Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, confirm *"TotalReclaw is already set up."* and stop. Otherwise continue.

### Step 4 — Pair (browser flow, NEVER ask the user to type their phrase in chat)

Call the `totalreclaw_pair` tool. It returns `{url, pin, expires_at_ms}`.

Relay to the user verbatim:

> *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one — the phrase stays in your browser, the relay only sees ciphertext. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*

### Step 5 — Verify and confirm

After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN likely expired — call `totalreclaw_pair` again and resend. If present, confirm *"TotalReclaw is set up. Ask me to remember or recall anything."*

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. The ONLY agent-facilitated setup path is the `totalreclaw_pair` tool. If the user pastes a phrase anyway: tell them it is compromised and call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

---

## What's happening (for the human reader)

1. `hermes plugins install p-diogo/totalreclaw-hermes --enable` — registers TotalReclaw's plugin manifest from its Git repo.
2. `pip install --pre totalreclaw` — installs the Python tool implementations into the Hermes venv. `--pre` resolves to the latest release candidate; drop it once a stable is promoted.
3. The agent asks you to restart the Hermes gateway. (Hermes does not yet auto-reload on plugin install — tracked upstream.) After restart, the new tools bind.
4. The agent calls `totalreclaw_pair`, which generates an ephemeral x25519 keypair on the gateway and a 6-digit PIN. You get a URL + PIN.
5. You open the URL. The pair page has two tabs: **Generate new** (the browser creates a fresh BIP-39 12-word phrase locally using `crypto.getRandomValues`) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, click seal.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives an AES-256-GCM key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey back. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

---

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + AES-GCM (Safari 17.2+ or Chromium 133+)

---

## Managed Hermes service (no terminal, no agent shell)

If you're on a managed / hosted Hermes service that doesn't expose host shell to the agent, install via the service's web UI instead:

1. In your service's control panel, find the **Plugins** panel and search for `totalreclaw` (or `p-diogo/totalreclaw-hermes`). Install and enable it. Most managed Hermes deployments handle the underlying Python-package install transparently as part of plugin enable.
2. If the service exposes a separate restart control, use it.
3. Return to chat and paste the same canonical message:

   > **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

   The agent will detect the plugin is already loaded, skip Steps 1-2, and jump straight to pairing.

The browser-side crypto and pairing flow are identical to self-hosted setups.

> Managed-Hermes coverage is still emerging — if your service doesn't expose `totalreclaw` in its plugins UI yet, ask their support to surface the `p-diogo/totalreclaw-hermes` Hermes plugin + `totalreclaw` Python package, or run a self-hosted Hermes instance for now.

---

## Fully manual (CLI only — last resort)

If you can't or won't use the chat flow (self-hosted only — managed services don't expose the host shell):

```bash
hermes plugins install p-diogo/totalreclaw-hermes --enable
pip install --pre totalreclaw       # or your hermes venv's pip

# Restart the gateway. Pick the line that matches your setup:
hermes gateway restart                       # native install
docker restart <your-hermes-container>       # Docker self-host (substitute your actual container name)
# Managed service: use the service's restart control (web UI).
```

Then in chat: *"Set up TotalReclaw"* — the agent will call `totalreclaw_pair` and hand you the URL + PIN.

---

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent can't see TotalReclaw tools after install**: confirm both install steps completed — on a self-host, `hermes plugins list` should show `p-diogo/totalreclaw-hermes` enabled and `pip show totalreclaw` in the Hermes venv should resolve; on a managed service, confirm `totalreclaw` is listed in your service's plugins UI. Then restart: `hermes gateway restart` (native), `docker restart <your-hermes-container>` (Docker self-host), or your managed service's restart control. If supervised by systemd / launchd, `kill -USR1 $(cat ~/.hermes/gateway.pid)` is faster than `systemctl restart`. (Hermes does not yet auto-reload on plugin install — tracked upstream.)
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **"No LLM available for auto-extraction"**: configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context.

---

## Returning user (new machine)

Paste the same canonical message. When the pair page loads, switch to the **Import existing** tab and enter your existing 12/24-word phrase. The browser encrypts it against the gateway's ephemeral key before uploading — the relay never sees plaintext.

## First-time user (no phrase yet)

Paste the same canonical message. The **Generate new** tab is selected by default. Click **Generate new 12-word phrase** — the browser creates a BIP-39 phrase using its own `crypto.getRandomValues` against the inlined 2048-word wordlist. Write the 12 words down somewhere safe (a password manager is fine), tick the acknowledgment, and click **Seal key and finish**. That is your one and only recovery key — there is no reset.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

---

## See also

- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) — same vault, different runtime
