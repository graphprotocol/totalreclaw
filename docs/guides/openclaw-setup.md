# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. Facts, preferences, and decisions are extracted automatically and recalled in future sessions. All data is encrypted on your device -- the server never sees plaintext.

---

## Install

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

First-run setup runs a secure CLI wizard (v3.2.0+); the plugin never auto-generates a recovery phrase silently and never asks the LLM to display one.

> **Restart required:** after `plugins install`, restart the gateway so HTTP routes and hooks bind: `openclaw restart` (native) or `docker restart openclaw-qa` (Docker). If you skip this, tool calls return "onboarding required" or 404.

> **First interaction downloads a ~216 MB embedding model.** Cached locally, one-time.

**Installing a release candidate (RC / pre-release)?** npm pre-releases are published under the `rc` dist-tag (or you can pin by exact version). Never install an RC on production — only for QA against staging.

```bash
openclaw plugins install @totalreclaw/totalreclaw@rc               # always the latest RC
openclaw plugins install @totalreclaw/totalreclaw@3.3.1-rc.2       # pin exact version
```

Find the current RC via `npm view @totalreclaw/totalreclaw dist-tags` or the [npm page](https://www.npmjs.com/package/@totalreclaw/totalreclaw?activeTab=versions).

<details>
<summary>From-source install (for plugin development)</summary>

```bash
git clone https://github.com/p-diogo/totalreclaw.git
openclaw plugins install ./totalreclaw/skill/plugin
```

</details>

---

## Your recovery phrase

TotalReclaw is keyed by a 12-word BIP-39 recovery phrase. QR pairing generates or imports it in your **browser**, which then ships the phrase to the gateway end-to-end-encrypted. The phrase never touches the LLM, the chat transcript, or the agent's shell-tool stdout.

**Stored at:** `~/.totalreclaw/credentials.json` (mode `0600`, owner-only). A separate `~/.totalreclaw/state.json` tracks onboarding state and never contains secrets.

**Save the phrase somewhere safe.** It is the only key to your memories and the only way to use the same vault from another agent (Claude Desktop, Cursor, Hermes, etc.). No password reset, no recovery email, no support ticket that can recover lost memories.

### Setup (default: QR pairing — agent-facilitated)

Ask the OpenClaw agent in chat: "Set up TotalReclaw for me." The agent will call the `totalreclaw_pair` tool and relay a URL + 6-digit PIN:

> "Open https://your-gateway/plugin/totalreclaw/pair/finish?sid=...#pk=... in your browser, enter your phrase (or let the browser generate one), and confirm PIN 492731."

**What happens under the hood:**

1. Your browser fetches the pair page.
2. The browser performs x25519 ECDH against the gateway's ephemeral public key (passed in the URL `#fragment` — never hits server logs), derives a ChaCha20-Poly1305 key via HKDF-SHA256.
3. You type (or let the browser generate) your recovery phrase.
4. The browser encrypts locally and uploads ciphertext + nonce + its own pubkey + PIN.
5. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode 0600).

**The recovery phrase never enters the LLM context.** Not the chat, not the agent's reasoning, not the agent's shell stdout, not the tool-call payload. Browser-side crypto keeps it isolated by construction.

Session TTL is 15 minutes by default (5-60 min configurable). The QR URL is single-use. Supported browsers: Safari 17.2+, Chromium 118+.

### Setup (user-terminal ONLY — do NOT run this through an agent)

If you prefer to set up entirely in your own terminal:

```bash
openclaw totalreclaw onboard
```

The wizard asks:

1. **Generate** a new phrase. Printed as a 3x4 grid on your terminal with a "write it down" warning; you retype three specific words to prove you saved it. Persisted to `credentials.json` on success. Phrase never leaves the terminal.
2. **Import** an existing phrase. Hidden stdin (masked with `*`), BIP-39 checksum validation.
3. **Skip** for now. Memory tools stay disabled until you re-run.

Check state any time: `openclaw totalreclaw status`.

> **Do NOT ask an agent to run `openclaw totalreclaw onboard` through its shell tool.** Agent shell stdout is captured into LLM context. Even though the wizard doesn't print the phrase by default, running phrase-related CLIs via an agent shell is a phrase-safety hazard — the agent MUST use `totalreclaw_pair` instead. The CLI is for you, not the agent.

### In-chat prompts

Ask the agent "set up TotalReclaw for me" and it should call `totalreclaw_pair` directly. For users who explicitly prefer local-terminal setup, the agent falls back to `totalreclaw_onboarding_start` — a pointer-only tool that tells YOU to run the CLI wizard yourself. The agent never runs the wizard for you.

### rc.4 phrase-safety changes (3.3.1-rc.4+)

Per `project_phrase_safety_rule.md`:

- `totalreclaw_onboard` agent tool — **REMOVED**. Even with `emitPhrase: false`, nothing architecturally prevented leakage. Use `totalreclaw_pair`.
- `totalreclaw setup` / `openclaw totalreclaw onboard` CLI commands — **KEPT but user-terminal only**. They MUST NOT be invoked via any agent shell tool.
- `totalreclaw_pair` agent tool — **CANONICAL**. Browser-side x25519 + ChaCha20-Poly1305 + HKDF-SHA256 keeps the phrase out of the LLM round-trip by construction. Now ported to Hermes Python as well (v2.3.1rc4+).

### Retrieving your phrase later

```bash
cat ~/.totalreclaw/credentials.json | jq -r .mnemonic
```

On a new machine: run `openclaw totalreclaw onboard` and choose "import". Do NOT paste the phrase into chat.

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
| **Onboard** | "Set up TotalReclaw for me" -- points you at the CLI wizard |
| **Pair** (remote) | "Help me set up TotalReclaw on my VPS" -- returns QR + PIN + URL (v3.3.1+) |

> The legacy `totalreclaw_setup` tool is **deprecated** (v3.2.0+) -- it rejects phrase arguments and redirects to the CLI to prevent the phrase leaking to the LLM provider.

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

| Problem | Fix |
|---------|-----|
| Plugin not loading | Restart the gateway: `openclaw restart` or `docker restart openclaw-qa`. |
| Tool calls return "onboarding required" | `openclaw totalreclaw onboard` (local) or `openclaw totalreclaw pair generate` (remote). |
| Routes return 404 after `plugins install` | Gateway wasn't restarted. Restart it. |
| Can't find my recovery phrase | `cat ~/.totalreclaw/credentials.json \| jq -r .mnemonic` |
| Tools not appearing | Gateway config needs `"tools": { "allow": ["totalreclaw", "group:plugins"] }`. Rebuild Docker image if applicable. |
| "Not authenticated" / 401 | Check your phrase -- exact words, exact order, lowercase, single spaces. |
| Memories not appearing | Try explicit recall: *"What do you remember about X?"* |
| `plugins.allow is empty` warning | OpenClaw-side, not a TotalReclaw bug. Add to allowlist or ignore. |
| `No LLM available for auto-extraction` | Fixed in v3.3.1 (reads `~/.openclaw/agents/<agent>/agent/auth-profiles.json`). Upgrade, or set `plugins.entries.totalreclaw.config.extraction.llm.{provider,apiKey}` explicitly. |
| Quota exceeded (403) | Upgrade to Pro. |

---

## Further reading

- [Feature Comparison](feature-comparison.md)
- [Importing Memories](importing-memories.md)
- [Memory types guide](memory-types-guide.md) -- v1 taxonomy
- [Detailed reference](beta-tester-guide-detailed.md) -- env vars, extraction tuning, architecture
- [totalreclaw.xyz](https://totalreclaw.xyz)
