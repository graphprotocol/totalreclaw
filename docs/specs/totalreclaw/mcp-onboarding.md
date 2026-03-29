<!--
Product: TotalReclaw
Version: 1.0
Last updated: 2026-03-04
-->

# Design: NanoClaw MCP Onboarding & Payment UX

**Version:** 1.0
**Date:** March 4, 2026
**Status:** Design Complete
**Depends On:**
- [Billing & Onboarding Architecture](../subgraph/billing-and-onboarding.md)
- [MCP Server Spec](mcp-server.md)
- [MCP Auto-Memory Spec](mcp-auto-memory.md)

---

## 1. Skill vs MCP: Onboarding Differences

The billing architecture in `billing-and-onboarding.md` was designed for the OpenClaw skill, where an orchestrating agent guides the user through every step. The MCP server is a fundamentally different interface.

| Aspect | OpenClaw Skill | Generic MCP Server |
|--------|---------------|-------------------|
| **Runtime context** | Inside an agent that can converse with the user | Passive tool provider; host agent (Claude Desktop, etc.) calls tools |
| **Lifecycle hooks** | `before_agent_start`, `agent_end`, `before_compaction` fire automatically | No hooks; server only acts when a tool is called |
| **UX guidance** | Agent can say "upgrade for $5/mo" proactively | Cannot initiate messages; can only return tool results |
| **Seed generation** | Agent generates seed, displays it, asks user to write it down | Must happen before MCP server starts (env var or setup command) |
| **Seed storage** | `credentials.json` in container workspace | `~/.totalreclaw/credentials.json` or env var on user's machine |
| **Payment trigger** | Agent detects approaching quota, creates checkout URL | Tool call fails with quota error; response includes checkout URL |
| **Payment detection** | Agent polls relay for subscription activation | Next tool call succeeds if subscription is active |
| **Recovery** | Agent prompts for seed, derives wallet, restores | User sets seed in env/config, restarts MCP server |
| **Wallet signing** | Plugin signs relay requests with seed-derived key | MCP server signs relay requests with seed-derived key (identical crypto) |
| **First-run friction** | Zero -- agent handles everything | Must run setup command or set env var before first use |

### Key Insight

The MCP server cannot guide the user. It can only respond to tool calls. This means:

1. **Setup must happen before the MCP server starts** (not during a conversation).
2. **Payment prompts are reactive** (returned as tool results when quota is exceeded), not proactive.
3. **The host agent (Claude Desktop) is the messenger** -- it relays error messages and URLs to the user.

---

## 2. Seed Management for MCP

### 2.1 Where the Seed Comes From

The MCP server uses the same BIP-39 mnemonic as the skill. Three scenarios:

| Scenario | How the seed is provided |
|----------|------------------------|
| **New user (MCP-first)** | Run `npx @totalreclaw/mcp-server setup` to generate a seed and store it |
| **Existing skill user** | Copy the seed from the skill (they wrote it down during onboarding) and paste it into setup |
| **Recovery** | Paste the 12-word seed into setup or env var |

### 2.2 Setup Command (Recommended Path)

A CLI setup command handles seed generation and storage. This runs once, before the user configures their MCP client.

```bash
npx @totalreclaw/mcp-server setup
```

**Behavior:**

```
$ npx @totalreclaw/mcp-server setup

TotalReclaw — Setup
====================

Do you already have a recovery phrase? (y/N): _

[If N — new user]
Generated recovery phrase (WRITE THIS DOWN):

  abandon badge cake dance eagle fabric galaxy habit ice jacket keen ladder

WARNING: If you lose this phrase, your memories are unrecoverable.

Saved credentials to ~/.totalreclaw/credentials.json
Server URL: https://relay.totalreclaw.com (default)

Add this to your MCP client config:
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_CREDENTIALS_PATH": "~/.totalreclaw/credentials.json",
        "TOTALRECLAW_SERVER_URL": "https://relay.totalreclaw.com"
      }
    }
  }
}

[If Y — existing user]
Enter your 12-word recovery phrase: _
> abandon badge cake dance eagle fabric galaxy habit ice jacket keen ladder

Verified. Saved credentials to ~/.totalreclaw/credentials.json
```

