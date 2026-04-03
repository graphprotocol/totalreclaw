# TotalReclaw for NanoClaw -- Getting Started

This guide walks NanoClaw admins through setting up TotalReclaw encrypted memory for their agents, from scratch. No prior TotalReclaw experience required.

---

## What You Get

Once configured, your NanoClaw agent will:

- **Automatically remember** important facts from conversations (preferences, decisions, context)
- **Automatically recall** relevant memories at the start of each session
- **Preserve context** before compaction so nothing is lost
- **Encrypt everything** inside the container -- the TotalReclaw server never sees plaintext

End users do not need to do anything. Memory is fully automatic.

---

## Prerequisites

- A running NanoClaw deployment (Docker or platform-hosted)
- Node.js 18+ available on your local machine (for the one-time setup wizard)
- Internet access from the NanoClaw container (to reach `api.totalreclaw.xyz`)

---

## Step 1: Generate a Recovery Phrase

The **recovery phrase** is a 12-word phrase that derives all encryption keys client-side. It never leaves the container. If you lose it, your encrypted memories are unrecoverable.

On your local machine (not inside the container), run:

```bash
npx @totalreclaw/mcp-server setup
```

The wizard will:

1. Ask if you have an existing recovery phrase (say **no** if this is your first time)
2. Generate a cryptographically secure 12-word recovery phrase
3. Register the phrase with the TotalReclaw managed service
4. Display the recovery phrase for you to save

**Write down the 12 words in exact order and store them securely.** Treat this like a password -- anyone with the phrase can decrypt your memories.

> **Already have a recovery phrase?** If you previously set up TotalReclaw with Claude Desktop, OpenClaw, or another NanoClaw instance, you can reuse the same phrase. Your agent will have access to all existing memories.

---

## Step 2: Configure Your NanoClaw Deployment

Add these environment variables to your NanoClaw deployment. The MCP server automatically registers with the relay on startup -- no manual setup step is needed.

### Required

```bash
TOTALRECLAW_RECOVERY_PHRASE="word1 word2 word3 word4 word5 word6 word7 word8 word9 word10 word11 word12"
```

### Optional

```bash
# Use self-hosted server instead of managed service
# TOTALRECLAW_SELF_HOSTED=true
# TOTALRECLAW_SERVER_URL=https://your-server.example.com

# Namespace for memory isolation (default: group folder name)
# TOTALRECLAW_NAMESPACE=main

# Enable/disable automatic fact extraction (default: true)
# TOTALRECLAW_AUTO_EXTRACT=true

# Turns between extractions (default: 3)
# TOTALRECLAW_EXTRACT_INTERVAL=3
```

### Where to set these

This depends on how you deploy NanoClaw:

- **Docker Compose:** Add to the `environment` section of your service
- **Docker run:** Pass with `-e` flags
- **Railway / Render / Fly.io:** Add in the platform's environment variable settings
- **`.env` file:** Add to the file if your deployment reads from one

After setting the variables, restart your NanoClaw deployment.

> **First run note:** The MCP server downloads a ~600MB embedding model on first startup. This is cached locally and only happens once per deployment. Allow extra time for the first boot.

---

## Step 3: Verify It Works

Start a conversation with your NanoClaw agent and ask:

> "Do you have access to TotalReclaw memory tools?"

