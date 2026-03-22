# TotalReclaw -- Private Memory for Your AI Agent

**What it is:** Encrypted memory that works across sessions. Your AI remembers your preferences, decisions, and context -- and only you can read it. Not even our server sees your data.

**Works with:** OpenClaw, Claude Desktop, Cursor, NanoClaw, or any MCP-compatible agent.

---

## TL;DR — OpenClaw Users

Ask your agent:

> "Install the @totalreclaw/totalreclaw plugin"

That's it. The agent will install it, guide you through setup (recovery phrase), and memory is automatic from there. Read on for details, other platforms, or troubleshooting.

---

## Quick Start

### OpenClaw (recommended -- fully automatic)

Ask your OpenClaw agent:

> "Install the @totalreclaw/totalreclaw plugin"

Or if you prefer the terminal:

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

After installation, your agent will ask: *"Do you have an existing recovery phrase, or should I generate a new one?"*

- **New user:** Let it generate one (it uses a cryptographically secure generator). Write down the 12 words on paper, in exact order.
- **Returning user:** Enter your existing phrase to restore your memories.

> **Note:** The first run downloads a ~600MB embedding model for local inference. This is cached locally and only happens once.

**After setup, memory is fully automatic:**
- Your agent remembers important things from conversations (preferences, decisions, facts)
- At the start of each conversation, relevant memories are loaded automatically
- You never need to tell your agent to "remember" anything -- it just does

You can always be explicit too -- "remember that I prefer dark mode" or "what do you remember about my project?" -- but it's not required.

### Claude Desktop / Cursor / Other MCP Agents

1. Run the setup wizard:
   ```bash
   npx @totalreclaw/mcp-server setup
   ```
   The wizard will ask if you have an existing recovery phrase, generate one if needed, register you, and print a config snippet to paste into your MCP client.

2. Paste the config snippet (the wizard tells you exactly where).

3. Start chatting. Your agent has memory tools it can use when appropriate.

**Note:** MCP agents don't have automatic memory like OpenClaw. The agent uses the tools when contextually appropriate, and you can also explicitly ask it to remember or recall things.

### NanoClaw

NanoClaw agents get TotalReclaw memory automatically — no setup required on your end. The NanoClaw admin adds a `TOTALRECLAW_RECOVERY_PHRASE` secret for your group, and the agent-runner spawns `@totalreclaw/mcp-server` as a background process.

**What this means for you:**
- Your agent has 8 memory tools available: remember, recall, forget, export, import, import_from (Mem0/MCP Memory), status, upgrade
- Memory is shared within your NanoClaw group (same namespace = same memories)
- All data is encrypted with your group's recovery phrase before leaving the container
- Billing and quota work identically to Claude Desktop / Cursor

**To verify it's working**, ask your agent:

> "What's my TotalReclaw status?"

If TotalReclaw is configured, the agent will show your tier, usage, and storage mode.

**Recovery:** Your group's memory is tied to the `TOTALRECLAW_RECOVERY_PHRASE`. If the admin changes it, previous memories become inaccessible. The admin should store this phrase securely.

---

## How It Works

