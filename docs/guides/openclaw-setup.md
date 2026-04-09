# TotalReclaw for OpenClaw

TotalReclaw gives your OpenClaw agent encrypted, persistent memory. Facts, preferences, and decisions are extracted automatically from conversations and recalled in future sessions. All data is encrypted on your device before it leaves -- the server never sees plaintext.

---

## Install

```bash
openclaw skills install totalreclaw
openclaw plugins install ~/.openclaw/workspace/skills/totalreclaw
```

Then start a conversation and tell your agent:

> "Set up TotalReclaw for me. Generate a new recovery phrase."

Write down the 12-word phrase and store it safely. There is no password reset -- this phrase is the only key to your memories.

**Returning user?** Say: *"I have an existing TotalReclaw recovery phrase: word1 word2 ... word12"* and your existing memories become accessible immediately.

> **Note:** The first interaction downloads a ~344MB embedding model. This is cached locally and only happens once.

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

You can also use memory directly by asking your agent:

| Tool | Example prompt |
|------|---------------|
| **Remember** | "Remember that I prefer PostgreSQL over MySQL" |
| **Recall** | "What do you remember about my database choices?" |
| **Forget** | "Forget what you know about my old email address" |
| **Export** | "Export all my TotalReclaw memories as plain text" |
| **Status** | "What's my TotalReclaw subscription status?" |

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
| "1 suspicious code pattern" warning | Normal. The plugin reads config and makes encrypted network calls. |
| Tools not appearing in conversations | Ensure your gateway config includes `"tools": { "allow": ["totalreclaw", "group:plugins"] }`. Rebuild the Docker image if using Docker. |
| "Not authenticated" / 401 | Check your recovery phrase -- exact words, exact order. |
| Memories not appearing | Try an explicit recall: *"What do you remember about X?"* |
| Quota exceeded (403) | Upgrade to Pro for permanent mainnet storage. |

For detailed technical reference (environment variables, configuration, architecture), see the [detailed guide](beta-tester-guide-detailed.md).

---

## Further Reading

- [Feature Comparison](feature-comparison.md) -- what works on each platform
- [Importing Memories](importing-memories.md) -- migrate from Mem0, ChatGPT, Claude, and more
- [Memory Dedup](memory-dedup.md) -- how duplicate prevention works
- [totalreclaw.xyz](https://totalreclaw.xyz)
