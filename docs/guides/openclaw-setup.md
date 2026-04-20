# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. Facts, preferences, and decisions are extracted automatically from conversations and recalled in future sessions. All data is encrypted on your device before it leaves -- the server never sees plaintext.

---

## Install

One command -- install from npm:

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

The OpenClaw CLI resolves `@totalreclaw/totalreclaw` from the npm registry, activates the plugin, and wires it into your gateway. Starting in **v3.2.0**, first-run setup uses a secure CLI wizard that runs on your terminal -- the plugin does not auto-generate a recovery phrase silently, and it never asks the LLM to display one.

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

TotalReclaw is keyed by a 12-word BIP-39 recovery phrase. **v3.2.0 introduces a secure CLI wizard** that generates or imports the phrase entirely on your terminal -- it never touches the LLM, never appears in a chat transcript, and never leaves your machine.

**Where it is stored:** `~/.totalreclaw/credentials.json` (mode `0600`, owner-only). This file contains your recovery phrase plus derived identifiers. A separate `~/.totalreclaw/state.json` file tracks onboarding state; it never contains secrets.

**You must save the phrase somewhere safe.** It is the only key to your memories and the only way to use the same vault from another agent (Claude Desktop, Cursor, Hermes, etc.). There is no password reset, no recovery email, no support ticket that can recover lost memories.

> **Use a dedicated phrase.** Never reuse a recovery phrase that has been used for a blockchain wallet or any on-chain activity. TotalReclaw keys are memory-only -- they should not share entropy with funds.

### First-time setup (v3.2.0)

After installing the plugin, open a terminal on the same machine as your OpenClaw gateway and run:

```bash
openclaw totalreclaw onboard
```

The wizard asks whether you want to:

1. **Generate** a new recovery phrase. The wizard prints a 3×4 word grid on your terminal, walks through a "write it down" warning, and requires you to retype three specific words to prove you saved it. On success, the phrase is persisted to `credentials.json` and memory tools become active. The phrase is displayed ONLY on your terminal -- it is not sent to the LLM, not written to any transcript, and not transmitted over the network.

2. **Import** an existing TotalReclaw recovery phrase (if you already set up TotalReclaw on another client). The wizard accepts the 12-word phrase via hidden stdin (input is masked with `*`), validates the BIP-39 checksum, and persists it to `credentials.json`. Your existing memories become accessible immediately.

3. **Skip** for now. Memory tools stay disabled until you re-run the wizard.

After the wizard completes, go back to your chat session. You can check state at any time:

```bash
openclaw totalreclaw status
```

### In-chat prompts

If you ask the agent in chat "set up TotalReclaw for me", the LLM will call the `totalreclaw_onboarding_start` tool, which returns a pointer back to the CLI wizard. Similarly, typing `/totalreclaw onboard` as a slash command returns a pointer. **Neither surface ever shows your recovery phrase in chat** -- that would leak it to the LLM provider's logs.

### Remote-gateway note

v3.2.0 onboarding works locally. If you run OpenClaw on a remote VPS and connect via `openclaw tui --url ws://...`, the wizard needs TTY access on the same machine that writes `credentials.json` -- SSH into the gateway host and run `openclaw totalreclaw onboard` there. Remote onboarding via QR-pairing is planned for v3.3.0.

### Retrieving your phrase later

To view your phrase on the same machine after setup:

```bash
cat ~/.totalreclaw/credentials.json
```

Copy the `mnemonic` field. On a new machine, run `openclaw totalreclaw onboard` and choose "import" with this phrase.

### Returning user

Run the wizard, choose "import", and paste your existing phrase when prompted. The plugin re-derives your keys and your existing memories become accessible immediately.

Do **not** paste your phrase into the chat -- that ships it to the LLM provider. The hidden stdin prompt in the wizard is the only safe surface.

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
| **Onboarding start** | Point the user at the secure CLI wizard (v3.2.0+) | "Set up TotalReclaw for me" |

> **Note on `setup`:** as of v3.2.0, onboarding moves to the `openclaw totalreclaw onboard` CLI wizard (see [First-time setup](#first-time-setup-v320) above). The legacy `totalreclaw_setup` tool is **deprecated** -- it now rejects phrase arguments and redirects to the CLI to prevent the phrase from leaking to the LLM provider. Use the `totalreclaw_onboarding_start` tool (or the `/totalreclaw onboard` slash command) if you want the agent to point the user at the wizard.

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