### 2.3 What Gets Stored

The setup command derives keys from the mnemonic and stores derived credentials (NOT the mnemonic itself):

```json
// ~/.totalreclaw/credentials.json
{
  "userId": "sha256-of-auth-key-hex",
  "salt": "base64-encoded-32-byte-salt",
  "version": 1
}
```

The mnemonic is shown once and never stored on disk. This matches the skill's behavior: the `credentials.json` contains the userId and salt needed to re-derive keys at runtime, but requires the mnemonic (via env var `TOTALRECLAW_RECOVERY_PHRASE`) to actually derive the encryption key.

### 2.4 Runtime Key Derivation

At MCP server startup:

1. Read `credentials.json` for userId and salt.
2. Read `TOTALRECLAW_RECOVERY_PHRASE` env var for the mnemonic.
3. Derive keys: `deriveKeys(mnemonic)` produces authKey, encryptionKey, dedupKey, salt.
4. Verify: derived salt matches stored salt (ensures correct mnemonic).
5. If no env var and no credentials file: fail with clear error message.

### 2.5 Alternative: Environment Variable Only

For users who prefer not to run a setup command, the existing env-var-only path works:

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "abandon badge cake dance ...",
        "TOTALRECLAW_SERVER_URL": "https://relay.totalreclaw.com"
      }
    }
  }
}
```

The MCP server auto-registers on first run if no `credentials.json` exists, matching the current behavior in `mcp/src/index.ts` (lines 68-76).

### 2.6 Security Considerations

| Concern | Mitigation |
|---------|------------|
| Mnemonic in env var is visible in process list | MCP servers run as child processes of the host app; same trust boundary |
| `credentials.json` on disk | Contains only userId + salt, not the mnemonic or derived keys |
| Mnemonic in `claude_desktop_config.json` | File permissions (600) recommended; documented in README |
| Shared machine risk | Document: "Do not use on shared machines without full disk encryption" |

---

## 3. MCP Tool Additions for Billing

### 3.1 `totalreclaw_status` Tool

Returns the user's subscription status, usage, and upgrade URL if applicable.

```typescript
const statusToolDefinition = {
  name: 'totalreclaw_status',
  description: `Check your TotalReclaw subscription status and usage.

Call this tool when:
- User asks about their subscription or plan
- User asks how many memories they have
- User asks about limits or quota
- You receive a quota error from another TotalReclaw tool

Returns: current tier, usage stats, and upgrade URL if on free tier.`,
  inputSchema: {
    type: 'object',
    properties: {},
  },
  annotations: {
    readOnlyHint: true,
    idempotentHint: true,
  },
};
```

**Response format:**

```json
{
  "tier": "free",
  "usage": {
    "facts_stored": 42,
    "free_writes_remaining": 8,
    "free_write_limit": 50
  },
  "upgrade": {
    "url": "https://totalreclaw.com/upgrade?wallet=0xabc123...",
    "price": "$5/month",
    "features": "Unlimited memories, priority sync"
  }
}
```

For paid users:

```json
{
  "tier": "pro",
  "usage": {
    "facts_stored": 847
  },
  "subscription": {
    "source": "stripe",
    "expires_at": "2026-04-03T00:00:00Z",
    "auto_renew": true
  }
}
```

### 3.2 `totalreclaw_upgrade` Tool

Creates a checkout session and returns the payment URL.

```typescript
const upgradeToolDefinition = {
  name: 'totalreclaw_upgrade',
  description: `Get a payment URL to upgrade your TotalReclaw subscription.

Call this tool when:
- User wants to upgrade to a paid plan
- User asks how to get more memory storage
- Free tier quota is exhausted and user wants to continue

Returns a URL the user can open in their browser to complete payment via Stripe.`,
  inputSchema: {
    type: 'object',
    properties: {
      method: {
        type: 'string',
        enum: ['card'],
        description: 'Payment method. "card" for credit/debit card via Stripe. Default: "card".',
      },
    },
  },
  annotations: {
    readOnlyHint: true,
    idempotentHint: false,
  },
};
```

**Response format:**

```json
{
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_live_abc123...",
  "method": "card",
  "plan": "TotalReclaw Pro — $5/month",
  "instructions": "Open this URL in your browser to complete payment. Your subscription will activate automatically within 60 seconds of payment."
}
```

### 3.3 Why Two Tools Instead of One

- `totalreclaw_status` is read-only and idempotent. The LLM can call it freely to check status.
- `totalreclaw_upgrade` creates a Stripe checkout session (side effect). Separating it prevents accidental session creation on every status check.
- Clear separation makes tool descriptions simpler and LLM tool selection more reliable.

---

## 4. Error Handling Patterns

### 4.1 Quota Exceeded (Free Tier Limit)

When a `totalreclaw_remember` call fails due to quota:

```json
{
  "content": [{
    "type": "text",
    "text": "{\"error\":\"free_tier_quota_exceeded\",\"message\":\"You've used all 50 free memories. Upgrade to TotalReclaw Pro for unlimited storage.\",\"upgrade_url\":\"https://totalreclaw.com/upgrade?wallet=0xabc123...\",\"usage\":{\"facts_stored\":50,\"free_write_limit\":50}}"
  }],
  "isError": true
}
```

The host agent (Claude Desktop) will see this error and can relay it to the user:

> "It looks like you've used all your free TotalReclaw memories. You can upgrade to Pro for $5/month for unlimited storage. Would you like me to get you a payment link?"

### 4.2 No Subscription (Expired)

```json
{
  "content": [{
    "type": "text",
    "text": "{\"error\":\"subscription_expired\",\"message\":\"Your TotalReclaw Pro subscription expired on 2026-03-01. Renew to continue storing memories.\",\"upgrade_url\":\"https://totalreclaw.com/upgrade?wallet=0xabc123...\"}"
  }],
  "isError": true
}
```

### 4.3 Not Configured (No Seed)

When the MCP server starts without a seed or credentials:

```json
{
  "content": [{
    "type": "text",
    "text": "{\"error\":\"not_configured\",\"message\":\"TotalReclaw is not set up. Run 'npx @totalreclaw/mcp-server setup' in your terminal to generate a recovery phrase and configure the server.\",\"docs_url\":\"https://totalreclaw.com/docs/mcp-setup\"}"
  }],
  "isError": true
}
```

### 4.4 Relay Server Unreachable

```json
{
  "content": [{
    "type": "text",
    "text": "{\"error\":\"server_unreachable\",\"message\":\"Cannot reach TotalReclaw server at https://relay.totalreclaw.com. Check your internet connection or server URL configuration.\"}"
  }],
  "isError": true
}
```

### 4.5 Error Design Principles

1. **Always include a human-readable `message`** -- the host agent will relay this to the user.
2. **Include actionable URLs** where applicable (upgrade, docs).
3. **Use structured error codes** (`free_tier_quota_exceeded`, `subscription_expired`, etc.) so the host agent can react programmatically if it wants to.
4. **Never expose internal details** (stack traces, wallet private keys, etc.).
5. **Read operations (recall) should not fail on quota** -- only writes are metered.

---

## 5. Recommended MCP Onboarding Flow (Step-by-Step)

### 5.1 New User Flow

```
Step 1: Install
  $ npm install -g @totalreclaw/mcp-server
  (or use npx for zero-install)

