# TotalReclaw for Claude Code, Cursor, and Windsurf

TotalReclaw gives MCP-capable coding tools (Claude Code, Cursor, Windsurf, IronClaw) encrypted, persistent memory. The fastest setup is a single chat message — the agent fetches this guide, runs the MCP install for you, and gives you a link to finish setup in your browser. **You do nothing in the terminal, and your recovery phrase never goes through the chat.**

> **Using Claude Desktop or another chat app?** Follow [`claude-desktop-setup.md`](./claude-desktop-setup.md) instead. Chat apps are the main audience for the MCP server; this guide covers coding tools.
>
> **A dedicated integration for coding tools is planned**: hooks plus a CLI skill, so recall and capture happen automatically. Until it ships, this guide sets up the same MCP server that chat apps use.

> **Wrong runtime?** This guide is for **MCP-capable coding tools** (Claude Code, Cursor, Windsurf, IronClaw). For **Claude Desktop**, follow [`claude-desktop-setup.md`](./claude-desktop-setup.md). If you are an **OpenClaw** agent, follow [`openclaw-setup.md`](./openclaw-setup.md); if you are a **Hermes** agent, follow [`hermes-setup.md`](./hermes-setup.md); if you are unsure which runtime you are, follow the universal dispatcher [`install.md`](./install.md) instead.

If you already have Claude Code (or Cursor / Windsurf) running, paste this into your chat:

> **Install TotalReclaw, see <https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/claude-code-setup.md>**

Your agent will fetch this page, run the MCP install command, and then give you a link and a 6-digit PIN. You open the link in any browser, on this computer or your phone. The browser page creates your recovery phrase, or takes the one you already have. Your phrase never goes through the chat and is never pasted into a config file.

---

## Agent instructions (read this if you are a Claude Code / Cursor / Windsurf agent that just fetched this URL)

You have been asked to install TotalReclaw. Run the steps below in order. Do not deviate. **Do not invoke `npx @totalreclaw/mcp-server setup` or any other phrase-touching CLI / tool — those return the recovery phrase through your context, which violates the phrase-safety rule.** Account setup happens in the user's browser through `totalreclaw_pair` (Step 4). It needs `@totalreclaw/mcp-server` 3.5.1 or later. If you are running inside **Claude Desktop**, stop and follow [`claude-desktop-setup.md`](./claude-desktop-setup.md) instead.

### Step 1 — Detect the host runtime

Decide which of these the user is running:

- **Claude Code** — has the `claude` CLI on PATH (run `command -v claude`).
- **Claude Desktop** — not covered here; follow [`claude-desktop-setup.md`](./claude-desktop-setup.md).
- **Cursor / Windsurf** — config via Settings UI or `.cursor/mcp.json` / `~/.codeium/windsurf/mcp_config.json`.
- **IronClaw (NEAR AI)** — config at `~/.ironclaw/mcp.json` or via the IronClaw dashboard. See [ironclaw-setup.md](./ironclaw-setup.md) for the IronClaw-specific walkthrough.

If you have a shell tool and `claude` is on PATH, prefer Step 2A (CLI). Otherwise, walk the user through the JSON snippet (Step 2B).

### Step 2A — Register the MCP server via the Claude Code CLI (preferred)

If `claude` is on PATH, run via your shell tool:

```bash
claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server
```

This registers `totalreclaw` at user scope (available across all Claude Code sessions). Do NOT pass `-e TOTALRECLAW_RECOVERY_PHRASE=...` here — the account is set up in the browser in Step 4, so no phrase ever goes into the config.

To pin a release candidate: `claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server@rc` (newest RC line) or `@totalreclaw/mcp-server@latest` (current stable). Prefer `@latest` — the `rc` dist-tag tracks the newest release-candidate and can lag `latest`, so `@rc` is not necessarily newer than stable; use it only to test a specific RC.

If the CLI exits cleanly, continue to Step 3. If it fails with `command not found` / ENOENT, fall back to Step 2B.

### Step 2B — Register the MCP server via JSON config (fallback)

Tell the user verbatim:

