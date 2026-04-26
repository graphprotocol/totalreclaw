# TotalReclaw for Claude Code, Claude Desktop, Cursor, and Windsurf

TotalReclaw gives any MCP-compatible agent (Claude Code, Claude Desktop, Cursor, Windsurf, IronClaw) encrypted, persistent memory. The fastest setup is a single chat message — the agent fetches this guide, runs the MCP install for you, and walks you through wiring your recovery phrase. **You do nothing in the terminal.**

If you already have Claude Code (or Claude Desktop / Cursor / Windsurf) running, paste this into your chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md>**

Your agent will fetch this page, run the MCP install command, and walk you through getting a recovery phrase into the config without ever printing it in chat. The phrase comes from your browser (via the OpenClaw or Hermes account-setup flow) or from a phrase you already have on file.

---

## Agent instructions (read this if you are a Claude Code / Claude Desktop / Cursor / Windsurf agent that just fetched this URL)

You have been asked to install TotalReclaw. Run the steps below in order. Do not deviate. **Do not invoke `npx @totalreclaw/mcp-server setup`, `totalreclaw_setup`, or any other phrase-touching CLI / tool — those return the recovery phrase through your context, which violates the phrase-safety rule.** Phrase entry is the user's responsibility.

### Step 1 — Detect the host runtime

Decide which of these the user is running:

- **Claude Code** — has the `claude` CLI on PATH (run `command -v claude`).
- **Claude Desktop** — macOS / Windows native app; config at `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).
- **Cursor / Windsurf** — config via Settings UI or `.cursor/mcp.json` / `~/.codeium/windsurf/mcp_config.json`.
- **IronClaw (NEAR AI)** — config at `~/.ironclaw/mcp.json` or via the IronClaw dashboard. See [ironclaw-setup.md](./ironclaw-setup.md) for the IronClaw-specific walkthrough.

If you have a shell tool and `claude` is on PATH, prefer Step 2A (CLI). Otherwise, walk the user through the JSON snippet (Step 2B).

### Step 2A — Register the MCP server via the Claude Code CLI (preferred)

If `claude` is on PATH, run via your shell tool:

```bash
claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server
```

This registers `totalreclaw` at user scope (available across all Claude Code sessions). Do NOT pass `-e TOTALRECLAW_RECOVERY_PHRASE=...` here — the user will add the phrase themselves in Step 3 so it never enters your context.

If the CLI exits cleanly, continue to Step 3. If it fails with `command not found` / ENOENT, fall back to Step 2B.

### Step 2B — Register the MCP server via JSON config (fallback)

Tell the user verbatim:

> *Open your MCP config file ([Claude Desktop on macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`](file:///Users/$USER/Library/Application%20Support/Claude/claude_desktop_config.json), Windows: `%APPDATA%\Claude\claude_desktop_config.json`; Cursor: `.cursor/mcp.json` or Settings → MCP Servers; Windsurf: `~/.codeium/windsurf/mcp_config.json`) and paste the block below into `mcpServers`. Leave the recovery phrase placeholder for now — we'll fill it in next. Reply `done` once the file is saved.*

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "REPLACE_ME_IN_STEP_3"
      }
    }
  }
}
```

Wait for `done` before continuing.

### Step 3 — Get a recovery phrase to the user (NEVER through chat)

Check `test -s ~/.totalreclaw/credentials.json`. If it exists, the user already has a phrase from a previous TotalReclaw client (OpenClaw, Hermes, NanoClaw). Tell them verbatim:

> *You already have a recovery phrase from another TotalReclaw client. Run `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic` in your own terminal (NOT through me — your terminal output should not enter my context), copy the 12 words, and paste them as the value of `TOTALRECLAW_RECOVERY_PHRASE` in the config you just opened. Reply `done` once it's set.*

If `~/.totalreclaw/credentials.json` does NOT exist, tell them verbatim:

> *To generate or import a recovery phrase, the safest path is the browser-side account-setup flow that OpenClaw or Hermes uses (the relay only ever sees ciphertext). If you have OpenClaw or Hermes available, run their account setup once (see [openclaw-setup.md](./openclaw-setup.md) or [hermes-setup.md](./hermes-setup.md)) — it writes `~/.totalreclaw/credentials.json`, and we'll reuse it here. If you don't, generate a 12-word BIP-39 phrase using a trusted offline tool (a hardware wallet, an air-gapped BIP-39 generator, or a password manager's seed-phrase generator), paste the 12 words into `TOTALRECLAW_RECOVERY_PHRASE` in your config (NOT into this chat), and reply `done`.*

Wait for `done`. Do not at any point ask the user to type the phrase into chat. Do not run `npx @totalreclaw/mcp-server setup` via your shell tool — its stdout contains the phrase.

### Step 4 — Restart the host

Tell the user verbatim:

> *Restart your host: in Claude Code, run `claude mcp restart totalreclaw` (or quit and reopen `claude`); in Claude Desktop, quit (Cmd-Q on macOS) and relaunch; in Cursor / Windsurf, restart the app from Settings → MCP Servers (toggle off / on) or quit and relaunch. Reply `done` once it's back up.*

MCP is stateless JSON-RPC — there is no auto-reload, the host has to spawn a fresh `mcp-server` process to pick up the new config.

### Step 5 — Verify

Once the user replies `done`, ask the agent's own internal toolset (yourself): *"Are TotalReclaw tools bound?"* List the `totalreclaw_*` tools you can see. If `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_pin`, etc. are present and don't return `"TotalReclaw is not configured"`, confirm: *"TotalReclaw is set up. Ask me to remember or recall anything."* If they're missing, the host didn't pick up the config — re-prompt the restart in Step 4.

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `npx @totalreclaw/mcp-server setup`, `totalreclaw_setup`, `npx @totalreclaw/totalreclaw generate-mnemonic`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. The MCP server has NO browser account-setup flow (it's stateless JSON-RPC); the user MUST source their phrase from another TotalReclaw client's account-setup flow (OpenClaw / Hermes browser flow), reuse `~/.totalreclaw/credentials.json` from a prior install, or paste a phrase they generated offline directly into the config file. If the user pastes a phrase in chat anyway: tell them it is compromised and they need to generate a fresh wallet via the OpenClaw or Hermes browser account-setup flow.

---

## What's happening (for the human reader)

1. `claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server` registers the TotalReclaw MCP server in your Claude Code user-scope config (`~/.claude.json`). Other MCP-compatible hosts use a JSON config block with the same shape.
2. When the host launches, it spawns `npx @totalreclaw/mcp-server` as a stdio child process and discovers the available tools via the MCP handshake.
3. The MCP server reads `TOTALRECLAW_RECOVERY_PHRASE` from its environment, derives your auth + encryption keys via BIP-39 + HKDF, registers with the relay (`api.totalreclaw.xyz` by default), and writes a credentials cache to `~/.totalreclaw/credentials.json`.
4. All encryption — XChaCha20-Poly1305 for memories, blind indices for search trapdoors, content fingerprinting — happens inside the MCP server process on your machine. The relay only ever sees ciphertext.
5. Unlike OpenClaw and Hermes, the MCP server has **no lifecycle hooks** — there's no auto-recall on every message and no auto-extract every N turns. The host agent (Claude Code, etc.) calls `totalreclaw_remember` and `totalreclaw_recall` explicitly when its prompt context tells it to.

First real interaction downloads a ~600 MB embedding model (cached locally, one-time).

---

## Prerequisites

- **Node.js 18+** (22 recommended)
- **An MCP-compatible host:** Claude Code, Claude Desktop, Cursor, Windsurf, IronClaw, or similar
- **A recovery phrase** — generated via OpenClaw / Hermes browser account-setup flow, an offline BIP-39 generator, or already on file at `~/.totalreclaw/credentials.json`

---

## Manual install (if the chat flow doesn't apply)

If you can't or won't use the chat flow:

```bash
# Register the MCP server (Claude Code; user scope so it's available across sessions)
claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server