Step 2: Setup (one-time)
  $ npx @totalreclaw/mcp-server setup
  -> Generates 12-word recovery phrase
  -> User writes it down
  -> Saves credentials.json to ~/.totalreclaw/
  -> Registers with relay server (free tier)
  -> Prints MCP client config snippet

Step 3: Configure MCP client
  Copy the printed config into claude_desktop_config.json (or equivalent).
  Set TOTALRECLAW_RECOVERY_PHRASE to the recovery phrase.

Step 4: First conversation
  -> Claude Desktop starts MCP server
  -> MCP server loads credentials, derives keys
  -> User chats normally
  -> LLM calls totalreclaw_recall (per server instructions)
  -> No memories yet; LLM proceeds
  -> User shares preferences
  -> LLM calls totalreclaw_remember
  -> Facts stored (free tier, sponsored gas)

Step 5: Free tier approaches limit
  -> totalreclaw_remember returns quota warning (not error) in response:
     { "success": true, "fact_id": "...", "quota_warning": "4 free writes remaining" }
  -> LLM can mention this to user

Step 6: Free tier exhausted
  -> totalreclaw_remember returns error with upgrade URL
  -> LLM relays to user: "You've used all free memories. Want to upgrade?"
  -> User says yes
  -> LLM calls totalreclaw_upgrade({ method: "card" })
  -> Returns Stripe checkout URL
  -> User opens URL in browser, pays
  -> Within 60 seconds, Stripe webhook activates subscription
  -> Next totalreclaw_remember call succeeds