1. **You set up with a 12-word recovery phrase** (like a crypto wallet)
2. **All memories are encrypted on your device** before reaching our server
3. **The server only stores encrypted blobs** -- it can never read them
4. **The TotalReclaw managed service** facilitates on-chain storage, gas sponsorship, billing, and query routing — without ever seeing your data. Your encrypted memories are anchored on [Gnosis Chain](https://www.gnosis.io/) and indexed by [The Graph](https://thegraph.com). If you prefer full control, you can self-host the open-source server and store encrypted memories in your own PostgreSQL database instead.
5. **Same phrase on any device = same keys = same memories**

---

## What Gets Remembered

Your agent extracts and stores atomic facts from conversations:

| Type | Example |
|------|---------|
| **Preference** | "User prefers TypeScript over JavaScript" |
| **Decision** | "User decided to use PostgreSQL for the main database" |
| **Fact** | "User works at Acme Corp" |
| **Goal** | "User wants to launch by Q2" |

Each fact is scored by importance. Low-importance facts decay over time; critical ones persist.

---

## Recovery & Multi-Device

Your 12-word recovery phrase is the only thing you need to access your memories from a new device or agent. Same phrase = same keys = same data.

**Write it down and store it safely.** If you lose it, your memories are unrecoverable -- that's the point of end-to-end encryption.

To recover: just run the setup again (OpenClaw: reinstall the skill; MCP: re-run the setup wizard) and enter your existing phrase when prompted. NanoClaw users: ask your group admin — recovery is managed via the group's recovery phrase.

---

## Storage

By default, TotalReclaw uses its managed service at `api.totalreclaw.xyz`, which facilitates on-chain storage, gas sponsorship, billing, and query routing — without ever seeing your data. Encrypted facts are submitted on-chain (Gnosis Chain) via ERC-4337 and indexed by The Graph. Gas is sponsored — you pay nothing.

If you prefer full control, you can self-host the open-source server and store encrypted memories in your own PostgreSQL database instead.

| Option | How It Works | When to Use |
|--------|-------------|-------------|
| **TotalReclaw (managed)** | Encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph. Gas is sponsored. No single server controls your data. | Default — recommended for most users |
| **Self-hosted** | Encrypted facts are stored in your own server's PostgreSQL database. Faster writes, but you manage the infrastructure. | Set `TOTALRECLAW_SELF_HOSTED=true` and `TOTALRECLAW_SERVER_URL` to your server |

Both options encrypt your data identically on your device — the difference is where the encrypted blobs are stored.

---

## Free Tier & Upgrading

| Tier | Memories | Reads | Storage | Key Features | Price |
|------|----------|-------|---------|--------------|-------|
| **Free** | 500/month | Unlimited | Testnet (trial) | Cosine dedup, auto-extract every 3 turns | $0 |
| **Pro** | Unlimited | Unlimited | Permanent on-chain (Gnosis) | + LLM-guided dedup | $5/month |

- Counter resets at the start of each calendar month
- Pay with card (Stripe)
- When you hit the limit, your agent tells you and provides an upgrade link
- **Free tier** includes full encryption, cosine-based dedup, and auto-extraction every 3 turns
- **Pro tier** adds LLM-guided dedup (catches contradictions, not just paraphrases)

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Setup not working | Re-run: `openclaw plugins install @totalreclaw/totalreclaw` or `npx @totalreclaw/mcp-server setup` |
| "Not authenticated" / 401 | Check your recovery phrase -- exact words, exact order |
| Memories not appearing | Try an explicit recall: "what do you remember about X?" |
| Quota exceeded (403) | Wait for monthly reset or upgrade to Pro |
| Want to upgrade to Pro | Ask your agent "upgrade my TotalReclaw subscription" |

For detailed technical troubleshooting, see [beta-tester-guide-detailed.md](./beta-tester-guide-detailed.md).

---

## Importing from Other Tools

Switching from Mem0 or another AI memory system? TotalReclaw can import your existing memories. Ask your agent:

> "Import my memories from Mem0 using API key m0-your-key-here"

Everything is encrypted on your device before storage — the same E2EE guarantee as natively stored memories. Imports are idempotent (running twice won't create duplicates).

**Supported sources:** Mem0 (live API), MCP Memory Server (JSONL file). More planned.

For full details, supported sources, and troubleshooting, see the [Importing Memories guide](./importing-memories.md).

---

## Feature Overview

| Feature | OpenClaw | MCP (Claude Desktop, Cursor) | NanoClaw |
|---------|:---:|:---:|:---:|
| Auto-remember (every N turns) | Yes | -- | Yes |
| Auto-recall (every message) | Yes | -- | Yes |
| Explicit tools (remember, recall, forget, export) | Yes | Yes | Yes |
| Import from Mem0/MCP Memory | Yes | Yes | Yes |
| Status & billing | Yes | Yes | Yes |
| Upgrade to Pro | -- | Yes | Yes |
| Near-duplicate prevention (cosine) | Yes | Yes | Yes |
| LLM-guided dedup (contradictions) | Pro | -- | Yes |
| Memory consolidation tool | Yes | Yes | Yes |
| Pre-compaction memory flush | Yes | -- | Yes |

---

## Memory Dedup

TotalReclaw uses two complementary layers to prevent duplicate memories:

- **Cosine similarity** (all platforms) -- catches paraphrases and near-duplicates before storing. "User prefers dark mode" and "User likes dark themes" are recognized as the same fact.
- **LLM-guided classification** (OpenClaw + NanoClaw) -- catches contradictions that cosine misses. When you say "I switched to light mode", the LLM recognizes this contradicts the earlier "prefers dark mode" and updates it, even though the embeddings are dissimilar.

**Cosine catches paraphrases, LLM catches contradictions.** Together they cover the full spectrum. MCP agents (Claude Desktop, Cursor) rely on cosine only since they lack lifecycle hooks. For the full technical details, see [memory-dedup.md](./memory-dedup.md).

---

## Known Limitations (Beta)

- MCP agents rely on explicit tool use rather than automatic memory hooks (by design)
- Beta runs on Gnosis mainnet -- expect occasional downtime
- The managed service (on-chain storage) is the default. Set `TOTALRECLAW_SELF_HOSTED=true` and provide your own `TOTALRECLAW_SERVER_URL` for self-hosted mode

---

## Billing & Subscriptions

**Checking your subscription status:**
- **OpenClaw:** Ask your agent "What's my TotalReclaw subscription status?"
- **MCP agents:** The agent has a `status` tool that shows your tier, usage, and limits

**What happens at the quota limit:**
- Free tier: 500 memories/month, unlimited reads
- When you hit the limit, your agent will tell you and provide an upgrade link
- After upgrading via Stripe, writes resume immediately

**Monthly reset:** Usage counters reset at the start of each calendar month.

---

## Further Reading

- [Importing Memories](./importing-memories.md) -- migrate from Mem0, MCP Memory Server, and other tools
- [Detailed technical guide](./beta-tester-guide-detailed.md) -- full reference with architecture, configuration, and environment variables
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage

---

*TotalReclaw beta v1.0-beta -- [totalreclaw.xyz](https://totalreclaw.xyz)*