# Other hosts: paste the JSON block below into the appropriate config file.
```

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

| Host | Config file |
|---|---|
| Claude Code | `~/.claude.json` (or `claude mcp add -s user`) |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` (project) or Settings → MCP Servers (global) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| IronClaw | `~/.ironclaw/mcp.json` or IronClaw dashboard — see [ironclaw-setup.md](./ironclaw-setup.md) |

Restart the host. Verify by asking *"Do you have access to TotalReclaw memory tools?"*.

> **Testing a release candidate?** Pin via `args: ["-y", "@totalreclaw/mcp-server@rc"]` (latest RC on the `@rc` dist-tag) or `@totalreclaw/mcp-server@3.2.1` (pin a specific version). Check what each tag resolves to: `npm view @totalreclaw/mcp-server dist-tags`. Most users should leave the args at `["-y", "@totalreclaw/mcp-server"]`, which resolves to the current stable.

---

## How MCP differs from OpenClaw and Hermes

| Behavior | OpenClaw plugin / Hermes | MCP server (this guide) |
|---|---|---|
| Auto-recall on every message | yes (lifecycle hook) | no — host invokes `totalreclaw_recall` when its prompt tells it to |
| Auto-extract every N turns | yes (lifecycle hook) | no — host invokes `totalreclaw_remember` when its prompt tells it to |
| Pre-compaction flush | yes | no |
| Session debrief | yes | no (use `totalreclaw_debrief` explicitly) |
| Browser account-setup flow | yes (`totalreclaw_pair` account-setup tool) | **no** — MCP is stateless JSON-RPC; phrase must come from another client or offline tool |
| First-run welcome | yes (host prepends context on session start) | no — MCP doesn't expose session lifecycle to the server |

