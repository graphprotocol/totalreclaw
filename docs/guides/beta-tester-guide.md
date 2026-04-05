# TotalReclaw -- Private Memory for Your AI Agent

**What it is:** Encrypted memory that works across sessions. Your AI remembers your preferences, decisions, and context -- and only you can read it. Not even our server sees your data.

**Works with:** OpenClaw, Claude Desktop, Cursor, NanoClaw, or any MCP-compatible agent.

---

## TL;DR — OpenClaw Users

Two terminal commands, then chat:

```bash
openclaw skills install totalreclaw
openclaw plugins install ~/.openclaw/workspace/skills/totalreclaw
```

Then tell your agent: *"Set up TotalReclaw for me. Generate a new recovery phrase."*

That's it. Write down the 12 words, and memory is automatic from there.

---

## Quick Start

### OpenClaw (recommended -- fully automatic)

**Step 1: Install the plugin**

```bash
openclaw skills install totalreclaw
openclaw plugins install ~/.openclaw/workspace/skills/totalreclaw
```

Restart the gateway if prompted. You may see a "1 suspicious code pattern" warning -- this is normal (the plugin reads configuration and makes encrypted network calls).

**Step 2: Set up your recovery phrase**

Start a conversation and tell your agent:

> "Set up TotalReclaw for me. Generate a new recovery phrase."

The agent will generate a 12-word phrase and display it. **Write it down on paper and store it safely.** This phrase is the only key to your encrypted memories -- there is no password reset.

**Returning user?** If you already have a phrase from another device or agent:

> "I have an existing TotalReclaw recovery phrase: word1 word2 word3 ... word12"

The agent will import it and all your existing memories become accessible immediately.

The gateway restarts automatically after setup. Wait a few seconds, then continue chatting normally.

> **Note:** The first interaction downloads a ~34MB embedding model for local inference. This is cached locally and only happens once.

**After setup, memory is fully automatic:**
- Your agent extracts and stores important facts from your conversations (preferences, decisions, project context)
- In new sessions, ask *"What do you remember about me?"* to see your stored memories
- You can also be explicit: *"Remember that I prefer dark mode"* or *"What database did I choose?"*
- Everything is encrypted before leaving your machine -- the server never sees plaintext

**Verified to work:** Same recovery phrase on a different device retrieves all your memories. Your data is truly portable.

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
- Your agent has 14 memory tools available (remember, recall, forget, export, import, import_from, status, upgrade, migrate, consolidate, debrief, setup, support, account)
- Memory is shared within your NanoClaw group (same namespace = same memories)
- All data is encrypted with your group's recovery phrase before leaving the container
- Billing and quota work identically to Claude Desktop / Cursor

**To verify it's working**, ask your agent:

> "What's my TotalReclaw status?"

If TotalReclaw is configured, the agent will show your tier, usage, and storage mode.

**Recovery:** Your group's memory is tied to the `TOTALRECLAW_RECOVERY_PHRASE`. If the admin changes it, previous memories become inaccessible. The admin should store this phrase securely.

### Python Client (for custom integrations)

Install the Python client:

```bash
pip install totalreclaw
```

> **Docker users:** On slim images (e.g., `python:3.12-slim`), install a C compiler first for PyStemmer:
> ```bash
> apt-get update && apt-get install -y gcc g++
> ```

```python
from totalreclaw import TotalReclaw

async def main():
    client = TotalReclaw(
        recovery_phrase="your 12-word phrase",
        server_url="https://api.totalreclaw.xyz",
    )
    await client.resolve_address()
    await client.register()

    # Store a memory (importance is a float from 0.0 to 1.0)
    await client.remember("I prefer dark mode", importance=0.8)

    # Search memories
    results = await client.recall("What are my preferences?")
    for r in results:
        print(f"  [{r.rrf_score:.3f}] {r.text}")

    await client.close()
```

You must call `resolve_address()` and `register()` before any remember/recall/forget/export operations. The recovery phrase is the same one used across all TotalReclaw clients -- same phrase, same memories.

For the Hermes Agent plugin, see the [Hermes setup guide](hermes-setup.md).

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
| **Decision** | "User decided to use PostgreSQL for the main database because the data is relational" |
| **Fact** | "User works at Acme Corp" |
| **Episodic** | "We deployed v2.0 on March 15th" |
| **Goal** | "User wants to launch by Q2" |
| **Context** | "The project uses PostgreSQL" |
| **Summary** | "Today we discussed the migration plan" |

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
| **Free** | Unlimited | Unlimited | Testnet (Base Sepolia) | Cosine dedup, auto-extract every 3 turns | $0 |
| **Pro** | Unlimited | Unlimited | Permanent on-chain (Gnosis) | + LLM-guided dedup | $3.99/month |

- Pay with card (Stripe)
- **Free tier** uses Base Sepolia testnet -- unlimited memories, full encryption, cosine-based dedup, and auto-extraction every 3 turns. Testnet data may be reset.
- **Pro tier** stores permanently on Gnosis mainnet and adds LLM-guided dedup (catches contradictions, not just paraphrases)

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Setup not working | Re-run: `openclaw skills install totalreclaw` or `npx @totalreclaw/mcp-server setup` |
| "Not authenticated" / 401 | Check your recovery phrase -- exact words, exact order |
| Memories not appearing | Try an explicit recall: "what do you remember about X?" |
| Quota exceeded (403) | Upgrade to Pro for permanent mainnet storage |
| Want to upgrade to Pro | Ask your agent "upgrade my TotalReclaw subscription" |

For detailed technical troubleshooting, see [beta-tester-guide-detailed.md](./beta-tester-guide-detailed.md).

---

## Importing from Other Tools

Switching from Mem0 or another AI memory system? TotalReclaw can import your existing memories. Ask your agent:

> "Import my memories from Mem0 using API key m0-your-key-here"

Everything is encrypted on your device before storage — the same E2EE guarantee as natively stored memories. Imports are idempotent (running twice won't create duplicates).

**Supported sources:** Mem0 (live API), MCP Memory Server (JSONL file), ChatGPT (data export), Claude (data export). More planned.

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
- Free tier: unlimited memories on testnet (Base Sepolia), unlimited reads
- Pro tier: unlimited memories on Gnosis mainnet (permanent on-chain storage)
- After upgrading via Stripe, writes move to mainnet immediately

**Free tier** stores on Base Sepolia testnet (unlimited, but testnet data may be reset). **Pro tier** stores permanently on Gnosis mainnet.

---

## Further Reading

- [Importing Memories](./importing-memories.md) -- migrate from Mem0, MCP Memory Server, and other tools
- [Detailed technical guide](./beta-tester-guide-detailed.md) -- full reference with architecture, configuration, and environment variables
- [Monitoring Setup Guide](./monitoring-setup.md) -- production monitoring and alerting setup
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage

---

*TotalReclaw beta v1.0-beta -- [totalreclaw.xyz](https://totalreclaw.xyz)*
