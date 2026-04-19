# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. Facts, preferences, and decisions are extracted automatically from conversations and recalled in future sessions. All data is encrypted on your device before it leaves -- the server never sees plaintext.

---

## Install

One command -- install from npm:

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

The OpenClaw CLI resolves `@totalreclaw/totalreclaw` from the npm registry, activates the plugin, and wires it into your gateway. On first load the plugin auto-generates a recovery phrase if one is not already configured -- you do not need to run a separate setup command.

> **Note:** TotalReclaw is distributed via the npm registry. Future CLI resolvers may surface it via additional registries.

> **Note:** The first interaction downloads a ~344MB embedding model. This is cached locally and only happens once.

<details>
<summary>Developer / from-source install (non-primary)</summary>

If you are working from a fork or want to pin a specific local source tree, clone the repo and point `openclaw plugins install` at the local path:

```bash
git clone https://github.com/p-diogo/totalreclaw.git
openclaw plugins install ./totalreclaw/skill/plugin
```

This is for plugin development only. Users following this guide should stick with the one-line npm install above.

</details>

---

## Your recovery phrase

TotalReclaw is keyed by a 12-word BIP-39 recovery phrase. The plugin handles generation automatically the first time it loads.

**Where it is stored:** `~/.openclaw/extensions/totalreclaw/credentials.json` (owner-only permissions). This file contains your recovery phrase plus derived identifiers.

**You must save it somewhere safe.** It is the only key to your memories and the only way to use the same vault from another agent (Claude Desktop, Cursor, Hermes, etc.). There is no password reset, no recovery email, no support ticket that can recover lost memories.

> **Use a dedicated phrase.** Never reuse a recovery phrase that has been used for a blockchain wallet or any on-chain activity. TotalReclaw keys are memory-only -- they should not share entropy with funds.

### How to retrieve your phrase (current behavior, v3.1.0)

When the plugin generates a fresh phrase, it is written to the credentials file above. Depending on your LLM provider, the phrase may *also* appear in your first chat response via a one-time banner. **This surfacing is LLM-dependent -- it may or may not happen.** Either way, the phrase is saved on disk.

To retrieve it reliably:

```bash
cat ~/.openclaw/extensions/totalreclaw/credentials.json
```

Copy the `mnemonic` field and store it somewhere safe (password manager, offline paper backup -- not a plaintext note in the cloud).

> **Heads up:** v3.2.0 will improve onboarding so the phrase is surfaced outside the LLM channel. Until that ships, the `cat` command above is the source of truth.

### Returning user

If you already have a phrase from another client (Hermes, MCP, NanoClaw, etc.), tell the agent before the first extraction:

> "I have an existing TotalReclaw recovery phrase: word1 word2 ... word12"

The plugin will use it to re-derive your keys and your existing memories become accessible immediately. Alternatively, write the phrase into `credentials.json` yourself before first load.

---

## What Happens Automatically

Once set up, memory is fully automatic. You do not need to do anything.

| Hook | What it does |
|------|-------------|
| **Auto-recall** | Before every message, the agent searches your vault for relevant memories and injects them into context. |
| **Auto-extract** | Every 3 turns, the agent extracts important facts (preferences, decisions, context) and stores them encrypted. |
| **Pre-compaction flush** | Before the context window is compacted, all pending facts are extracted and saved so nothing is lost. |
| **Session debrief** | At the end of a conversation, the agent captures broader session-level context (up to 5 items). |

---

## Explicit Tools

You can also drive memory directly by asking your agent. The v1 taxonomy adds pin / retype / set_scope for when you want to curate a memory rather than let the automatic flow decide.

| Tool | What it does | Example prompt |
|------|--------------|---------------|
| **Remember** | Store a specific fact now | "Remember that I prefer PostgreSQL over MySQL" |
| **Recall** | Search your vault | "What do you remember about my database choices?" |
| **Forget** | Delete a memory | "Forget what you know about my old email address" |
| **Pin** | Mark a memory as permanent (won't decay / auto-evict) | "Pin that -- it's important" |
| **Unpin** | Remove the permanent flag | "Unpin the note about my old editor" |
| **Retype** | Reclassify an existing memory (`claim`, `preference`, `directive`, `commitment`, `episode`, `summary`) | "That should be a preference, not a fact" |
| **Set scope** | Reassign a memory to a scope (`work`, `personal`, `health`, `family`, `creative`, `finance`, `misc`) | "File that under work" |
| **Export** | Download everything as text / JSON | "Export all my TotalReclaw memories as plain text" |
| **Status** | Tier + usage | "What's my TotalReclaw status?" |
| **Import from** | Pull memories in from Mem0 / ChatGPT / Claude / Gemini | "Import my Gemini history from ~/Downloads/..." |
| **Setup** | Re-run setup (no-op when credentials already match) | "Set up TotalReclaw for me" |

> **Note on `setup`:** since v3.1.0, a fresh vault is bootstrapped automatically on first load. The `setup` tool remains available for explicit re-setup (e.g., switching recovery phrases) but is a no-op when your credentials are already in place.

---

## Importing Memories

Switching from another AI memory tool? TotalReclaw can import from Mem0, MCP Memory Server, ChatGPT, and Claude.

> "Import my memories from Mem0 using API key m0-your-key-here"

See the [Importing Memories guide](importing-memories.md) for all supported sources and instructions.

---

## Billing

| Tier | Storage | Price |
|------|---------|-------|
| **Free** | Unlimited on Base Sepolia testnet (may be reset) | $0 |
| **Pro** | Permanent on Gnosis mainnet | $3.99/month |

Both tiers have unlimited memories and reads. Pro adds permanent on-chain storage and LLM-guided dedup (catches contradictions, not just paraphrases).

Upgrade by asking your agent: *"Upgrade my TotalReclaw subscription."*

[See pricing on totalreclaw.xyz](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Plugin not loading | Restart the gateway. On first install, npm dependencies may still be installing in the background -- restart once more after a minute. |
| I can't find my recovery phrase | `cat ~/.openclaw/extensions/totalreclaw/credentials.json` -- the `mnemonic` field is your phrase. The LLM banner that prints the phrase on first load is not guaranteed to surface; the file is. |
| Tools not appearing in conversations | Ensure your gateway config includes `"tools": { "allow": ["totalreclaw", "group:plugins"] }`. Rebuild the Docker image if using Docker. |
| "Not authenticated" / 401 | Check your recovery phrase -- exact words, exact order, all lowercase, single spaces. |
| Memories not appearing | Try an explicit recall: *"What do you remember about X?"* |
| Quota exceeded (403) | Upgrade to Pro for permanent mainnet storage. |

> **Security scanner:** since plugin 3.1.0 (which builds on the 3.0.7 / 3.0.8 fs-helpers consolidation), `openclaw security audit --deep` reports **0 `code_safety` warnings** for totalreclaw. If you see a scanner warning on an older version, upgrade with `openclaw plugins install @totalreclaw/totalreclaw@latest`.

For detailed technical reference (environment variables, configuration, architecture), see the [detailed guide](beta-tester-guide-detailed.md).

---

## Further Reading

- [Feature Comparison](feature-comparison.md) -- what works on each platform
- [Importing Memories](importing-memories.md) -- migrate from Mem0, ChatGPT, Claude, and more
- [Memory Dedup](memory-dedup.md) -- how duplicate prevention works
- [totalreclaw.xyz](https://totalreclaw.xyz)