```

### 5.2 Existing Skill User Flow

```
Step 1: User already has a 12-word recovery phrase from OpenClaw

Step 2: Setup with existing phrase
  $ npx @totalreclaw/mcp-server setup
  > Do you already have a recovery phrase? y
  > Enter your 12-word recovery phrase: abandon badge cake ...
  -> Derives wallet address -> matches existing subscription
  -> Saves credentials.json
  -> Prints config snippet

Step 3-4: Same as new user flow, but memories already exist
  -> First totalreclaw_recall returns existing memories
  -> Subscription is already active (if paid)
```

### 5.3 Recovery Flow

```
Step 1: User has recovery phrase written down

Step 2: Run setup on new machine
  $ npx @totalreclaw/mcp-server setup
  > Enter your 12-word recovery phrase: ...
  -> Re-derives same wallet address
  -> All memories decryptable (same encryption key)
  -> Subscription still active (wallet address matches)
```

---

## 6. Architecture Implications (Code Changes)

### 6.1 Changes to `mcp/src/index.ts`

| Change | Description |
|--------|-------------|
| Add `totalreclaw_status` tool registration | Register in `ListToolsRequestSchema` handler and `CallToolRequestSchema` switch |
| Add `totalreclaw_upgrade` tool registration | Same as above |
| Enhance error handling in `getClient()` | Return structured `not_configured` error instead of crashing |
| Add quota warning to `handleRemember` response | Check remaining free writes after successful store |

### 6.2 New Files

| File | Purpose |
|------|---------|
| `mcp/src/tools/status.ts` | `handleStatus()` -- queries relay for subscription + usage |
| `mcp/src/tools/upgrade.ts` | `handleUpgrade()` -- creates Stripe checkout session via relay API |
| `mcp/src/cli/setup.ts` | CLI setup command for seed generation and credential storage |

### 6.3 Changes to `mcp/package.json`

```json
{
  "bin": {
    "totalreclaw-mcp": "dist/index.js",
    "totalreclaw-setup": "dist/cli/setup.js"
  },
  "scripts": {
    "setup": "node dist/cli/setup.js"
  }
}
```

The `setup` subcommand is detected via `process.argv`:

```typescript
// mcp/src/index.ts (top)
if (process.argv[2] === 'setup') {
  import('./cli/setup.js').then(m => m.runSetup());
} else {
  main();
}
```

### 6.4 Relay Server API Additions

The relay server needs two new endpoints (consumed by the MCP server):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /v1/subscription/status` | GET | Returns tier, usage, limits for a wallet address (authenticated) |
| `POST /v1/subscription/checkout` | POST | Creates Stripe checkout session, returns URL (authenticated) |

These are the same endpoints the OpenClaw skill would use. The MCP server and skill share the same relay API.

### 6.5 Server Instructions Update

