# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. One copy-paste prompt to the agent installs the plugin, restarts the gateway, and walks you through pairing in your browser.

---

## Install via chat (default)

Open your chat with your OpenClaw agent. Paste this message:

> **Install TotalReclaw latest RC and walk me through the recovery-phrase setup**

The agent will install the package, restart its gateway, call the `totalreclaw_pair` tool, and give you a URL + PIN to enter your recovery phrase in the browser. Your phrase never touches the chat.

<details>
<summary>What happens behind the scenes</summary>

1. Agent runs `openclaw skills install totalreclaw` (resolves to the latest RC on ClawHub).
2. Agent restarts the gateway (`openclaw restart` native, or `docker restart tr-openclaw` in Docker) so HTTP routes and hooks bind.
3. Agent calls the `totalreclaw_pair` tool.
4. A pair URL + 6-digit PIN is surfaced back to you in chat.
5. You open the URL in your browser and enter (or let the browser generate) your recovery phrase, then confirm the PIN.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives a ChaCha20-Poly1305 key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey to the gateway.
7. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
8. The agent confirms setup and your memory tools are live.

The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction. Your first real interaction will download a ~216 MB embedding model (cached locally, one-time).

</details>

---

## Prerequisites

- OpenClaw v3.2.0+ with the gateway running
- An up-to-date browser with WebCrypto x25519 + ChaCha20-Poly1305 (Safari 17.2+ or Chromium 118+)

---

## Manual install (CLI)

If you'd rather run the commands yourself:

```bash
openclaw skills install totalreclaw
openclaw restart              # or: docker restart tr-openclaw
```

`openclaw skills install totalreclaw` resolves to the latest RC on ClawHub. Then ask the agent "set up TotalReclaw for me" — it will call `totalreclaw_pair` and hand you the URL + PIN.

<details>
<summary>From-source install (for plugin development)</summary>

```bash
git clone https://github.com/p-diogo/totalreclaw.git
openclaw plugins install ./totalreclaw/skill/plugin
```

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

Your recovery phrase is the only key to your memories. Store it safely. Retrieve later with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. On a new machine, paste the canonical prompt above and choose "import" in the browser.

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

- **Agent can't see TotalReclaw tools**: restart the gateway (`openclaw restart` or `docker restart tr-openclaw`).
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: repeat the canonical prompt so the agent re-runs `totalreclaw_pair`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.

---

## Further reading

- [Feature Comparison](feature-comparison.md)
- [Importing Memories](importing-memories.md)
- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Detailed reference](beta-tester-guide-detailed.md) — env vars, extraction tuning, architecture
- [totalreclaw.xyz](https://totalreclaw.xyz)
