# Hermes Agent + TotalReclaw

TotalReclaw gives your Hermes agent encrypted, persistent memory. One copy-paste prompt to the agent installs the plugin, restarts the gateway, and walks you through pairing in your browser.

## Install via chat (default)

Open your chat with your Hermes agent. Paste this message:

> **Install TotalReclaw (`pip install --pre totalreclaw` from PyPI) and walk me through the recovery-phrase setup**

The agent will install the package, restart its gateway, call the `totalreclaw_pair` tool, and give you a URL + PIN to enter your recovery phrase in the browser. Your phrase never touches the chat.

<details>
<summary>What happens behind the scenes</summary>

1. Agent runs `pip install --pre totalreclaw` (pip resolves to the latest RC on PyPI).
2. Agent runs `hermes gateway restart` so the newly installed plugin is picked up.
3. Agent calls the `totalreclaw_pair` tool.
4. A pair URL + 6-digit PIN is surfaced back to you in chat.
5. You open the URL in your browser and enter (or let the browser generate) your recovery phrase, then confirm the PIN.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives a ChaCha20-Poly1305 key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey to the gateway.
7. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
8. The agent confirms setup and your memory tools are live.

The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

</details>

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + ChaCha20-Poly1305 (Safari 17.2+ or Chromium 118+)

## Manual install (CLI)

If you'd rather run the commands yourself:

```bash
pip install --pre totalreclaw
hermes gateway restart
```

`--pre` lets pip resolve to the latest release candidate without pinning a version. Drop `--pre` once a stable is promoted. Ubuntu/Debian/Docker: add `--break-system-packages` or use a venv if you hit `externally-managed-environment`.

Then ask the agent "set up TotalReclaw for me" — it will call `totalreclaw_pair` and hand you the URL + PIN.

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

## Troubleshooting

- **Agent can't see TotalReclaw tools**: `hermes gateway restart`.
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **"No LLM available for auto-extraction"**: configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context.

## Returning user (new machine)

Paste the same prompt. When the pair page loads, choose "import" and enter your existing 12/24-word phrase. The browser encrypts it against the gateway's ephemeral key before uploading.

## See also

- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) — same vault, different runtime
