# Hermes Agent + TotalReclaw

TotalReclaw gives your Hermes agent encrypted, persistent memory. Two install approaches — pick whichever fits your workflow.

## Fastest — shell + chat (Approach A, preferred)

Terminal:

```bash
pip install --pre totalreclaw
hermes gateway restart    # or `docker restart tr-hermes` for Docker Hermes
```

Then in your Hermes chat:

> **Set up TotalReclaw**

The agent will call the pairing tool and give you a URL + PIN. Open the URL, enter your recovery phrase, confirm PIN. Done.

Why this works: the Hermes pip package bundles both `SKILL.md` (agent instructions) and the plugin into the same wheel, so once pip + restart complete, the agent has everything it needs. The chat prompt triggers the skill's fast path: check for existing credentials, call `totalreclaw_pair`, relay URL + PIN.

<details>
<summary><strong>Approach B — explicit two-step (fallback)</strong></summary>

If you'd rather spell out every step explicitly (useful if the agent doesn't know about TotalReclaw yet), install the same way but use this verbose chat prompt:

Terminal:

```bash
pip install --pre totalreclaw
hermes gateway restart    # or `docker restart tr-hermes` for Docker Hermes
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
4. You open the URL in your browser and enter (or let the browser generate) your recovery phrase, then confirm the PIN.
5. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives a ChaCha20-Poly1305 key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey to the gateway.
6. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. The agent confirms setup and your memory tools are live.

The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

</details>

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + ChaCha20-Poly1305 (Safari 17.2+ or Chromium 118+)

## Notes on `--pre`

`--pre` lets pip resolve to the latest release candidate without pinning a version. Drop `--pre` once a stable is promoted. Ubuntu/Debian/Docker: add `--break-system-packages` or use a venv if you hit `externally-managed-environment`.

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

## Troubleshooting

- **Agent can't see TotalReclaw tools**: `hermes gateway restart`.
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **"No LLM available for auto-extraction"**: configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context.

## Returning user (new machine)

Paste the same canonical prompt. When the pair page loads, choose "import" and enter your existing 12/24-word phrase. The browser encrypts it against the gateway's ephemeral key before uploading.

## Canonical prompts (these match the QA harness scenario contracts)

- Approach A: `Set up TotalReclaw`
- Approach B: `TotalReclaw is already installed. Use the totalreclaw_pair tool to walk me through the QR recovery-phrase setup.`

## See also

- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) — same vault, different runtime