Add billing-awareness to the server `instructions` field in `mcp/src/prompts.ts`:

```typescript
// Append to existing SERVER_INSTRUCTIONS:
`
### Billing
If a totalreclaw_remember call fails with a quota error, inform the user and offer to call totalreclaw_upgrade to get a payment link. Do not repeatedly retry failed quota calls.

If the user asks about their subscription or usage, call totalreclaw_status.
`
```

### 6.6 No Changes Needed

| Component | Why no change |
|-----------|---------------|
| `skill/plugin/crypto.ts` | MCP server already uses the same key derivation (via `@totalreclaw/client`) |
| `skill-nanoclaw/mcp/nanoclaw-agent-runner.ts` | NanoClaw now spawns `@totalreclaw/mcp-server` (npm package) — no longer self-contained |
| Relay server auth | Same wallet-signature auth; MCP server signs requests the same way as the skill |
| Subgraph / smart contracts | No changes; billing is in the relay layer, not on-chain |

---

## 7. Subscription Status Checking

### 7.1 How the MCP Server Knows Subscription Status

The flow is identical to the skill:

```
MCP Server                         Relay Server
    |                                    |
    |-- GET /v1/subscription/status ---->|
    |   (signed with wallet key)         |
    |                                    |-- Check subscriptions table
    |                                    |   WHERE wallet_address = <derived>
    |<-- { tier, usage, expires_at } ----|
```

### 7.2 When to Check

- **On `totalreclaw_status` tool call**: Always check (no cache).
- **On `totalreclaw_remember` failure**: The relay rejects the write and returns the quota error directly. The MCP server does not need to pre-check.
- **NOT on every tool call**: Checking subscription on every recall/remember would add latency. The relay server enforces limits server-side; the MCP server just forwards the error.

### 7.3 Caching

Subscription status is not cached in the MCP server. The relay server is authoritative. This avoids stale-cache bugs where a user pays but the MCP server still thinks they are on free tier.

---

## 8. What Happens When Free Tier Is Exhausted

### 8.1 Sequence

```
1. User shares a preference
2. LLM calls totalreclaw_remember({ facts: [{ text: "User prefers dark mode" }] })
3. MCP server forwards to relay
4. Relay checks: wallet 0xabc has 50/50 free writes used
5. Relay returns: 402 Payment Required + structured error
6. MCP server returns isError tool result with upgrade_url
7. LLM reads error, tells user: "Your free memory limit is reached."
8. LLM asks: "Would you like to upgrade?"
9. User: "Yes"
10. LLM calls totalreclaw_upgrade({ method: "card" })
11. MCP server calls relay: POST /v1/subscription/checkout
12. Relay creates Stripe Checkout session
13. MCP server returns checkout URL
14. LLM tells user: "Open this link to upgrade: https://checkout.stripe.com/..."
15. User opens link, pays
16. Stripe webhook fires -> relay activates subscription
17. User: "Done, I paid"
18. LLM calls totalreclaw_remember again -> succeeds
```

### 8.2 Read Operations

`totalreclaw_recall` is NOT metered. Users can always search and read their memories, even after exhausting the free write quota. This is intentional -- locking users out of their own data would be hostile.

### 8.3 Quota Warning (Pre-Exhaustion)

When the free tier is close to exhaustion (e.g., 5 writes remaining), the relay server includes a `quota_warning` field in successful write responses:

```json
{
  "success": true,
  "fact_id": "uuid-here",
  "quota_warning": {
    "free_writes_remaining": 3,
    "free_write_limit": 50,
    "message": "3 free writes remaining. Upgrade anytime with totalreclaw_upgrade."
  }
}
```

The MCP server passes this through in the tool result. The LLM may or may not mention it to the user -- that is the LLM's discretion based on the server instructions.

---

## 9. First-Run Experience

### 9.1 Happy Path (Setup Completed)

If the user ran `setup` and configured their MCP client correctly:

1. MCP server starts, loads credentials, derives keys -- no user interaction needed.
2. First tool call works immediately.
3. User is on free tier.