> *Open your MCP config file (Cursor: `.cursor/mcp.json` or Settings → MCP Servers; Windsurf: `~/.codeium/windsurf/mcp_config.json`) and paste the block below into `mcpServers`. There is no recovery phrase to add — you'll set up your account in the browser in a moment. Reply `done` once the file is saved.*

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"]
    }
  }
}
```

Wait for `done` before continuing.

### Step 3 — Restart the host so the TotalReclaw tools load

Tell the user verbatim:

> *Restart your host so it loads TotalReclaw: in Claude Code, quit and reopen `claude`; in Cursor / Windsurf, restart the app from Settings → MCP Servers (toggle off / on) or quit and relaunch. Reply `done` once it's back up.*

MCP is stateless JSON-RPC — there is no auto-reload, the host has to spawn a fresh `mcp-server` process to pick up the new config.

### Step 4 — Set up the account in the browser with `totalreclaw_pair`

Call `totalreclaw_status` first.

- If it reports a configured account, skip to Step 6: the server found an existing `~/.totalreclaw/credentials.json` from another TotalReclaw client on this machine and is using it.
- If it returns `not_configured` with a `reason` (for example `keychain_wrapped`: Hermes keeps the phrase in the system keychain, which the server can't read yet), tell the user the `message` in plain words and stop. Don't pair.
- If it returns `not_configured` with no `reason`, continue below.

Ask the user one question: *"Are you new to TotalReclaw, or do you already have a TotalReclaw recovery phrase?"* Then call `totalreclaw_pair` with `mode: "generate"` for a new user, or `mode: "import"` for a user who already has a phrase. Other TotalReclaw clients on this computer (for example Hermes or OpenClaw) share `~/.totalreclaw/credentials.json`. From 3.5.1, `totalreclaw_pair` refuses with `already_configured` when that file exists, so it never replaces another client's account. Tell the user they are already set up, and never move, rename or delete the file. Older servers replaced the file, so if the user already uses TotalReclaw on this computer, always use `import` with that same phrase, never `generate`. The tool returns a `url` and a 6-digit `pin`. Tell the user verbatim, filling in the two values:

> *Open this link in any browser — on this computer or your phone: <url>. Enter this PIN when asked: <pin>. The link works for about 10 minutes; if it stops working, tell me and I'll make a new one. Reply `done` when the page says "You're all set".*

Then add the one line that matches their answer:

- New user: *"The page will create your recovery phrase. Write it down on paper and keep it safe — it is the only way back into your memories if you lose this computer."*
- Existing phrase: *"Type your recovery phrase into that page, not into this chat."*

Wait for `done`. If the link expired or the page couldn't finish, call `totalreclaw_pair` again for a fresh link and PIN. Do not at any point ask the user to type the phrase into chat or into a config file. Do not run `npx @totalreclaw/mcp-server setup` via your shell tool — its stdout contains the phrase.

### Step 5 — Restart the host again

Pairing writes `~/.totalreclaw/credentials.json`, and the server only reads it at startup. Tell the user verbatim:

> *Almost done. Restart your host once more, the same way as before, so TotalReclaw picks up your account. Reply `done` once it's back up.*

### Step 6 — Verify

Once the user replies `done`, ask the agent's own internal toolset (yourself): *"Are TotalReclaw tools bound?"* List the `totalreclaw_*` tools you can see. If `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_pin`, etc. are present and don't return `"TotalReclaw is not configured"`, confirm: *"TotalReclaw is set up. Ask me to remember or recall anything."* If they're missing, the host didn't pick up the config — re-prompt the restart in Step 3. If they return "not configured", pairing didn't finish — go back to Step 4.

### Phrase safety (HARD — never break)

NEVER echo, generate, or ask the user to paste a recovery phrase in chat. NEVER invoke `npx @totalreclaw/mcp-server setup`, `npx @totalreclaw/totalreclaw generate-mnemonic`, or any phrase-touching CLI via your shell tool — stdout enters LLM context. The MCP server is stateless JSON-RPC (no HTTP server of its own), but it DOES expose a browser-mediated account-setup flow via the `totalreclaw_pair` tool — the phrase is generated/imported in the browser, uploaded as ciphertext, decrypted in-memory, and written to `~/.totalreclaw/credentials.json`, so it never enters your context. Always use `totalreclaw_pair`. An existing `~/.totalreclaw/credentials.json` from another TotalReclaw client on the same machine is used as is, with nothing to copy. Never suggest generating a phrase with another tool, and never suggest putting the phrase into the config file unless the user explicitly asks for the advanced path below. If the user pastes a phrase in chat anyway: don't repeat it, tell them it is compromised and should be replaced with a new account, and call `totalreclaw_support` for how to get help. (`totalreclaw_pair` won't replace an existing account on this computer.)

---

## What's happening (for the human reader)

1. `claude mcp add -s user totalreclaw -- npx -y @totalreclaw/mcp-server` registers the TotalReclaw MCP server in your Claude Code user-scope config (`~/.claude.json`). Other MCP-compatible hosts use a JSON config block with the same shape.
2. When the host launches, it spawns `npx @totalreclaw/mcp-server` as a stdio child process and discovers the available tools via the MCP handshake.
3. `totalreclaw_pair` opens a short-lived pairing session. Your browser creates (or takes) your recovery phrase, encrypts it to the MCP server's one-time key, and sends only ciphertext through the relay (`api.totalreclaw.xyz` by default). The MCP server decrypts it on your machine, derives your keys, registers with the relay, and writes `~/.totalreclaw/credentials.json` (mode `0600`). On later starts it reads that file.
4. All encryption — XChaCha20-Poly1305 for memories, blind indices for search trapdoors, content fingerprinting — happens inside the MCP server process on your machine. The relay only ever sees ciphertext.
5. Unlike OpenClaw and Hermes, the MCP server has **no lifecycle hooks** — there's no auto-recall on every message and no auto-extract every N turns. The host agent (Claude Code, etc.) calls `totalreclaw_remember` and `totalreclaw_recall` explicitly when its prompt context tells it to.

First real interaction downloads a ~600 MB embedding model (cached locally, one-time).

---

## Prerequisites

- **Node.js 18+** (22 recommended)
- **An MCP-capable coding tool:** Claude Code, Cursor, Windsurf, IronClaw, or similar (for Claude Desktop, see [`claude-desktop-setup.md`](./claude-desktop-setup.md))
- **A browser** on this computer or your phone, for the one-time pairing step. You don't need a recovery phrase beforehand: pairing creates one, or takes the one you already have.

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
      "args": ["-y", "@totalreclaw/mcp-server"]
    }
  }
}
```

