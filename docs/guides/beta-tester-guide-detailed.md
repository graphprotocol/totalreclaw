# TotalReclaw Beta Tester Guide

**Version:** 1.1
**Date:** March 2026
**Audience:** Internal beta testers (OpenClaw, Claude Desktop, Cursor, or any MCP-compatible agent)
**Time to read:** ~15 minutes
**Time to complete setup:** ~10 minutes (MCP) / ~30 minutes (OpenClaw plugin)

---

## Table of Contents

1. [What Is TotalReclaw?](#1-what-is-totalreclaw)
2. [Installation](#2-installation)
3. [First-Time Setup -- Key Generation](#3-first-time-setup----key-generation)
4. [Understanding the Wallet Address](#4-understanding-the-wallet-address)
5. [Free Tier -- How It Works](#5-free-tier----how-it-works)
6. [Automatic Memory (No Action Required)](#6-automatic-memory-no-action-required)
7. [Explicit Memory Tools](#7-explicit-memory-tools)
8. [Testing Your Setup (Manual Validation)](#8-testing-your-setup-manual-validation)
9. [Upgrading to Pro Tier](#9-upgrading-to-pro-tier)
10. [Recovery on a New Device](#10-recovery-on-a-new-device)
11. [MCP Server Setup (Claude Desktop / Cursor)](#11-mcp-server-setup-claude-desktop--cursor)
12. [Running E2E Validation (Optional, for Technical Testers)](#12-running-e2e-validation-optional-for-technical-testers)
13. [Configuration Reference](#13-configuration-reference)
14. [Troubleshooting](#14-troubleshooting)
15. [Known Limitations (Beta)](#15-known-limitations-beta)

---

## 1. What Is TotalReclaw?

TotalReclaw is a private, encrypted memory layer for your AI agent. It remembers things you tell it across sessions -- your preferences, decisions, facts about your projects -- and only you can decrypt those memories. Not even the TotalReclaw server can read them. Think of it as a password manager, but for your AI's memory: fully encrypted, fully portable, and under your control.

---

## 2. Installation

TotalReclaw works with two types of AI agents: **OpenClaw** (via the plugin) and **any MCP-compatible agent** such as Claude Desktop or Cursor (via the MCP server). Choose the path that matches your setup.

### Prerequisites

Before you begin, make sure you have:

1. **Node.js 18 or later** installed on your machine. You can check by running:
   ```
   node --version
   ```
   If you see `v18.x.x` or higher, you are all set. If not, download Node.js from [nodejs.org](https://nodejs.org).
2. **Internet access** -- TotalReclaw communicates with the relay server at `api.totalreclaw.xyz`.
3. **A safe place to write down 12 words** -- you will generate a recovery phrase during setup. This is your only way to recover your memories if you switch devices. More on this in the next section.

**For OpenClaw users only:**

4. **An OpenClaw account** with an active workspace.
5. **An LLM provider configured in OpenClaw.** TotalReclaw auto-detects your agent's LLM provider and API key -- no extra LLM configuration is needed. Embeddings are generated locally (no API key required). Fact extraction uses a cheap model from your existing provider (e.g., Anthropic -> `claude-haiku-4-5`, OpenAI -> `gpt-4.1-mini`).

**For MCP users (Claude Desktop / Cursor):** Skip to [Section 11](#11-mcp-server-setup-claude-desktop--cursor) for the fastest setup path.

### Install the OpenClaw Plugin

1. Open your OpenClaw workspace.
2. Navigate to **Settings > Plugins** (or **Skill Marketplace** if available).
3. Search for **TotalReclaw** and click **Install**.

If TotalReclaw is not yet listed in the marketplace, install it manually:

1. Open a terminal and navigate to your OpenClaw plugins directory:
   ```
   openclaw plugins install @totalreclaw/totalreclaw
   ```
   Or ask your agent: *"Install the totalreclaw plugin"*

2. The plugin registers itself with the ID `totalreclaw` and sets up automatically on first run.

3. Verify the plugin appears in your OpenClaw plugin list. You should see **TotalReclaw** described as "Zero-knowledge encrypted memory vault for AI agents."

---

## 3. First-Time Setup -- Key Generation

TotalReclaw uses a 12-word recovery phrase (called a BIP-39 mnemonic) as your master key. This phrase is used to derive your encryption key and your identity. You must generate it before using TotalReclaw for the first time.

> **WARNING: Your recovery phrase is the ONLY way to access your memories.**
> If you lose it, your memories are permanently unrecoverable.
> There is no reset, no recovery email, no support ticket.
> Write it down on paper and store it somewhere safe.

### Step 1: Generate your recovery phrase

**MCP users (recommended path):** Run the setup wizard, which generates a phrase, registers you, and prints the MCP config snippet all in one step:

```
npx @totalreclaw/mcp-server setup
```

The wizard will ask if you already have a recovery phrase. If you are a new user, it generates one, displays it, and asks you to confirm you have saved it before proceeding. It then registers you with the relay server and saves your credentials to `~/.totalreclaw/credentials.json`. See [Section 11](#11-mcp-server-setup-claude-desktop--cursor) for the full MCP setup flow.

**OpenClaw plugin users:** Open a terminal and run:

```
npx @totalreclaw/totalreclaw generate-mnemonic
```

You will see output like this:

```
  Your TotalReclaw master mnemonic (12 words):

  apple banana cherry dolphin eagle falcon grape honey iris jungle kite lemon

  WRITE THIS DOWN. If you lose it, your memories are unrecoverable.
  Set it as TOTALRECLAW_MASTER_PASSWORD in your environment.
```

The 12 words shown above are an example. Your actual phrase will be different and unique to you.

### Step 2: Write it down

Write all 12 words on paper, in the exact order shown. Double-check every word. Store the paper somewhere safe -- a locked drawer, a safe, or wherever you keep important documents.

> **Do not store your recovery phrase in a text file on your computer, in a screenshot,
> or in a cloud notes app.** If someone gains access to these 12 words, they can
> decrypt all your memories.

### Step 3: Set your environment variables (OpenClaw plugin)

> **MCP users:** If you used `npx @totalreclaw/mcp-server setup`, your credentials are already saved and the setup wizard printed the config snippet you need. Skip to [Section 11](#11-mcp-server-setup-claude-desktop--cursor).

Set the following environment variables in your OpenClaw configuration (e.g., workspace settings or shell environment):

```
# --- Required ---
TOTALRECLAW_MASTER_PASSWORD="word1 word2 word3 word4 word5 word6 word7 word8 word9 word10 word11 word12"
TOTALRECLAW_SERVER_URL="https://api.totalreclaw.xyz"

# --- Subgraph mode (on-chain storage, enabled by default) ---
TOTALRECLAW_SUBGRAPH_MODE="true"
TOTALRECLAW_CHAIN_ID="10200"
```

Replace `word1 word2 ...` with your actual 12-word phrase. Keep the quotes around it. These can be set in your OpenClaw workspace environment settings, your shell profile, or any method your OpenClaw instance supports for environment variables.

The values above are all you need for the Chiado testnet. Subgraph endpoint and bundler/paymaster access are handled automatically by the relay server.

> **Note:** `TOTALRECLAW_SERVER_URL` is always required -- it handles user registration and billing, even in subgraph mode.

Your agent's LLM API key (e.g., `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) should already be configured in your OpenClaw environment. TotalReclaw auto-detects it -- no extra LLM setup is needed.

### Step 4: Understand what happens behind the scenes

Your 12-word phrase is the single root of all your keys and identity. The derivation chain works as follows:

1. **BIP-39 seed** -- Your 12-word mnemonic produces a 512-bit master seed.
2. **Private key** -- BIP-32 hierarchical derivation at path `m/44'/60'/0'/0/0` (the standard Ethereum path) produces a 256-bit private key.
3. **EOA address** -- The standard Ethereum address derived from the private key. This is the "owner" of your Smart Account.
4. **Encryption key** -- HKDF-SHA256 with info string `totalreclaw-encryption-key-v1` derives your AES-256-GCM encryption key.
5. **Auth key** -- HKDF-SHA256 with info string `totalreclaw-auth-key-v1` derives an authentication key. Its SHA-256 hash is registered with the server as your identity.
6. **Smart Account address** -- A deterministic ERC-4337 Smart Account address computed via CREATE2 from your EOA address. This is your on-chain identity.

You never need to manage crypto or interact with any blockchain directly -- it all happens automatically. The same mnemonic always produces the same keys and the same wallet address, on any device. This is how cross-device recovery works (validated by the E2E integration test suite in Journey 3).

---

## 4. Understanding the Wallet Address

Your 12-word recovery phrase derives an Ethereum-compatible wallet address in two steps:

1. **EOA (Externally Owned Account)** -- Derived from the BIP-44 path `m/44'/60'/0'/0/0`. This is the same method used by popular wallets like MetaMask.
2. **Smart Account** -- A deterministic ERC-4337 Smart Account address computed from the EOA via CREATE2 (using the canonical SimpleAccountFactory v0.7). This is your actual on-chain identity.

Here is what you need to know:

- **Your Smart Account address is your identity** on Gnosis Chain, the blockchain where TotalReclaw stores encrypted data. It is also the key used for billing and subscription tracking.
- **You never need to fund it.** The TotalReclaw paymaster (Pimlico) sponsors all transaction fees (gas) on your behalf during the beta.
- **You never need to send transactions or interact with the blockchain.** The plugin and MCP server handle everything automatically.
- **The same phrase always produces the same wallet address and the same encryption key.** This is how recovery works -- enter your phrase on any device, and you get the same identity and can decrypt all your memories. This property is validated by the E2E integration tests (Journey 3: same mnemonic on "Device B" derives identical auth and encryption keys).

If you want to view your derived wallet address, check the plugin logs after the first run. The plugin logs a message like `Registered new user: <user-id>` during initialization. Your credentials (user ID and salt -- not the mnemonic itself) are stored in the credentials file (see [Configuration Reference](#13-configuration-reference) for its location).

---

## 5. Free Tier -- How It Works

TotalReclaw includes a free tier so you can start using it immediately:

- **Your first 100 memory writes per month are free** (beta; this limit may be adjusted).
- **Reading and searching your memories is always free and never metered.** You can always access your own data, even after exhausting the write quota.
- All transaction fees (gas) are covered by the TotalReclaw paymaster (Pimlico) on Gnosis Chain. You pay nothing.
- No credit card, no crypto, and no signup beyond your recovery phrase are required.
- The relay server tracks your write usage by wallet address. The counter resets automatically at the start of each calendar month.

When you approach the free tier limit, the agent will proactively tell you. For example:

> "You've used 90/100 free writes this month. Consider upgrading to Pro for unlimited storage."

If you reach the limit, new memory writes will be rejected with a `quota_exceeded` error, but you can still search and recall all your existing memories. You can either wait for the monthly reset or upgrade to Pro (see [Upgrading to Pro Tier](#9-upgrading-to-pro-tier)).

> **Note:** If you upgrade to Pro and later cancel, your write counter is preserved from before the upgrade. For example, if you used 100 free writes, upgraded, wrote more, and then cancelled, the next write after cancellation would still be blocked until the monthly reset.

---

## 6. Automatic Memory (No Action Required)

Once TotalReclaw is installed and configured, it works in the background without any action from you. Two automatic behaviors are always running via OpenClaw lifecycle hooks.

> **Note:** Automatic behaviors (auto-search and auto-store) are OpenClaw plugin features. The MCP server does not have lifecycle hooks -- it responds to explicit tool calls only. See [Section 11](#11-mcp-server-setup-claude-desktop--cursor) for MCP-specific behavior.

### Auto-Search (every message)

Before every agent turn (via the `before_agent_start` hook), if your message is at least 5 characters long, the plugin automatically searches your stored memories for anything relevant. If it finds matching memories, it silently injects them into the agent's context so the agent can reference them naturally.

You do not need to ask the agent to recall anything -- it happens automatically. The agent will simply "know" relevant things from past conversations.

### Auto-Store (every N turns)

On the `agent_end` hook (which fires after each conversation turn), the plugin checks whether enough turns have elapsed since the last extraction. By default, it extracts every 5 turns. During extraction, it scores each fact by importance (1-10 scale), and facts that meet the minimum importance threshold (default: 3) are encrypted and stored.

This means you do not need to explicitly tell the agent to remember things. Preferences, decisions, and notable facts are captured automatically as you chat.

The number of turns between automatic extractions is configurable via the `TOTALRECLAW_EXTRACT_EVERY_TURNS` environment variable (default: `5`).

### Pre-Compaction Flush

When OpenClaw triggers a context compaction (because your conversation is getting long), TotalReclaw performs a full memory flush beforehand. This ensures no important information is lost when older messages are compressed or removed from context. This happens automatically with no action from you.

### Pre-Reset Flush

Similarly, if a session is reset or cleared, the plugin extracts and stores any important facts from the conversation before the reset occurs.

---

## 7. Explicit Memory Tools

In addition to automatic behaviors, TotalReclaw provides seven tools you can use by asking the agent directly. The OpenClaw plugin exposes the first four (remember, recall, forget, export). The MCP server exposes all seven, adding import, status, and upgrade.

### 7.1 Remember -- `totalreclaw_remember`

Use this when you want to explicitly store something.

**Example prompts:**
- "Remember that I prefer dark mode in all my apps."
- "Store this: my database password rotation schedule is every 90 days."
- "Remember that the project deadline is March 15th."

**What happens behind the scenes:**
1. The plugin extracts the fact from your message.
2. It encrypts the fact with your key (AES-256-GCM).
3. It generates blind search indices (hashed keywords) so the server can find it later without seeing the plaintext.
4. It generates a content fingerprint to prevent duplicates.
5. It stores the encrypted blob on the server.

**Expected agent response:**
> "Memory stored (ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890)"

### 7.2 Recall -- `totalreclaw_recall`

Use this when you want to search your stored memories.

**Example prompts:**
- "What do you remember about my coding preferences?"
- "Recall anything related to the TotalReclaw project."
- "What databases have I mentioned preferring?"

**What happens behind the scenes:**
1. The plugin encrypts your query into blind search trapdoors.
2. The server returns matching encrypted candidates (up to several thousand).
3. The plugin decrypts all candidates on your device.
4. It re-ranks them using BM25 (text matching) + cosine similarity (semantic matching) + RRF fusion (combining scores).
5. The top results (default: 8) are returned.

**Expected agent response:**
> 1. User prefers PostgreSQL for databases (importance: 8/10) -- 2h ago [ID: ...]
> 2. User prefers TypeScript over JavaScript (importance: 7/10) -- 1d ago [ID: ...]

### 7.3 Forget -- `totalreclaw_forget`

Use this when you want to delete a specific memory.

**Example prompts:**
- "Forget that I said I like Java."
- "Delete the memory about my old email address."

**What happens behind the scenes:**
The agent first searches for matching memories using `totalreclaw_recall`, identifies the relevant one by ID, and then calls `totalreclaw_forget` with that ID. The memory is soft-deleted (marked as a tombstone with importance set to 0).

**Expected agent response:**
> "Memory a1b2c3d4-e5f6-7890-abcd-ef1234567890 deleted"

### 7.4 Export -- `totalreclaw_export`

Use this to get a copy of all your stored memories.

**Example prompts:**
- "Export all my memories as JSON."
- "Export my memories in markdown format."

**What happens behind the scenes:**
1. The plugin downloads all your encrypted memory blobs from the server.
2. It decrypts every memory on your device.
3. It formats them as JSON or Markdown (your choice).

**Expected agent response (JSON format):**
```json
[
  {
    "id": "a1b2c3d4-...",
    "text": "User prefers PostgreSQL for databases",
    "metadata": {
      "type": "preference",
      "importance": 0.8,
      "source": "explicit",
      "created_at": "2026-03-04T10:30:00.000Z"
    },
    "created_at": "2026-03-04T10:30:00.000Z"
  }
]
```

**Expected agent response (Markdown format):**
```
# Exported Memories (3)

1. **[preference]** User prefers PostgreSQL for databases (importance: 8/10)
   _ID: a1b2c3d4-... | Created: 2026-03-04T10:30:00.000Z_
2. **[fact]** User prefers TypeScript over JavaScript (importance: 7/10)
   _ID: b2c3d4e5-... | Created: 2026-03-04T10:25:00.000Z_
```

### 7.5 Import -- `totalreclaw_import_from`

Use this to import memories from external AI memory systems. Currently supports **Mem0** and **MCP Memory Server**. Available in both OpenClaw plugin and MCP server.

**Importing from Mem0:**

If you have been using Mem0 (mem0.ai) and want to migrate your memories to TotalReclaw, ask your agent:

- "Import my memories from Mem0" (if your Mem0 API key is configured)
- "Import my Mem0 memories using API key m0-abc123..."
- "Import memories from Mem0 for user my-user-id"

The agent will call `totalreclaw_import_from` with `source="mem0"`. You need to provide:
1. Your **Mem0 API key** (from [app.mem0.ai](https://app.mem0.ai) → Settings → API Keys)
2. Optionally, your **Mem0 user ID** (defaults to `"user"`)

**What happens behind the scenes:**
1. The tool fetches all your memories from the Mem0 API.
2. Each memory is encrypted with your TotalReclaw key (AES-256-GCM) on your device.
3. Blind search indices and content fingerprints are generated.
4. Encrypted memories are stored in TotalReclaw.
5. Duplicate memories (matching content fingerprints) are skipped if you run the import again.

**Expected agent response:**
> "Successfully imported 42 memories from Mem0. All 42 memories were imported with no skipped entries."

**Importing from MCP Memory Server:**

If you have been using `@modelcontextprotocol/server-memory`, you can import your JSONL knowledge graph:

- "Import my memories from MCP Memory Server"
- "Import memories from file /path/to/memory.jsonl"

**Notes:**
- All processing happens client-side — Mem0 data is fetched, decrypted/parsed locally, re-encrypted with your TotalReclaw key, then stored. The TotalReclaw server never sees plaintext.
- The import is idempotent — running it multiple times will not create duplicates (content fingerprint dedup).

### 7.6 Status -- `totalreclaw_status` (MCP only)

Use this to check your subscription status and usage.

**Example prompts:**
- "What's my TotalReclaw subscription status?"
- "How many free memories do I have left?"

**Expected agent response:**
> "You're on the free tier. You've used 42/100 free writes this month."

### 7.7 Upgrade -- `totalreclaw_upgrade` (MCP only)

Use this to get a payment link for upgrading to Pro.

**Example prompts:**
- "I'd like to upgrade TotalReclaw."
- "Get me a payment link for TotalReclaw Pro."

**What happens behind the scenes:**
1. The MCP server requests a checkout session from the relay server (Stripe or Coinbase Commerce).
2. It returns a payment URL that you open in your browser.
3. After payment, the webhook activates your subscription within 60 seconds.

**Expected agent response:**
> "Here's your upgrade link: https://checkout.stripe.com/... Open it in your browser to complete payment."

---

## 8. Testing Your Setup (Manual Validation)

Follow this checklist step by step to verify that TotalReclaw is working correctly. Each step has a clear expected outcome. If any step fails, see [Troubleshooting](#13-troubleshooting).

### Checklist

**Step 1: Store a fact via auto-extraction.**

1. Start a new conversation with your agent.
2. Say: "I always use PostgreSQL for databases and prefer TypeScript over JavaScript."
3. Continue chatting for at least 5 more turns about unrelated topics (this triggers auto-extraction).
4. **Expected:** The plugin silently extracts and stores your database and language preferences. You will not see a confirmation, but the facts are stored in the background.

**Step 2: Verify cross-session memory.**

5. Start a **new conversation** (to confirm memories persist across sessions).
6. Ask: "What databases do I prefer?"
7. **Expected:** The agent mentions PostgreSQL. It may phrase this naturally (e.g., "Based on what I know about you, you prefer PostgreSQL for databases.") or reference the memory explicitly.

**Step 3: Explicitly store a memory.**

8. Say: "Remember that my favorite color is blue."
9. **Expected:** The agent responds with a confirmation, such as: "Memory stored (ID: ...)"

**Step 4: Explicitly recall a memory.**

10. In another message, ask: "What is my favorite color?"
11. **Expected:** The agent responds with "blue."

**Step 5: Forget a memory.**

12. Say: "Forget that my favorite color is blue."
13. **Expected:** The agent searches for the memory, finds it, and confirms deletion: "Memory ... deleted."

**Step 6: Verify the memory is gone.**

14. Ask: "What is my favorite color?"
15. **Expected:** The agent does not mention blue, or says it does not know.

**Step 7: Export your memories.**

16. Say: "Export all my memories as JSON."
17. **Expected:** The agent returns a JSON array containing your stored facts (your database preference and any other facts extracted during the conversation). The favorite-color memory should not appear (it was deleted in Step 5).

### Validation Summary

| Step | Action | Expected Result | Pass? |
|------|--------|----------------|-------|
| 1-4 | Auto-store + auto-recall | Agent remembers PostgreSQL preference in new session | |
| 8-9 | Explicit remember | Confirmation with memory ID | |
| 10-11 | Explicit recall | Agent knows favorite color is blue | |
| 12-13 | Explicit forget | Confirmation of deletion | |
| 14-15 | Verify forget | Agent no longer knows favorite color | |
| 16-17 | Export | JSON with stored facts, no deleted memory | |

---

## 9. Upgrading to Pro Tier

When your free tier write limit (100 writes/month, beta default) is reached, the agent will prompt you to upgrade.

**What you will see:**
> "You've used 100/100 free writes this month. Upgrade to Pro for unlimited storage."

> **Note:** The Pro tier price ($2-5/month) is beta pricing and not yet finalized.

### Two payment options:

**Option A: Credit card**

1. Tell the agent: "I'd like to upgrade with a credit card."
2. The agent generates a Stripe Checkout URL and shares it with you.
3. Click the URL. It opens in your browser.
4. Complete the payment (card, Apple Pay, or Google Pay are all accepted).
5. After payment, Stripe sends a webhook to the relay server, which activates your subscription.
6. The agent confirms: "You're all set on the Pro tier."

**Option B: Cryptocurrency**

1. Tell the agent: "I'd like to upgrade with crypto."
2. The agent generates a Coinbase Commerce payment URL and shares it.
3. Click the URL. It opens in your browser.
4. Pay with USDC, USDT, or ETH from any supported network: Base, Ethereum, Solana, Polygon, or Arbitrum. No bridging is required.
5. After the payment is confirmed, Coinbase sends a webhook to the relay server, which activates your subscription.
6. The agent confirms: "You're all set on the Pro tier."

After upgrading, you can continue using TotalReclaw with higher write limits. Your subscription is tied to your wallet address, so it follows you across devices (as long as you use the same recovery phrase).

---

## 10. Recovery on a New Device

If you switch to a new computer or install TotalReclaw on a second agent, you can restore all your memories using your 12-word recovery phrase. This works because the same mnemonic always derives the same encryption key and the same wallet address -- your data is not tied to a specific device.

### OpenClaw Plugin Recovery

1. Install the TotalReclaw plugin on the new device (follow [Section 2](#2-installation)).
2. Set the same environment variables as in [Step 3](#step-3-set-your-environment-variables-openclaw-plugin) -- use the same 12-word phrase and the same server URL.
3. Start a conversation with your agent.
4. The plugin derives the same Smart Account address and encryption key from your phrase.
5. All your memories are automatically retrieved from the subgraph and decrypted on your device.
6. If you have an active Pro subscription, it is recognized automatically (same wallet address).

### MCP Server Recovery

1. Run the setup wizard with your existing phrase:
   ```
   npx @totalreclaw/mcp-server setup
   ```
2. When asked "Do you have an existing seed phrase?", answer **yes** and enter your 12 words.
3. The wizard re-derives the same wallet address, saves credentials, and prints the MCP config snippet.
4. Add the config snippet to your MCP client. All your existing memories are accessible immediately.

### Recovery Troubleshooting

> **If recovery does not work,** double-check every word in your recovery phrase and its
> exact order. BIP-39 phrases are all lowercase. Even one wrong word
> derives a completely different encryption key and wallet address, meaning the server
> will treat you as a different user with no memories. The E2E integration tests
> (Journey 3) validate this property: a wrong mnemonic produces different keys, and
> the server rejects authentication with a 401 or 403 error.

---

## 11. MCP Server Setup (Claude Desktop / Cursor)

If you use Claude Desktop, Cursor, VS Code, or any other MCP-compatible AI agent, TotalReclaw is available as a standalone MCP server. This is the fastest way to get started -- no OpenClaw required.

### Step 1: Run the setup wizard

Open a terminal and run:

```
npx @totalreclaw/mcp-server setup
```

The wizard walks you through:

1. **Seed phrase** -- Generate a new 12-word phrase or import an existing one.
2. **Key derivation** -- Derives your auth key, encryption key, and wallet address.
3. **Server registration** -- Registers you with the relay server at `https://api.totalreclaw.xyz` (free tier, no payment required).
4. **Credential storage** -- Saves your user ID and salt to `~/.totalreclaw/credentials.json` (the mnemonic itself is NOT stored on disk).
5. **Config snippet** -- Prints the JSON snippet you need to add to your MCP client config.

### Step 2: Configure your MCP client

Copy the config snippet printed by the setup wizard into your MCP client configuration file.

**Claude Desktop:** Edit `claude_desktop_config.json` (usually at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_MASTER_PASSWORD": "your twelve words here",
        "TOTALRECLAW_SERVER_URL": "https://api.totalreclaw.xyz"
      }
    }
  }
}
```

**Cursor:** Add the same config block to your Cursor MCP settings.

> **Security note:** The mnemonic is stored in your MCP client config file. Ensure the file has restricted permissions (mode 600, owner read/write only). Do not use TotalReclaw on shared machines without full disk encryption.

### Step 3: Start using it

Restart your MCP client (Claude Desktop, Cursor, etc.). The MCP server starts automatically, loads your credentials, and derives your keys. You can immediately use the seven tools:

- **totalreclaw_remember** -- Store a memory
- **totalreclaw_recall** -- Search your memories
- **totalreclaw_forget** -- Delete a memory
- **totalreclaw_export** -- Export all memories (JSON or Markdown)
- **totalreclaw_import** -- Import memories from a previous export
- **totalreclaw_status** -- Check subscription status and usage
- **totalreclaw_upgrade** -- Get a payment link to upgrade

### MCP vs OpenClaw: Key Differences

| Feature | OpenClaw Plugin | MCP Server |
|---------|----------------|------------|
| Auto-search (every message) | Yes (via `before_agent_start` hook) | No -- use `totalreclaw_recall` explicitly |
| Auto-store (every N turns) | Yes (via `agent_end` hook) | No -- use `totalreclaw_remember` explicitly |
| Pre-compaction flush | Yes | No |
| Billing tools (status, upgrade) | Via agent orchestration | Via `totalreclaw_status` and `totalreclaw_upgrade` tools |
| Import from Mem0/MCP Memory | Yes (`totalreclaw_import_from`) | Yes (`totalreclaw_import_from`) |
| Import from JSON/Markdown export | No | Yes (`totalreclaw_import`) |
| Setup method | Environment variables in OpenClaw config | `npx @totalreclaw/mcp-server setup` wizard |
| LLM for fact extraction | Auto-detected from agent's provider | Handled by host agent (Claude, etc.) |

### Alternative: Environment Variable Only (No Setup Wizard)

If you prefer not to run the setup command, you can set the mnemonic directly as an environment variable in your MCP client config. The MCP server will auto-register on first run if no `~/.totalreclaw/credentials.json` exists:

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_MASTER_PASSWORD": "your twelve words here",
        "TOTALRECLAW_SERVER_URL": "https://api.totalreclaw.xyz"
      }
    }
  }
}
```

---

## 12. Running E2E Validation (Optional, for Technical Testers)

TotalReclaw has two test suites: **functional tests** (plugin-level, mock infrastructure) and **integration tests** (server-level, real PostgreSQL). This section requires familiarity with the command line and Node.js.

### Functional Test Suite (Plugin)

If you want to run the automated end-to-end test suite to verify the plugin against a mock server:

1. Clone the test repository and install dependencies:
   ```
   git clone https://github.com/p-diogo/totalreclaw-plugin.git
   cd totalreclaw-plugin/tests/e2e-functional
   npm install
   ```

2. Run the full test suite:
   ```
   npx tsx run-all.ts --scenarios=A,B,C,D,E,F,G,H --instances=server-improved
   ```

3. To run a subset of scenarios:
   ```
   npx tsx run-all.ts --scenarios=A,B --instances=server-improved
   ```

#### What each scenario tests

| Scenario | Name | What it validates |
|----------|------|-------------------|
| A | Preferences | Storing and recalling user preferences (e.g., "I prefer dark mode") |
| B | Technical Facts | Technical knowledge recall (e.g., "I use PostgreSQL") |
| C | Noise Filtering | Low-importance facts are filtered out and not stored |
| D | Topic Switching | Correct recall after switching between multiple conversation topics |
| E | Long Conversations | Memory extraction and recall in extended conversations (many turns) |
| F | Subgraph Mode | End-to-end flow using the on-chain subgraph path (mock) |
| G | Pagination | Correct behavior when the memory vault has many entries (pagination) |
| H | Freeform | Open-ended conversational memory with diverse fact types |

#### Expected result

All scenarios should show **PASS**. The test runner outputs a summary table and writes a detailed JSON report to `tests/e2e-functional/e2e-results/`.

#### Available test instances

You can test against multiple configurations by specifying different instances:

| Instance | Description |
|----------|-------------|
| `server-improved` | Server mode with all retrieval improvements enabled |
| `server-baseline` | Server mode with baseline configuration |
| `server-recency` | Server mode with recency-weighted ranking |
| `subgraph-improved` | Subgraph mode with all improvements enabled |
| `subgraph-baseline` | Subgraph mode with baseline configuration |

Example running all instances:
```
npx tsx run-all.ts --scenarios=A,B,C,D,E,F,G,H --instances=server-improved,server-baseline,server-recency,subgraph-improved,subgraph-baseline
```

### Integration Test Suite (Server API)

The integration tests run against a real server with PostgreSQL and validate the full API surface including billing, authentication, relay proxy, and cross-device recovery.

1. Start the test infrastructure (requires Docker):
   ```
   cd totalreclaw-plugin/tests/e2e-integration
   npm install
   docker compose up -d  # Starts PostgreSQL and mock services
   ```

2. Run all journeys:
   ```
   npx tsx run-integration-tests.ts
   ```

#### What each journey tests

| Journey | Name | Assertions | What it validates |
|---------|------|:---:|-------------------|
| 1 | Core Memory Operations | 12 | Register, store, search, decrypt, export, delete round-trip |
| 2 | Deduplication | -- | Content fingerprint prevents duplicate storage |
| 3 | Wallet & Seed Derivation | 10 | BIP-39 mnemonic generation, HKDF key derivation, cross-device recovery (same mnemonic = same keys), wrong mnemonic rejection |
| 4 | Free Tier Quota | 10 | Write quota enforcement (N writes then 403), read operations bypass quota, billing status reporting |
| 5 | Stripe Upgrade | 10 | Free -> exhaust -> Stripe webhook -> pro activation -> cancel -> revert to free, write counter persistence |
| 6 | Coinbase Commerce | 10 | Coinbase upgrade, webhook idempotency, charge:failed handling, monthly counter reset |
| 7 | Security | 10 | Auth enforcement (no token, bad token, unregistered token), webhook signature validation, cross-user isolation, SQL injection handling |
| 8 | Full Relay Pipeline | 7 | Bundler proxy, subgraph proxy, mock request forwarding, error propagation |

---

## 13. Configuration Reference

All configuration is done through environment variables, set in your OpenClaw workspace environment settings or in the MCP client config.

### Required Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_MASTER_PASSWORD` | Your 12-word BIP-39 recovery phrase. **Required.** | -- (none) |
| `TOTALRECLAW_SERVER_URL` | URL of the TotalReclaw relay server. **Required.** | `https://api.totalreclaw.xyz` |

### LLM Provider Keys (OpenClaw plugin only, auto-detected from your agent)

The OpenClaw plugin auto-detects your agent's LLM provider and API key for fact extraction. No extra configuration is needed -- the key your agent already uses for its own LLM calls is reused. The MCP server does not need an LLM key because the host agent (Claude, etc.) handles fact extraction directly. The table below shows which providers are supported by the OpenClaw plugin.

| Variable | Provider |
|----------|----------|
| `OPENAI_API_KEY` | OpenAI (GPT-4.1-mini for extraction) |
| `ANTHROPIC_API_KEY` | Anthropic (Claude Haiku for extraction) |
| `ZAI_API_KEY` | Z.AI (GLM-4.5-flash for extraction) |
| `GEMINI_API_KEY` | Google Gemini (Gemini 2.0 Flash for extraction) |
| `MISTRAL_API_KEY` | Mistral (Mistral Small for extraction) |
| `GROQ_API_KEY` | Groq (LLaMA 3.3 70B for extraction) |
| `DEEPSEEK_API_KEY` | DeepSeek (DeepSeek Chat for extraction) |
| `OPENROUTER_API_KEY` | OpenRouter (routes to Claude Haiku for extraction) |
| `XAI_API_KEY` | xAI (Grok-2 for extraction) |
| `TOGETHER_API_KEY` | Together AI |
| `CEREBRAS_API_KEY` | Cerebras |

### Optional Variables (Tuning)

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_CREDENTIALS_PATH` | Path to the credentials file (stores user ID and salt). The MCP setup wizard saves to `~/.totalreclaw/credentials.json` by default. | `~/.totalreclaw/credentials.json` |
| `TOTALRECLAW_COSINE_THRESHOLD` | Minimum cosine similarity of the top result required to return memories. Lower = more permissive. | `0.15` |
| `TOTALRECLAW_MIN_IMPORTANCE` | Minimum importance score (1-10) for auto-extracted facts. Facts below this are silently dropped. | `3` |
| `TOTALRECLAW_EXTRACT_EVERY_TURNS` | Number of conversation turns between automatic extractions. | `5` |
| `TOTALRECLAW_RELEVANCE_THRESHOLD` | Minimum cosine relevance score for auto-injecting memories into context. | `0.3` |
| `TOTALRECLAW_SEMANTIC_SKIP_THRESHOLD` | Cosine similarity threshold for deduplication. Facts too similar to existing ones are skipped. | `0.85` |
| `TOTALRECLAW_CACHE_TTL_MS` | Hot cache time-to-live in milliseconds. Cached results within this window are reused for similar queries. | `300000` (5 minutes) |
| `TOTALRECLAW_LLM_MODEL` | **Advanced.** Override the auto-detected extraction model. TotalReclaw automatically derives a cheap model from your agent's provider (e.g., Anthropic → `claude-haiku-4-5`, OpenAI → `gpt-4.1-mini`). Only set this if the auto-derived model doesn't work for you. | Auto-detected |

### Subgraph Mode Variables (Advanced)

These variables control on-chain storage via the subgraph. **For the Chiado beta, subgraph mode is the default.**

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_SUBGRAPH_MODE` | Set to `true` to use on-chain subgraph storage. **Set this to `true` for beta.** | `false` |
| `TOTALRECLAW_CHAIN_ID` | Chain ID for on-chain transactions. `10200` = Chiado testnet, `100` = Gnosis mainnet. | `10200` |
| `TOTALRECLAW_DATA_EDGE_ADDRESS` | Address of the EventfulDataEdge smart contract on Chiado. | `0xA84c5433110Ccc93e57ec387e630E86Bad86c36f` |
| `TOTALRECLAW_ENTRYPOINT_ADDRESS` | ERC-4337 EntryPoint v0.7 address. Same on all chains. | `0x0000000071727De22E5E9d8BAf0edAc6f37da032` |
| `TOTALRECLAW_SUBGRAPH_PAGE_SIZE` | Maximum results per subgraph query page (Graph Studio limit: 1000). | `1000` |
| `TOTALRECLAW_TRAPDOOR_BATCH_SIZE` | Number of trapdoors per batch in subgraph queries. | `5` |

---

## 14. Troubleshooting

### "TOTALRECLAW_MASTER_PASSWORD not set"

**Cause:** The `TOTALRECLAW_MASTER_PASSWORD` environment variable is missing or empty.

**Fix (OpenClaw plugin):** Make sure the `TOTALRECLAW_MASTER_PASSWORD` environment variable is set in your OpenClaw workspace environment settings:
```
TOTALRECLAW_MASTER_PASSWORD="your twelve words here"
```
Restart the plugin after updating the variable.

**Fix (MCP server):** Make sure the `TOTALRECLAW_MASTER_PASSWORD` env var is set in your MCP client config (e.g., `claude_desktop_config.json`). Alternatively, run `npx @totalreclaw/mcp-server setup` to generate credentials.

### "TotalReclaw is not set up" (MCP only)

**Cause:** The MCP server started without a seed phrase and without a `credentials.json` file.

**Fix:** Run `npx @totalreclaw/mcp-server setup` in your terminal. This generates or imports your seed phrase, registers with the server, and saves your credentials.

### Memories not appearing in new conversations

**Cause:** The plugin may not be connecting to the correct server, or the auto-search hook may not be triggering.

**Fix:**
1. Verify that `TOTALRECLAW_SERVER_URL` is set to `https://api.totalreclaw.xyz`.
2. Try an explicit recall: "What do you remember about me?" If this returns results, auto-search is working but may have filtered your query as irrelevant (messages shorter than 5 characters are skipped).
3. Check the plugin logs for error messages (look for lines starting with `TotalReclaw:`).

### "Free tier quota exceeded"

**Cause:** You have used all 100 free writes for the month. (Note: reads are never metered.)

**Fix:** Either upgrade to Pro (see [Section 9](#9-upgrading-to-pro-tier)) or wait for the monthly reset. You can still search and recall your existing memories while on the free tier -- only new writes are blocked.

### Slow retrieval

**Cause:** Network latency to the relay server, or a large number of stored memories.

**Fix:**
1. Check your internet connection.
2. The hot cache (5-minute TTL) speeds up repeated queries. If you query the same topic twice within 5 minutes, the second query is near-instant.
3. If you have thousands of memories, retrieval may take a few seconds for the initial query each session. This is normal -- the plugin downloads and decrypts candidates client-side.

### Wrong memories returned or low relevance

**Cause:** The search and reranking thresholds may not match your use case.

**Fix:** Try adjusting these environment variables:
- `TOTALRECLAW_COSINE_THRESHOLD` -- lower the value (e.g., `0.10`) to be more permissive, or raise it (e.g., `0.25`) to be stricter.
- `TOTALRECLAW_RELEVANCE_THRESHOLD` -- lower the value (e.g., `0.15`) to inject memories more often, or raise it to reduce noise.
- `TOTALRECLAW_MIN_IMPORTANCE` -- raise the value (e.g., `5`) to only store more important facts.

### No LLM available for extraction

**Cause:** The plugin could not find an API key for any supported LLM provider.

**Fix:** Add at least one LLM provider API key to your environment. The simplest option:
```
OPENAI_API_KEY="sk-your-key-here"
```

### Recovery phrase does not restore memories

**Cause:** One or more words in the recovery phrase are wrong or in the wrong order.

**Fix:**
1. Double-check every word against what you wrote down.
2. BIP-39 phrases are **all lowercase**. Make sure you did not capitalize any words.
3. Verify the word order is exactly right. Even one wrong word derives a completely different key and wallet address, meaning the server will treat you as a new user.
4. Make sure there is exactly one space between each word, no leading or trailing spaces.

### Plugin logs show "LLM call failed" errors

**Cause:** Your LLM API key may be invalid, expired, or rate-limited.

**Fix:**
1. Test your API key independently (e.g., make a simple curl request to the provider's API).
2. Check that your API key has not expired or run out of credits.
3. If rate-limited, the plugin will retry on the next extraction cycle (every 5 turns by default).

---

## 15. Known Limitations (Beta)

This is a beta release. The following items are known limitations that will be addressed in future updates:

- **Free tier threshold:** The default limit of 100 writes/month is provisional and may be adjusted based on usage data. The limit is a server-side configuration value.
- **Subscription pricing:** The Pro tier price ($2-5/month) is beta pricing and not yet finalized.
- **Billing tools:** The upgrade flow (`totalreclaw_status`, `totalreclaw_upgrade`) may not be fully wired in all environments. If the agent cannot generate a checkout URL, contact the TotalReclaw team directly.
- **Auto-extraction timing (OpenClaw only):** The `TOTALRECLAW_EXTRACT_EVERY_TURNS` environment variable controls extraction frequency. The plugin fires extraction on the `agent_end` hook every N turns (default: 5). The skill config also accepts `autoExtractEveryTurns` via the `TOTALRECLAW_EXTRACT_EVERY_TURNS` env var.
- **MCP server has no auto-memory:** The MCP server does not have lifecycle hooks. It only responds to explicit tool calls. The host agent (Claude Desktop, Cursor) must call `totalreclaw_remember` and `totalreclaw_recall` explicitly.
- **Batch writes:** On-chain writes are currently sent one fact at a time. Batch writes for gas optimization are not yet implemented.
- **Decay and eviction engine:** The importance decay formula runs, but tuning is ongoing. Low-importance facts decay over time and may be evicted.
- **Write counter persists across upgrades:** If you exhaust the free tier, upgrade to Pro, and later cancel, the write counter from before the upgrade is preserved. The counter resets monthly, not on cancellation.
- **Subgraph mode:** On-chain storage via Gnosis Chain subgraph is functional and deployed to the Chiado testnet. For the beta, subgraph mode is the default storage path.

---

## Further Reading

For those who want to understand the technical architecture:

- **E2EE Architecture:** `docs/specs/totalreclaw/architecture.md` -- how encryption, LSH blind indices, and zero-knowledge search work.
- **Skill Specification:** `docs/specs/totalreclaw/skill-openclaw.md` -- full plugin spec with data models, triggers, and processing pipelines.
- **MCP Onboarding:** `docs/specs/totalreclaw/mcp-onboarding.md` -- MCP-specific onboarding design, seed management, and billing tool specifications.
- **Billing and Onboarding:** `docs/specs/subgraph/billing-and-onboarding.md` -- tier structure, payment infrastructure, and Gnosis Chain rationale.
- **Seed-to-Subgraph:** `docs/specs/subgraph/seed-to-subgraph.md` -- how BIP-39 derivation connects to on-chain identity.
- **Product Requirements:** `docs/prd.md` -- the full product requirements document.

---

*This guide was written for TotalReclaw beta v1.0-beta (updated v1.1). If you encounter issues not covered here, contact the TotalReclaw team.*
