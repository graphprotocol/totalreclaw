# Hermes Agent Setup Guide

Set up TotalReclaw as the encrypted memory layer for your Hermes Agent. Your memories are encrypted on-device before they leave -- Hermes handles the conversations, TotalReclaw protects the data.

## Prerequisites

- **Hermes Agent v0.5.0+** (with plugin lifecycle hooks)
- **Python 3.11+**
- **C compiler** (gcc/g++) -- needed to build PyStemmer from source
- ~344 MB disk space for the local embedding model (one-time download)

> **Docker users:** On slim images (e.g., `python:3.12-slim`), install a C compiler first:
> ```bash
> apt-get update && apt-get install -y gcc g++
> ```

> **Ubuntu/Debian users:** System Python on Ubuntu 23.04+ and Debian 12+ is "externally managed." You will need the `--break-system-packages` flag on every `pip install` (or use a virtual environment). System packages like PyYAML and PyJWT may also conflict with pip-installed versions -- see the troubleshooting note in the install section below.

## 1. Install TotalReclaw

```bash
# Step 1: Install TotalReclaw and Hermes Agent
pip install totalreclaw
pip install "git+https://github.com/NousResearch/hermes-agent.git"

# Step 2: Register the TotalReclaw plugin with Hermes
mkdir -p ~/.hermes/plugins/totalreclaw
cp -r $(python -c "import totalreclaw.hermes; print(totalreclaw.hermes.__path__[0])")/* ~/.hermes/plugins/totalreclaw/
```

> **Ubuntu/Debian/Docker note:** Add `--break-system-packages` if you see "externally-managed-environment" errors:
> ```bash
> pip install --break-system-packages totalreclaw
> pip install --break-system-packages "git+https://github.com/NousResearch/hermes-agent.git"
> ```

> **PyYAML / PyJWT conflicts:** On systems with OS-managed Python packages, pip may refuse to uninstall system-owned packages like PyYAML or PyJWT. If you see "Cannot uninstall PyYAML" or similar errors:
> ```bash
> # Force-install the conflicting packages first
> pip install --break-system-packages --ignore-installed PyYAML PyJWT rich requests
> # Then retry the hermes-agent install
> pip install --break-system-packages "git+https://github.com/NousResearch/hermes-agent.git"
> ```

> **Why two steps?** Hermes Agent is not yet on PyPI (install from GitHub). Hermes discovers plugins via its `~/.hermes/plugins/` directory, so the copy step is required.

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
  ✓ totalreclaw v1.0.0 (8 tools, 4 hooks)
```

## 3. Start chatting

The first time you mention anything worth remembering, the agent will:

1. Detect that TotalReclaw is not configured yet
2. Ask: "Do you have an existing recovery phrase, or should I generate a new one?"
3. Run `totalreclaw_setup` — if you don't have a phrase, the tool generates one automatically
4. Register with the relay, download the embedding model (~344 MB, one-time), and activate

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
| `totalreclaw_import_from` | Import memories from Gemini, ChatGPT, Claude, Mem0 |
| `totalreclaw_import_batch` | Process one batch of a large import |

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

### Environment variables (all optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `TOTALRECLAW_RECOVERY_PHRASE` | -- | Import an existing recovery phrase. If not set, the agent generates one on first use. |
| `TOTALRECLAW_SERVER_URL` | `https://api.totalreclaw.xyz` | Override for self-hosted deployments. Most users don't need this. |

TotalReclaw automatically uses whatever LLM provider you configured for Hermes. It picks a fast/cheap model from the same provider for fact extraction -- no extra API keys or model settings needed.

> **Z.AI users:** If you use Z.AI as your Hermes provider, set provider name to `zai` and add `ZAI_API_KEY` to `~/.hermes/.env`. TotalReclaw reads this automatically -- no separate TotalReclaw LLM configuration needed.

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

## 8. Importing conversation history

TotalReclaw can import your conversation history from Gemini, ChatGPT, Claude, and other AI tools. This lets you consolidate all your AI memories into one encrypted vault.

**Quick start:**

> "Import my Gemini history from ~/Downloads/Takeout/My Activity/Gemini Apps/My Activity.html"

For detailed export instructions and supported sources, see the **[Importing Memories guide](importing-memories.md)**.

The agent will show you an estimate before importing and process large files in batches with progress updates. All data is encrypted on your device before storage.

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

Or set the env var (auto-registers with relay on first use):
```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word phrase here"
```

### Embedding model download

The first recall/remember downloads ~344 MB (Harrier model). Subsequent calls use the cached model. If download fails, the plugin falls back to keyword-only search (no semantic similarity).

### Permission denied on model download (Docker)

If you see EACCES errors when the embedding model downloads, set the `HF_HOME` environment variable to a writable directory:

```bash
export HF_HOME=/tmp/hf-cache
```

This is common in Docker containers where the global npm cache directory isn't writable.