| Host | Config file |
|---|---|
| Claude Code | `~/.claude.json` (or `claude mcp add -s user`) |
| Claude Desktop | see [`claude-desktop-setup.md`](./claude-desktop-setup.md) |
| Cursor | `.cursor/mcp.json` (project) or Settings → MCP Servers (global) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| IronClaw | `~/.ironclaw/mcp.json` or IronClaw dashboard — see [ironclaw-setup.md](./ironclaw-setup.md) |

Restart the host, then ask your agent *"Set up TotalReclaw"*. It calls `totalreclaw_pair` and gives you a link and PIN; finish setup in the browser, then restart the host once more. Verify by asking *"Do you have access to TotalReclaw memory tools?"*.

### Advanced: recovery phrase in the host config (not recommended)

Use this only if you cannot open the pairing link in any browser, including on your phone. It puts your recovery phrase in plaintext in the host config file:

- Anyone or anything that can read that file (backups, sync tools, other apps on this computer) gets full access to your memories.
- The MCP server does not convert it to derived keys. The phrase stays in that file until you remove it.
- Never ask your agent to write the phrase into the file for you. Edit the file yourself.

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

When the variable is set, it takes precedence over `~/.totalreclaw/credentials.json`. To switch back to the paired account, delete the `env` block and restart the host.

> **Pin a specific version** with `args: ["-y", "@totalreclaw/mcp-server@latest"]` (stable — recommended) or `@totalreclaw/mcp-server@<specific-rc>` (e.g. `3.5.3-rc.1`) to test a particular RC. Prefer `@latest` in production: the `rc` dist-tag (`@totalreclaw/mcp-server@rc`) tracks the newest release-candidate line and can lag `latest`, so `@rc` is not necessarily newer than stable. Check what each tag resolves to: `npm view @totalreclaw/mcp-server dist-tags`.

---

## How MCP differs from OpenClaw and Hermes

| Behavior | OpenClaw plugin / Hermes | MCP server (this guide) |
|---|---|---|
| Auto-recall on every message | yes (lifecycle hook) | no — host invokes `totalreclaw_recall` when its prompt tells it to |
| Auto-extract every N turns | yes (lifecycle hook) | no — host invokes `totalreclaw_remember` when its prompt tells it to |
| Pre-compaction flush | yes | no |
| Session debrief | yes | no (use `totalreclaw_debrief` explicitly) |
| Browser account-setup flow | yes (`totalreclaw_pair` account-setup tool) | yes (`totalreclaw_pair` browser pairing — phrase generated/imported in the browser, never in chat) |
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
| **Upgrade / Account** | billing flow tools (Stripe checkout, account details) |

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