### 9.2 Unhappy Path (No Setup)

If the user added the MCP server to their config without running setup:

1. MCP server starts with no `TOTALRECLAW_RECOVERY_PHRASE` and no `credentials.json`.
2. First tool call returns:

```json
{
  "error": "not_configured",
  "message": "TotalReclaw needs to be set up first. Run this command in your terminal:\n\n  npx @totalreclaw/mcp-server setup\n\nThis will generate your recovery phrase and save your credentials.",
  "docs_url": "https://totalreclaw.com/docs/mcp-setup"
}
```

3. The host agent (Claude Desktop) relays this message to the user.
4. User runs setup, restarts Claude Desktop (or the MCP server reconnects).

### 9.3 Partial Config (Password but No Credentials File)

If `TOTALRECLAW_RECOVERY_PHRASE` is set but no `credentials.json` exists, the MCP server auto-registers (current behavior in `mcp/src/index.ts` lines 68-76). This is the zero-friction path for users who put their mnemonic directly in the env var.

---

## 10. Open Questions

| # | Question | Impact | Proposed Resolution |
|---|----------|--------|-------------------|
| 1 | Should `setup` store the mnemonic in the system keychain (macOS Keychain, Windows Credential Manager) instead of requiring the env var? | Better security, but adds native dependency | Defer. Env var is simple and matches how all other MCP servers handle secrets. Keychain integration is a future enhancement. |
| 2 | Should the MCP server auto-detect that it was invoked with `setup` via argv, or should it be a separate binary (`totalreclaw-setup`)? | Packaging simplicity | Single binary with argv detection. Simpler npm package, fewer bin entries. |
| 3 | Should `totalreclaw_recall` proactively return subscription status in its response (e.g., "free tier: 42/50 used")? | Reduces need for separate status calls | No. Keep tools focused. Status is a separate concern. The LLM can call `totalreclaw_status` if needed. |
| 4 | How does the checkout URL include the wallet address? | URL structure | Query parameter: `?wallet=0xabc123`. The relay validates the wallet matches the authenticated session. |
| 5 | Should there be a `totalreclaw_setup` MCP tool that runs the setup flow within the conversation? | Could enable fully in-conversation onboarding | Risky. MCP tools run in the server process, not a terminal. Generating and displaying a mnemonic via a tool result is insecure (it would be in the conversation history, potentially logged by the host). Setup should remain a separate CLI step. |
| 6 | What if the user loses their mnemonic but has `credentials.json`? | `credentials.json` alone cannot derive the encryption key | Document clearly: "credentials.json is not a backup. Your recovery phrase is the only way to recover your memories." This is the same tradeoff as any HD wallet. |
| 7 | Should the MCP server support the web dashboard flow (user visits totalreclaw.com/billing)? | Alternative payment path for users who don't want agent-mediated checkout | Yes, as a passive option. The `upgrade_url` in error responses can point to a web dashboard if the relay supports it. But this is a relay/website feature, not an MCP server feature. |

---

## 11. Summary of Recommendations

1. **Seed management**: Setup CLI command (`npx @totalreclaw/mcp-server setup`) for first-time config. Mnemonic in `TOTALRECLAW_RECOVERY_PHRASE` env var at runtime. `credentials.json` stores only userId + salt.

2. **Payment flow**: Two new MCP tools (`totalreclaw_status`, `totalreclaw_upgrade`). Quota errors include upgrade URLs. The host agent relays messages to the user.

3. **Error handling**: Structured JSON errors with human-readable messages and actionable URLs. Read operations never blocked by quota.

4. **First-run**: Setup command generates seed and prints MCP config snippet. If skipped, first tool call returns a clear setup instruction.

5. **Architecture**: Same crypto, same relay API, same wallet-signature auth as the skill. Two new relay endpoints for subscription status and checkout. No on-chain changes.

6. **Principle**: The simplest approach that works. MCP is the secondary interface. Do not over-engineer. Let the host agent be the messenger.
