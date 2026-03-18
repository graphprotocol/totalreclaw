# TotalReclaw Skill for NanoClaw

Zero-knowledge encrypted memory for NanoClaw agents, powered by the `@totalreclaw/mcp-server`.

## Setup

### Step 1: Recovery phrase

The **recovery phrase** is a 12-word BIP-39 mnemonic that derives all encryption keys client-side. TotalReclaw never sends it to the server.

- **New user:** Generate a 12-word BIP-39 mnemonic. Save it securely — it's the only way to recover your memories.
- **Returning user:** Use your existing phrase to restore memories on a new device.

### Step 2: Configure environment

Set these in your NanoClaw deployment (Docker env, `.env` file, or platform config):

```bash
TOTALRECLAW_SERVER_URL=https://api.totalreclaw.xyz
TOTALRECLAW_MASTER_PASSWORD="your twelve word recovery phrase here"
# Managed service is the default — set TOTALRECLAW_SELF_HOSTED=true only for self-hosted mode
```

The NanoClaw agent-runner automatically spawns the MCP server with these variables.

### Step 3: Verify

Ask the agent: *"Do you have access to TotalReclaw memory tools?"* It should confirm access to `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, and `totalreclaw_export`.

## How It Works

All encryption happens inside the MCP server process — the TotalReclaw server only sees ciphertext and hashed tokens:

- Facts encrypted with AES-256-GCM before leaving the container
- Search uses blind indices (SHA-256 hashes), not plaintext
- Recovery phrase derives all keys via Argon2id + HKDF
- With the managed service, encrypted facts are stored on-chain (Gnosis Chain) and indexed by The Graph

## Hooks

| Hook | Description |
|------|-------------|
| `before-agent-start` | Retrieves relevant memories before processing user message |
| `agent-end` | Extracts and stores facts periodically after agent turns |
| `pre-compact` | Full extraction before context truncation |

## Namespace Mapping

The skill maps NanoClaw's `groupFolder` to TotalReclaw's `namespace`:
- `main` → `main` namespace
- `work` → `work` namespace
- `family` → `family` namespace

This provides memory isolation between different contexts.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TOTALRECLAW_SERVER_URL` | TotalReclaw server URL | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_MASTER_PASSWORD` | 12-word BIP-39 recovery phrase | Required |
| `TOTALRECLAW_SELF_HOSTED` | Set to `true` to use your own server instead of the managed service | `false` |
| `TOTALRECLAW_NAMESPACE` | Default namespace | Group folder name |
| `TOTALRECLAW_AUTO_EXTRACT` | Enable automatic extraction | `true` |
| `TOTALRECLAW_EXTRACT_INTERVAL` | Turns between extractions | `5` |
| `TOTALRECLAW_CHAIN_ID` | Chain ID (10200=Chiado, 100=Gnosis) | `10200` |

> **Pro tip:** Pro-tier NanoClaw deployments can lower `TOTALRECLAW_EXTRACT_INTERVAL` to `2` for more frequent, higher-fidelity extraction.

## Available Tools

| Tool | Description |
|------|-------------|
| `totalreclaw_remember` | Store a fact in encrypted memory |
| `totalreclaw_recall` | Search memories by natural language query |
| `totalreclaw_forget` | Delete a specific memory by ID |
| `totalreclaw_export` | Export all memories decrypted as Markdown or JSON |
| `totalreclaw_status` | Check billing status and quota usage |
