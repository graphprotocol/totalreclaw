# Hermes Agent + TotalReclaw

TotalReclaw gives your Hermes agent encrypted, persistent memory. Install the plugin, pair your vault from your browser, chat.

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + ChaCha20-Poly1305 (Safari 17.2+ or Chromium 118+) for QR pairing

## 1. Install

```bash
pip install totalreclaw
```

Hermes auto-discovers the plugin via entry-point registration at next start. No manual copy, no config editing. Ubuntu/Debian/Docker: add `--break-system-packages` or use a venv if you hit `externally-managed-environment`.

**Installing a release candidate (RC / pre-release)?** Pre-releases on PyPI are hidden from `pip install` by default. Use `--pre` and pin the version explicitly:

```bash
pip install --pre totalreclaw==2.3.1rc4        # replace with the RC version you want
```

Find the latest RC via `pip index versions totalreclaw --pre` or on [PyPI](https://pypi.org/project/totalreclaw/#history). Never install an RC on production — only for QA against staging.

> **2.3.1rc4 changed the console-script list.** rc.3 and earlier shipped a `hermes` entry point that collided with the upstream `hermes-agent` CLI. rc.4 removes it — now `pip install totalreclaw` only creates the `totalreclaw` binary. If you upgraded from rc.3 and your `hermes` binary was overwritten, reinstall hermes-agent to restore it (`pip install --force-reinstall hermes-agent`).

## 2. Set up your vault (default: QR pair flow)

In any Hermes chat session, ask the agent to set up TotalReclaw. The agent will call the `totalreclaw_pair` tool and relay a URL + 6-digit PIN:

> "Open http://127.0.0.1:58391/pair/\<token\> in your browser, enter your phrase (or let the browser generate a new one), and confirm PIN 492731."

**What happens under the hood:**

1. Your browser fetches the pair page over loopback HTTP.
2. The browser generates an ephemeral x25519 keypair, does ECDH against the gateway's ephemeral pubkey (passed in the URL `#fragment`, so it never hits server logs), derives a ChaCha20-Poly1305 key via HKDF-SHA256.
3. You type (or let the browser generate) your recovery phrase.
4. The browser encrypts the phrase locally and POSTs ciphertext + nonce + its pubkey to the gateway.
5. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).

**The recovery phrase never crosses the LLM context.** Not the chat transcript, not the agent's shell stdout, not the tool-call payload. Browser-side crypto keeps it isolated from the LLM round-trip by construction.

After the browser says "Pairing complete", restart Hermes so the plugin picks up the new credentials:

```bash
hermes gateway restart
```

### If you prefer local-terminal setup (user-terminal ONLY — do NOT run this through an agent)

```bash
totalreclaw setup
```

This runs the phrase wizard entirely in **your** terminal. Pick "generate" or "import". The phrase is written to `~/.totalreclaw/credentials.json` (mode `0600`) silently. Retrieve it later with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. Save it somewhere safe — it is the ONLY way to recover your vault.

> **Do NOT ask an agent to run `totalreclaw setup` for you.** The agent's shell-tool stdout is captured into LLM context. Even though `totalreclaw setup` never prints the phrase by default, running phrase-related CLIs through the agent's shell is a phrase-safety hazard: any future flag change or regression could leak. The agent should ONLY use `totalreclaw_pair`.

## 3. Verify

Ask "What's my TotalReclaw status?", or run `hermes` then `/plugins` -- `totalreclaw` should be listed.

## Troubleshooting

| Issue | Fix |
|---|---|
| "No LLM available for auto-extraction" | Configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically. |
| Plugin not in `/plugins` | `hermes gateway restart`. |
| Pair page says "browser lacks x25519" | Upgrade to Safari 17.2+ or a recent Chromium. Older browsers can't run the AEAD crypto. |
| Pair URL unreachable from phone | The pair HTTP server binds to `127.0.0.1` by default. For remote-phone setup, SSH-port-forward the port (`ssh -L 58391:127.0.0.1:58391 ...`) or run the browser on the gateway host directly. |
| Recovery phrase appeared in chat | File a bug. Rotate: generate a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context. |

## Returning user (new machine)

Use `totalreclaw_pair` with `mode=import`: the browser page accepts your existing 12/24-word phrase and encrypts it against the gateway's ephemeral key before uploading.

## See also

- [Memory types guide](memory-types-guide.md) -- v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) -- same vault, different runtime