MCP is the lowest-overhead integration but the most explicit. You'll talk naturally and the host will still recall / store via tool calls when its context gates fire — you just won't get the every-turn automatic behavior that OpenClaw and Hermes provide.

---

## Available tools

| Tool | Example prompt |
|------|---------------|
| **Remember** | "Remember that I prefer PostgreSQL over MySQL" |
| **Recall** | "What do you remember about my database choices?" |
| **Forget** | "Forget what you know about my old email address" |
| **Pin / Unpin** | "Pin that — it's important" / "Unpin the note about my old editor" |
| **Retype** | "That should be a preference, not a fact" (types: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`) |
| **Set scope** | "File that under work" (scopes: `work`, `personal`, `health`, `family`, `creative`, `finance`, `misc`) |
| **Export** | "Export all my TotalReclaw memories as markdown" |
| **Status** | "What's my TotalReclaw status?" |
| **Import from** | "Import my Gemini history from ~/Downloads/..." |
| **Debrief** | "Debrief this session" — captures session-level summaries |
| **Upgrade / Migrate / Account** | billing flow tools (Stripe checkout, testnet → mainnet migration) |

Talk naturally — the host LLM picks the right tool from context. See [memory types guide](./memory-types-guide.md) for the v1 taxonomy.

---

## Importing from other tools

TotalReclaw can import from Mem0, MCP Memory Server, ChatGPT, Claude, and Gemini:

> "Import my memories from Mem0 using API key m0-your-key-here"

See [Importing Memories](importing-memories.md).

---

## Multi-device and portability

Your recovery phrase works across every TotalReclaw client. Same phrase = same memories.

- **OpenClaw** — see [openclaw-setup.md](./openclaw-setup.md)
- **Hermes (Python)** — see [hermes-setup.md](./hermes-setup.md)
- **NanoClaw** — see [nanoclaw-getting-started.md](./nanoclaw-getting-started.md)
- **IronClaw (NEAR AI)** — see [ironclaw-setup.md](./ironclaw-setup.md)
- **ZeroClaw (Rust)** — see the ZeroClaw README in the main repo

Same phrase, same memories. Switch agents without losing anything. To migrate, copy `~/.totalreclaw/credentials.json` (or just the `mnemonic` field) to the new machine.

---

## Billing

| Tier | Storage | Price |
|------|---------|-------|
| **Free** | Unlimited on Base Sepolia testnet (may reset) | $0 |
| **Pro** | Permanent on Gnosis mainnet | $3.99/month |

Both tiers have unlimited reads. Upgrade: *"Upgrade my TotalReclaw subscription."*

[Pricing](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent can't see TotalReclaw tools after restart**: confirm `claude mcp list` (Claude Code) or your host's MCP UI lists `totalreclaw`. Verify `TOTALRECLAW_RECOVERY_PHRASE` is set in the config (host won't strip stderr from the spawned `mcp-server` process — check your host's MCP server logs for the `TotalReclaw configured` line). If the env var is missing, the server starts in unconfigured mode and tool calls return `{"error": "TotalReclaw is not configured"}`.
- **"Not authenticated" / 401**: check your phrase — exact words, exact order, lowercase, single spaces.
- **First-run is slow / model download**: the embedding model is ~600 MB, downloaded once and cached. Be patient on first call.
- **Quota exceeded (403)**: free tier has a monthly write cap. Upgrade with *"Upgrade my TotalReclaw subscription"*.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via the OpenClaw or Hermes browser account-setup flow. The leaked phrase is unrecoverable once shipped through LLM context.

---

## MCP-specific notes

- **MCP is stateless.** There's no first-run welcome, no lifecycle hooks, and no auto-restart on config change. You restart the host explicitly to pick up new config.
- **No browser account-setup flow.** The MCP server has no HTTP routes — phrase entry is the user's responsibility (config file, env var, or copying from another client's `credentials.json`). This is by design: keeping crypto secrets out of LLM context is more important than convenience parity with OpenClaw / Hermes.
- **`totalreclaw_setup` exists but should not be used.** The tool exists for legacy compatibility. It generates a phrase and returns it through MCP tool-output JSON, which means the phrase enters the host LLM's context. The phrase-safety rule forbids this. New installs should always use the workflow in this guide.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/claude-code-setup.md>**

---

## Further reading

- [OpenClaw setup guide](./openclaw-setup.md) — same vault, different runtime, with browser account-setup flow
- [Hermes setup guide](./hermes-setup.md) — Python client with browser account-setup flow
- [Beta tester deep-dive](./beta-tester-guide-detailed.md) — env vars, extraction tuning, architecture
- [Memory types guide](./memory-types-guide.md) — v1 taxonomy
- [Importing memories](./importing-memories.md)
- [IronClaw setup](./ironclaw-setup.md) — full walkthrough for IronClaw (NEAR AI)
- [Feature comparison](./feature-comparison.md)
- [totalreclaw.xyz](https://totalreclaw.xyz)
