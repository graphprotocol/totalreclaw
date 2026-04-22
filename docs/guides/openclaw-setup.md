# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. Two install approaches — pick whichever fits your workflow.

---

## Fastest — skill + chat (Approach A, preferred)

Terminal:

```bash
openclaw skills install totalreclaw
openclaw gateway restart  # or `docker restart tr-openclaw` for Docker
```

Then in your OpenClaw chat:

> **Install totalreclaw**

The agent reads the skill, installs the plugin, asks you to restart the gateway once the plugin is in place, calls the pairing tool, and guides you through the QR flow.

Why this works: ClawHub classifies `totalreclaw` as a skill. `openclaw skills install totalreclaw` registers the SKILL.md into agent context **before** the plugin itself is installed — so the agent has the instructions to bootstrap the rest. The one `openclaw gateway restart` in the terminal makes the skill discoverable; the plugin install happens via shell from the agent, and a second user-driven restart binds the tools.

<details>
<summary><strong>Approach B — explicit two-step (fallback)</strong></summary>

If you'd rather spell out every step explicitly (useful if you prefer more control), install the skill the same way but use a verbose chat prompt:

Terminal:

```bash
openclaw skills install totalreclaw
openclaw gateway restart  # or `docker restart tr-openclaw` for Docker
```

Then in your OpenClaw chat:

> **TotalReclaw is a skill at 'totalreclaw' on ClawHub. Install it, then install the plugin, restart the gateway, and walk me through the QR pairing flow**

The agent reads the explicit directive, installs the plugin (`openclaw plugins install totalreclaw`), asks you to restart the gateway, calls `totalreclaw_pair`, and guides you through the QR flow.

</details>

<details>
<summary>What happens behind the scenes</summary>

1. `openclaw skills install totalreclaw` places the skill metadata + SKILL.md under `~/.openclaw/workspace/skills/totalreclaw/`.
2. The first gateway restart makes the skill visible to your agent's context.
3. The chat prompt triggers the skill's fast path: the agent runs `openclaw plugins install totalreclaw` via its shell tool.
4. The agent asks you to restart the gateway (`openclaw gateway restart` or `docker restart tr-openclaw`) so HTTP routes + hooks bind. The agent cannot self-restart the process it is running in.
5. Agent calls the `totalreclaw_pair` tool.
6. A pair URL + 6-digit PIN is surfaced back to you in chat.
7. You open the URL in your browser, enter (or let the browser generate) your recovery phrase, confirm the PIN.
8. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives a ChaCha20-Poly1305 key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey to the gateway.
9. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
10. The agent confirms setup and your memory tools are live. First real interaction downloads a ~216 MB embedding model (cached locally, one-time).

The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

</details>

---

## Prerequisites

- OpenClaw v3.2.0+ with the gateway running
- An up-to-date browser with WebCrypto x25519 + ChaCha20-Poly1305 (Safari 17.2+ or Chromium 118+)

---

## Fully manual (CLI only)

If you'd rather run every command yourself without any agent involvement:

```bash
openclaw plugins install @totalreclaw/totalreclaw
openclaw gateway restart              # or: docker restart tr-openclaw
```

Then ask the agent "set up TotalReclaw for me" — it will call `totalreclaw_pair` and hand you the URL + PIN.

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

- **Agent can't see TotalReclaw tools**: restart the gateway (`openclaw gateway restart` or `docker restart tr-openclaw`).
- **Pair URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running.
- **Browser fails to POST the encrypted phrase**: check the pair page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **Tool calls return "onboarding required"**: repeat the canonical prompt so the agent re-runs `totalreclaw_pair`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.

---

## Canonical prompts (these match the QA harness scenario contracts)

- Approach A: `Install totalreclaw`
- Approach B: `TotalReclaw is a skill at 'totalreclaw' on ClawHub. Install it, then install the plugin, restart the gateway, and walk me through the QR pairing flow`

---

## Further reading

- [Feature Comparison](feature-comparison.md)
- [Importing Memories](importing-memories.md)
- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Detailed reference](beta-tester-guide-detailed.md) — env vars, extraction tuning, architecture
- [totalreclaw.xyz](https://totalreclaw.xyz)
