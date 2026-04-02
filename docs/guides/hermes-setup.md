# Hermes Agent Setup Guide

Set up TotalReclaw as the encrypted memory layer for your Hermes Agent. Your memories are encrypted on-device before they leave -- Hermes handles the conversations, TotalReclaw protects the data.

## Prerequisites

- **Hermes Agent v0.5.0+** (with plugin lifecycle hooks)
- **Python 3.11+**
- ~600 MB disk space for the local embedding model (one-time download)

## 1. Install TotalReclaw

```bash
pip install totalreclaw[hermes]
```

This installs the Python client library and registers the Hermes plugin via the `hermes_agent.plugins` entry point.

Alternatively, install as a local plugin:

```bash
# Copy the plugin to Hermes plugins directory
mkdir -p ~/.hermes/plugins/totalreclaw
cp -r $(python -c "import totalreclaw.hermes; print(totalreclaw.hermes.__path__[0])")/* ~/.hermes/plugins/totalreclaw/
```

## 2. Verify installation

Start Hermes:

```bash
hermes
```

Check plugin status:

```
/plugins
```

You should see:

```
Plugins (1+):
  ✓ totalreclaw v0.1.0 (6 tools, 4 hooks)
```

## 3. Start chatting

The first time you mention anything worth remembering, the agent will:

1. Detect that TotalReclaw is not configured yet
2. Ask: "Do you have an existing recovery phrase, or should I generate a new one?"
3. Run `totalreclaw_setup` with your phrase
4. Register with the relay, download the embedding model, and activate

Your recovery phrase and credentials are saved to `~/.totalreclaw/credentials.json` (owner-only permissions). On subsequent restarts, the plugin loads them automatically.

> **Save your recovery phrase somewhere safe.** It is the only key to your memories. There is no password reset, no recovery, no support ticket that can help. Write it down and store it securely.

## 4. How it works

### Automatic memory (zero effort)

The plugin hooks into Hermes's lifecycle:

| Hook | When | What happens |
|------|------|-------------|
| `on_session_start` | New session | Resets turn counter, checks billing |
| `pre_llm_call` | First turn | Auto-recalls relevant memories, injects into context |
| `post_llm_call` | Every 3 turns | Extracts facts from conversation, stores encrypted |
| `on_session_end` | Session ends | Flushes any unprocessed messages |

### Explicit tools (full control)

| Tool | What it does |
|------|-------------|
| `totalreclaw_remember` | Store a specific memory |
| `totalreclaw_recall` | Search your memory vault |
| `totalreclaw_forget` | Delete a memory by ID |
| `totalreclaw_export` | Export all memories as plaintext |
| `totalreclaw_status` | Check billing and usage |
| `totalreclaw_setup` | Configure credentials |

### What gets extracted

The plugin recognizes 7 memory types:

- **fact** -- "Pedro's email is pedro@example.com"
- **preference** -- "I prefer dark mode"
- **decision** -- "I chose FastAPI because of async support"
- **episodic** -- "We deployed v2.0 on March 15th"
- **goal** -- "I want to launch by Q2"
- **context** -- "The project uses PostgreSQL"
- **summary** -- "Today we discussed the migration plan"

## 5. Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TOTALRECLAW_RECOVERY_PHRASE` | -- | BIP-39 mnemonic (auto-loads on startup) |
| `TOTALRECLAW_SERVER_URL` | `https://api.totalreclaw.xyz` | Relay server URL |

### Hermes config (optional)

Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  totalreclaw:
    memory_mode: hybrid  # "hybrid" (default) or "totalreclaw-only"
```

- **hybrid** -- TotalReclaw works alongside Hermes's built-in memory
- **totalreclaw-only** -- TotalReclaw is the sole memory system

## 6. Cross-agent portability

Your TotalReclaw memories are portable. The same recovery phrase works across:

- **Hermes Agent** (this plugin)
- **OpenClaw** (OpenClaw plugin)
- **Claude Desktop** (MCP server)
- **IronClaw** (MCP server)
- **Any MCP-compatible agent**

Store a memory in Hermes, recall it in Claude Desktop -- same encryption, same data.

## 7. Billing

| Tier | Storage | Cost |
|------|---------|------|
| Free | Unlimited on testnet (Base Sepolia) | $0 |
| Pro | Unlimited on mainnet (Gnosis) | $3.99/month |

Check your usage anytime:

```
What's my TotalReclaw status?
```

Upgrade via:

```
I'd like to upgrade to TotalReclaw Pro
```

## Troubleshooting

### Plugin not loading

```bash
hermes plugins
```

If not listed, verify:
1. `pip list | grep totalreclaw` shows the package
2. Hermes is v0.5.0+ (plugin hooks require v0.5.0)

### "TotalReclaw not configured"

Run setup:
```
Set up TotalReclaw with my recovery phrase: [your 12 words]
```

Or set the env var:
```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word phrase here"
```

### Embedding model download slow

The first recall/remember downloads ~600 MB. Subsequent calls use the cached model. If download fails, the plugin falls back to keyword-only search (no semantic similarity).