The agent should confirm access to tools including `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, and `totalreclaw_export`.

For a more detailed check, ask:

> "What's my TotalReclaw status?"

The agent will report your tier (Free or Pro), usage count, and storage mode.

### Quick functional test

1. Tell the agent: *"Remember that my favorite color is blue."*
2. Start a **new conversation** (to clear context).
3. Ask: *"What do you remember about my favorite color?"*

If the agent recalls that your favorite color is blue, everything is working.

---

## How Memory Works

### Automatic Hooks

NanoClaw's lifecycle hooks handle memory without any user action:

| Hook | When it fires | What happens |
|------|---------------|-------------|
| `before-agent-start` | Before processing each user message | Searches for relevant memories and injects them into context |
| `agent-end` | After the agent responds | Extracts new facts from the conversation and stores them encrypted |
| `pre-compact` | Before context truncation | Does a full extraction pass to preserve all facts before context is lost |

### Extraction

The agent periodically extracts atomic facts from conversations across 7 categories:

- **Preferences** ("prefers dark mode")
- **Decisions** ("chose PostgreSQL for the database because the data is relational")
- **Facts** ("works at Acme Corp")
- **Goals** ("wants to launch by Q2")
- **Episodic** ("we deployed v2.0 on March 15th")
- **Context** ("the project uses PostgreSQL")
- **Summary** ("today we discussed the migration plan")

Each fact is encrypted with AES-256-GCM before leaving the container. The server stores only ciphertext.

### Namespaces

Memories are isolated by namespace. By default, NanoClaw maps each `groupFolder` to a namespace of the same name:

- The `main` group's memories are in the `main` namespace
- The `work` group's memories are in the `work` namespace

Agents in one namespace cannot see memories from another. To share memories across groups, use the same namespace (set `TOTALRECLAW_NAMESPACE` to the same value).

---

## Managing Memory

While memory is automatic, your users (or you) can also interact with it explicitly:

| Action | How |
|--------|-----|
| Store something specific | *"Remember that the API key rotates every 90 days"* |
| Search memories | *"What do you remember about our deployment process?"* |
| Delete a memory | *"Forget the memory about my old address"* |
| Export all memories | *"Export my memories as Markdown"* |
| Check usage | *"What's my TotalReclaw status?"* |
| Upgrade to Pro | *"How do I upgrade TotalReclaw?"* |

---

## Multiple NanoClaw Instances

If you run multiple NanoClaw instances and want them to share the same memory:

1. Use the **same recovery phrase** across all instances
2. Use the **same namespace** (or the same group folder name)

If you want separate memory per instance, use **different recovery phrases** or **different namespaces**.

---

## Billing

| Tier | Writes | Reads | Storage | Price |
|------|--------|-------|---------|-------|
| **Free** | Unlimited | Unlimited | Testnet (Base Sepolia) | $0 |
| **Pro** | Unlimited | Unlimited | Permanent on-chain (Gnosis) | $3.99/month |

The free tier stores on Base Sepolia testnet (unlimited, but testnet data may be reset). Upgrade to Pro for permanent storage on Gnosis mainnet.

To upgrade, ask the agent: *"How do I upgrade TotalReclaw?"*

---

## Troubleshooting

### TotalReclaw tools not available

- **Check env vars:** Verify `TOTALRECLAW_RECOVERY_PHRASE` is set and not empty
- **Check MCP server:** Ensure `@totalreclaw/mcp-server` is installed in the Docker image:
  ```dockerfile
  RUN npm install -g @totalreclaw/mcp-server
  ```
- **Restart:** Environment variable changes require a container restart

### Registration failed / network errors

- Verify the container can reach `https://api.totalreclaw.xyz`
- Check for firewall rules or proxy settings blocking outbound HTTPS

### Memories not appearing across sessions

- Confirm the **same recovery phrase** is used (different phrase = different encryption keys = different memories)
- Check the **namespace** -- memories stored in `work` are not visible in `main`
- Wait a few seconds after storing -- on-chain indexing has a brief delay

### Quota exceeded (403 errors)

- The free tier is unlimited on testnet, but a high abuse-prevention cap exists server-side
- If you encounter a 403, check with *"What's my TotalReclaw status?"*
- Upgrade to Pro for permanent mainnet storage

### First startup is slow

- The MCP server downloads a ~600MB embedding model on first run. This is normal and only happens once. Subsequent starts are fast.

---

## Security Notes

- The recovery phrase derives all encryption keys. **Never share it** or commit it to version control.
- Use secrets management (Docker secrets, platform secret store) rather than plaintext `.env` files in production.
- The TotalReclaw server never sees plaintext data. Even if the server is compromised, your memories remain encrypted.
- All encryption uses AES-256-GCM. Key derivation uses BIP-39 seed + HKDF-SHA256.

---

## Next Steps

- [Beta Tester Guide](beta-tester-guide.md) -- full reference for all platforms, advanced features, and known limitations
- [Importing Memories](importing-memories.md) -- import from Mem0 or other sources
- [NanoClaw README](../../skill-nanoclaw/README.md) -- technical reference
- [totalreclaw.xyz](https://totalreclaw.xyz) -- project homepage
