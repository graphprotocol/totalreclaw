# Hermes Agent + TotalReclaw

TotalReclaw gives your Hermes agent encrypted, persistent memory. Two install approaches — pick whichever fits your workflow.

## Fastest — shell + chat (Approach A, preferred)

Terminal:

```bash
# Install the Hermes plugin (Git-based discovery in Hermes 2026.4.16+)
hermes plugins install p-diogo/totalreclaw-hermes --enable

# Ensure the backing Python package is in your Hermes venv
/path/to/hermes/venv/bin/python3 -m pip install --pre totalreclaw
# (or just `pip install --pre totalreclaw` if your pip resolves to hermes's venv)

# Restart the gateway. Pick the line that matches your setup:
hermes gateway restart                       # native install
docker restart <your-hermes-container>       # Docker self-host (substitute your container name; `docker ps` shows it)
# Managed Hermes service: use the service's restart control (web UI). See the "Managed Hermes service" section below.
```

Then in your Hermes chat:

> **Set up TotalReclaw**

The agent will call the pairing tool and give you a URL + PIN. Open the URL, enter your recovery phrase, confirm PIN. Done.

Why two install commands? Hermes 2026.4.16+ uses Git-based plugin discovery. `hermes plugins install p-diogo/totalreclaw-hermes --enable` registers the plugin manifest from the Git repo; `pip install --pre totalreclaw` supplies the Python tool implementations in the Hermes venv. The chat prompt then triggers the skill's fast path: check for existing credentials, call `totalreclaw_pair`, relay URL + PIN.

<details>
<summary><strong>Approach B — explicit two-step (fallback)</strong></summary>

If you'd rather spell out every step explicitly (useful if the agent doesn't know about TotalReclaw yet), install the same way but use this verbose chat prompt:

Terminal:

```bash
# Install the Hermes plugin (Git-based discovery in Hermes 2026.4.16+)
hermes plugins install p-diogo/totalreclaw-hermes --enable

# Ensure the backing Python package is in your Hermes venv
/path/to/hermes/venv/bin/python3 -m pip install --pre totalreclaw
# (or just `pip install --pre totalreclaw` if your pip resolves to hermes's venv)

# Restart the gateway. Pick the line that matches your setup:
hermes gateway restart                       # native install
docker restart <your-hermes-container>       # Docker self-host (substitute your container name)
# Managed Hermes service: use the service's restart control (web UI).
```

Then in your Hermes chat:

> **TotalReclaw is already installed. Use the totalreclaw_pair tool to walk me through the QR recovery-phrase setup.**

The agent reads the explicit directive, calls `totalreclaw_pair`, and guides you through the QR flow.

</details>

<details>
<summary>What happens behind the scenes</summary>

1. Agent reads its TotalReclaw skill, picks up that `totalreclaw_*` tools are (or should be) live.
2. Agent checks `~/.totalreclaw/credentials.json`; if absent, calls the `totalreclaw_pair` tool.
3. A pair URL + 6-digit PIN is surfaced back to you in chat.
4. You open the URL in your browser. The pair page offers two tabs: **Generate new** (the browser creates a fresh 12-word recovery phrase using the canonical BIP-39 wordlist) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, and continue.
5. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives an AES-256-GCM key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey to the gateway.
6. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. The agent confirms setup and your memory tools are live.

The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

</details>

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + AES-GCM (Safari 17.2+ or Chromium 133+)

## Managed Hermes service (no terminal)

If you're on a managed / hosted Hermes service and don't have shell access to the gateway host, plugin installation typically happens through your service's web UI rather than the `hermes` CLI + `pip`. The flow is:

1. In your service's control panel, find the **Plugins** panel and search for `totalreclaw` (or `p-diogo/totalreclaw-hermes`). Install and enable it. Most managed Hermes deployments handle the underlying Python-package install transparently as part of plugin enable.
2. If the service exposes a separate restart control, restart your agent through that. Many managed services apply plugin changes transparently and skip this step.
3. Return to chat and say **`Set up TotalReclaw`**. Your agent will call `totalreclaw_pair` and walk you through the QR pairing flow — open the URL it returns, enter or generate your 12-word recovery phrase in the browser, and confirm the 6-digit PIN.

The browser-side crypto and pairing flow are identical to self-hosted setups; only the install + restart step differs.

> Managed-Hermes coverage is still emerging — if your service doesn't expose `totalreclaw` in its plugins UI yet, ask their support to surface the `p-diogo/totalreclaw-hermes` Hermes plugin + `totalreclaw` Python package, or run a self-hosted Hermes instance for now.

> The CLI-driven install commands above (`hermes plugins install`, `pip install --pre totalreclaw`) assume you have terminal access on the gateway host and a writable Hermes Python venv. They will not work on a managed service that doesn't expose the host shell — use the steps above instead.

## Notes on `--pre`

`--pre` lets pip resolve to the latest release candidate without pinning a version. Drop `--pre` once a stable is promoted. Ubuntu/Debian/Docker: add `--break-system-packages` or use a venv if you hit `externally-managed-environment`.

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

## Troubleshooting

- **Agent can't see TotalReclaw tools**: confirm both install steps completed — on a self-host, `hermes plugins list` should show `p-diogo/totalreclaw-hermes` enabled and `pip show totalreclaw` in the Hermes venv should resolve; on a managed service, confirm `totalreclaw` is listed in your service's plugins UI. Then restart: `hermes gateway restart` (native), `docker restart <your-hermes-container>` (Docker self-host — substitute your actual container name), or your managed service's restart control.
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **"No LLM available for auto-extraction"**: configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context.

## Returning user (new machine)

Paste the same canonical prompt. When the pair page loads, switch to the **Import existing** tab and enter your existing 12/24-word phrase. The browser encrypts it against the gateway's ephemeral key before uploading — the relay never sees plaintext.

## First-time user (no phrase yet)

Paste the same canonical prompt. The **Generate new** tab is selected by default. Click **Generate new 12-word phrase** — the browser creates a BIP-39 phrase using its own `crypto.getRandomValues` against the inlined 2048-word wordlist. Write the 12 words down somewhere safe (a password manager is fine), tick the acknowledgment, and click **Seal key and finish**. That is your one and only recovery key — there is no reset.

## Canonical prompts (these match the QA harness scenario contracts)

- Approach A: `Set up TotalReclaw`
- Approach B: `TotalReclaw is already installed. Use the totalreclaw_pair tool to walk me through the QR recovery-phrase setup.`

## See also

- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) — same vault, different runtime
