# Client Setup — v1

**Applies to:** TotalReclaw v1 (core 2.0.0, plugin 3.0.0, mcp-server 3.0.0, nanoclaw 3.0.0, python 2.0.0, ZeroClaw 2.0.0).

One setup page per client. v1 is the default on every client — no env toggles, no feature flags to flip. Pick your platform.

---

## Which one do I want?

| You are using... | Install |
|---|---|
| OpenClaw | [OpenClaw plugin](#openclaw-plugin) |
| Claude Desktop, Cursor, Windsurf, or any MCP-compatible agent | [MCP server](#mcp-server) |
| NanoClaw (OpenClaw's lightweight variant) | [NanoClaw skill](#nanoclaw-skill) |
| Python / Hermes Agent | [Python client](#python-client) |
| Native Rust app / ZeroClaw (NEAR AI) | [ZeroClaw](#zeroclaw-rust-crate) |
| IronClaw (NEAR AI) | [MCP server via IronClaw](#mcp-server) |

All clients write to the same vault. If you use multiple clients, use the same 12-word recovery phrase everywhere.

---

## OpenClaw plugin

**Package:** `@totalreclaw/totalreclaw@^3.0.0` (ClawHub).
**Features:** fully automatic — auto-recall on every message, auto-extract every 3 turns, pre-compaction flush, session debrief.

### Install

Tell your OpenClaw agent:

> "Install the TotalReclaw skill from ClawHub"

Or via terminal:

```bash
openclaw skills install totalreclaw
```

### Configure

Set one env var:

```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

If you don't already have a recovery phrase, the plugin will generate one on first run and print it once.

That's it. v1 taxonomy, source-weighted reranking, and the new pin / retype / set_scope tools are all on by default.

**Full guide:** [openclaw-setup.md](./openclaw-setup.md)

---

## MCP server

**Package:** `@totalreclaw/mcp-server@^3.0.0` (npm).
**Features:** 19 MCP tools (including 4 new v1 tools: pin, unpin, retype, set_scope). No lifecycle hooks — the host agent invokes tools from context.

### Install + setup

```bash
npx @totalreclaw/mcp-server setup
```

The wizard generates a 12-word recovery phrase, registers you with the relay, and prints a config snippet.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase here"
      }
    }
  }
}
```

### Cursor / Windsurf

Settings > MCP Servers — use the same block as above.

### IronClaw (NEAR AI)

The IronClaw agent config references the MCP server via the same JSON shape. See [ironclaw-setup.md](./ironclaw-setup.md).

### Verify

Ask the agent: *"What TotalReclaw tools do you have?"* — you should see 19 tools including `totalreclaw_pin`, `totalreclaw_retype`, `totalreclaw_set_scope`.

**Full guide:** [claude-code-setup.md](./claude-code-setup.md)

---

## NanoClaw skill

**Package:** `@totalreclaw/skill-nanoclaw@^3.0.0` (npm, via NanoClaw).
**Features:** automatic memory, same hooks as OpenClaw. Shares the MCP server as a background agent-runner.

### Install

```bash
TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

Add this to your NanoClaw deployment (Docker env, `.env`, or platform config). The NanoClaw agent-runner auto-spawns `@totalreclaw/mcp-server` with this env.

If you need to generate a recovery phrase first, run `npx @totalreclaw/mcp-server setup` on any machine with Node 18+.

**Full guide:** [nanoclaw-getting-started.md](./nanoclaw-getting-started.md)

---

## Python client

**Package:** `totalreclaw==2.0.0` (PyPI).
**Features:** Hermes Agent plugin with pre_llm_call auto-recall + post_llm_call auto-extract + on_session_end debrief. Core remember/recall/forget/export/status tools.

### Install

```bash
pip install totalreclaw
```

**Docker slim images:** install a C compiler first (required by PyStemmer):

```bash
apt-get update && apt-get install -y gcc g++
```

### Use

```python
import asyncio
from totalreclaw import TotalReclaw

async def main():
    client = TotalReclaw(
        recovery_phrase="your twelve word recovery phrase here",
    )
    await client.resolve_address()
    await client.register()

    # v1 fields supported end-to-end
    await client.remember(
        "Pedro prefers dark mode for all editors",
        fact_type="preference",
        importance=0.8,
    )

    results = await client.recall("What does Pedro prefer?")
    for r in results:
        print(f"  [{r.rrf_score:.3f}] {r.text}")

    await client.close()

asyncio.run(main())
```

### Hermes Agent

```bash
pip install totalreclaw[hermes]
```

The plugin registers automatically with Hermes Agent v0.5.0+.

**Full guide:** [hermes-setup.md](./hermes-setup.md)

---

## ZeroClaw (Rust crate)

**Crate:** `totalreclaw-memory = "2.0.0"` (crates.io).
**Features:** native Rust Memory trait with v1 write path (`store_v1`), cosine + fingerprint dedup, v4 outer protobuf. Designed for ZeroClaw (NEAR AI agent framework) but usable in any Rust app.

### Add dependency

```toml
[dependencies]
totalreclaw-memory = "2.0.0"
```

### Use

```rust
use totalreclaw_memory::{Memory, TotalReclawMemory, V1StoreInput};

let memory = TotalReclawMemory::new(recovery_phrase).await?;
memory.store_v1(V1StoreInput {
    text: "Pedro prefers dark mode for all editors".into(),
    type_: "preference".into(),
    source: "user".into(),
    scope: Some("personal".into()),
    volatility: Some("stable".into()),
    importance: 0.8,
    reasoning: None,
}).await?;
```

**Full guide:** [zeroclaw-setup.md](./zeroclaw-setup.md)

---

## After setup

- [Memory types guide](./memory-types-guide.md) — what the agent stores and how to override it via natural language.
- [v1 migration guide](./v1-migration.md) — if you already have memories on an earlier version.
- [Environment variables](./env-vars-reference.md) — short list of actual env vars.
- [Feature comparison](./feature-comparison.md) — which features work on which client.

---

## Troubleshooting

**The setup wizard prints no phrase.** — You already have credentials at the default path (`~/.totalreclaw/credentials.json`). Either set `TOTALRECLAW_CREDENTIALS_PATH` to a new path or remove the existing file.

**The agent says "I don't have memory tools."** — Check the MCP server config path and JSON syntax. On Claude Desktop, restart after editing the config file.

**Pro-tier features are gated.** — Clients auto-detect your tier from the relay's billing response. If you just upgraded, wait up to 2 hours for the billing cache to refresh, or delete `~/.totalreclaw/billing-cache.json` to force a refresh.

**I still see old env vars in my shell.** — Env vars from the v0 era (see [env-vars-reference.md](./env-vars-reference.md#removed-in-v1)) are silently ignored in v1. Safe to delete.