Same phrase, same memories. Switch agents without losing anything. To add TotalReclaw on another computer, install it there and ask the agent to set it up: `totalreclaw_pair` with your existing phrase (`mode: "import"`). You type the phrase into the pairing page in your browser, never into a chat or a config file. Other TotalReclaw clients on the same computer share `~/.totalreclaw/credentials.json`, so they need no extra step.

---

## Billing

- **Free tier** — 250 memories/month on Gnosis mainnet. Permanent storage. Cosine dedup (paraphrase detection). E2E encrypted. No credit card required.
- **Pro tier** — 3,000 memories/month on Gnosis mainnet. Permanent. LLM-guided dedup (catches contradictions). Custom extraction interval. Pay via the `totalreclaw_upgrade` tool or visit <https://totalreclaw.xyz/pricing>. See `totalreclaw_status` for current pricing.

Upgrade: *"Upgrade my TotalReclaw subscription."*

[Pricing](https://totalreclaw.xyz/pricing)

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent can't see TotalReclaw tools after restart**: confirm `claude mcp list` (Claude Code) or your host's MCP UI lists `totalreclaw`, then restart the host again.
- **Tools return "TotalReclaw is not configured"**: pairing hasn't finished, or the host wasn't restarted after it. Ask your agent to *"Set up TotalReclaw"* (it calls `totalreclaw_pair`), finish in the browser, then restart the host. Your host's MCP server logs show a `TotalReclaw configured` line once it worked.
- **`totalreclaw_pair` is missing, or the pairing page says "Gateway could not finish pairing"**: you may be running an older cached server (before 3.5.1 it stopped waiting for the browser after 60 seconds). Set `args` to `["-y", "@totalreclaw/mcp-server@latest"]`, restart the host, and ask for a new link.
- **`totalreclaw_pair` says TotalReclaw is already set up**: a credentials file already exists on this computer, so pairing won't replace it. If tools still say "not configured", restart the host; if they mention the keychain, the account belongs to a Hermes install that keeps its phrase in the system keychain, which the MCP server can't read yet.
- **"Not authenticated" / 401**: if you imported an existing phrase, check it — exact words, exact order. Pair again with `mode: "import"` to re-enter it.
- **First-run is slow / model download**: the embedding model is ~600 MB, downloaded once and cached. Be patient on first call.
- **Quota exceeded (403)**: free tier has a monthly memory cap. Upgrade with *"Upgrade my TotalReclaw subscription"*.
- **Recovery phrase appeared in chat**: treat it as exposed and file a bug. The leaked phrase can't be made private again once it has been through LLM context; move your memories to a new account (ask `totalreclaw_support` for help).

---

## MCP-specific notes

- **MCP is stateless.** There's no first-run welcome, no lifecycle hooks, and no auto-restart on config change. You restart the host explicitly to pick up new config.
- **One recommended setup path, two advanced ones.** The MCP server is stateless JSON-RPC — it has no HTTP server or lifecycle hooks of its own — but it still offers three setup paths:
  1. **`totalreclaw_pair` (browser flow, agent-safe — the default).** The agent calls `totalreclaw_pair`, which returns a URL + 6-digit PIN. The user opens the URL in any browser, the browser generates (or imports) the recovery phrase, encrypts it, and uploads ciphertext to the MCP server, which decrypts it in-memory and writes `~/.totalreclaw/credentials.json`. The phrase never enters LLM context, chat, stdout, or logs. After pairing, the user restarts the host so the server re-reads the credentials file and switches out of unconfigured mode.
  2. **Advanced: `npx @totalreclaw/mcp-server setup` (terminal wizard, human-run only).** The wizard generates a phrase, registers with the relay, and prints a config snippet — including the phrase — to stdout. Because that stdout would enter an agent's context, this is for the **human** to run in their own terminal; an agent must never invoke it via a shell tool.
  3. **Advanced: phrase in the host config.** The user pastes an existing TotalReclaw phrase into the host config env (`TOTALRECLAW_RECOVERY_PHRASE`) themselves. It stays there in plaintext — see [Advanced: recovery phrase in the host config](#advanced-recovery-phrase-in-the-host-config-not-recommended).

  Keeping crypto secrets out of LLM context is the through-line: `totalreclaw_pair` never exposes the phrase to the agent or writes it into a config file, and the two advanced paths are human-only for the same reason.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/claude-code-setup.md>**

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
