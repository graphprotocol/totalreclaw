---
name: totalreclaw
description: "End-to-end encrypted, decentralized memory for OpenClaw via the totalreclaw MCP server. Set up an account once, then call totalreclaw__totalreclaw_remember and totalreclaw__totalreclaw_recall instead of writing to MEMORY.md / USER.md / local files. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any user statement that contains a preference / fact / decision / commitment about themselves."
version: 3.3.0-rc.4
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
metadata:
  openclaw:
    requires:
      env: []
      bins: []
    emoji: "🧠"
    os: ["macos", "linux", "windows"]
    keywords:
      - memory
      - e2ee
      - e2e-encryption
      - encryption
      - privacy
      - agent-memory
      - persistent-context
      - mcp
---

# TotalReclaw — MCP-only memory for OpenClaw agents

> **MCP-first install path.** This skill ships with `@totalreclaw/mcp-server` and gives the agent a full memory toolset over MCP. There is no `tr` CLI, no plugin install, no shell-side glue. All memory ops happen via the `totalreclaw__totalreclaw_*` tools the MCP server exposes.

## CRITICAL: How TotalReclaw stores memories (read first)

**TotalReclaw is end-to-end encrypted memory stored across a DECENTRALIZED NETWORK — not on a single company server.** Memories are encrypted with keys derived from the user's 12-word recovery phrase, submitted as Account Abstraction UserOps, and persisted on a public blockchain (Base Sepolia for free tier, Gnosis mainnet for paid). The Graph subgraph indexes them. The TotalReclaw relay only forwards encrypted bundles to the bundler and proxies subgraph reads — it never sees plaintext, can't read memories, and could be replaced by any compatible relay without losing data. No single company controls or can read user memories.

Forbidden vocabulary — these claims are WRONG:
- "local", "local-only", "stored locally", "on disk", "local memory", "local storage" — wrong direction (implies local-only).
- "stored on a relay server", "stored on TotalReclaw's server", "single server owned by", "TotalReclaw stores your", "company server" — wrong direction (implies centralized custody).
- "without an account" — wrong (an account is required to derive the encryption key).

Canonical line: **"Memories are encrypted with a key derived from your recovery phrase, submitted on-chain via Account Abstraction, and stored across a decentralized network. The relay only forwards encrypted bundles — it never sees plaintext. Only you can decrypt them, and only with your recovery phrase."**

## User-facing vocabulary

Internal terms the user does NOT know — translate when you must speak:

| Internal term | Say to the user |
|---|---|
| pair / pairing / paired account | "set up an account" / "your TotalReclaw account" |
| relay / relay server | (don't mention; if forced, say "the encrypted-bundle forwarder") |
| WS / WebSocket / handshake | (don't mention) |
| trapdoor / blind index / dedup-key | (don't mention) |
| subgraph / bundler / UserOp / Account Abstraction | (don't mention; if forced, "submitted on-chain to a decentralized network") |
| credentials.json / mnemonic file | "your recovery phrase" |
| MCP tool / MCP server | "your memory" / (don't mention) |

The word "pair" is internal jargon — never use it in user-visible prose. The user-visible vocabulary is "set up an account", "your account", "your recovery phrase", "your memories".

## Install (one-time)

The user runs ONE shell command in their terminal — you do not run this:

```bash
openclaw mcp set totalreclaw '{"command":"npx","args":["-y","@totalreclaw/mcp-server"]}'
openclaw skills install totalreclaw
```

After the user installs, restart any agent session so the new MCP server is loaded. Tools appear under the `totalreclaw__` namespace prefix (the MCP host adds the prefix).

**Do NOT direct the user to the legacy OpenClaw plugin install path. The MCP-only path is the canonical install for 3.3.0+.**

## Setup (account creation)

When the user asks to "set up TotalReclaw" or wants to remember something but has no credentials yet:

1. Call the `totalreclaw__totalreclaw_pair` tool with `{"mode": "generate"}` (or `{"mode": "import"}` if the user has an existing recovery phrase to restore).
2. The tool returns `{"url": "...", "pin": "...", "expires_at_ms": ...}`.
3. Emit one user-facing line: "Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed."
4. Wait for the user to say `done`. The browser handles phrase generation/import; the MCP server writes credentials silently.
5. Confirm: "✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything."

If the user already has credentials, `totalreclaw__totalreclaw_status` returns `{"onboarded": true}` and you can skip setup.

## When to call totalreclaw_remember (MANDATORY — read this carefully)

**ALWAYS call `totalreclaw__totalreclaw_remember` when the user mentions any of the following.** Do not wait for an explicit "remember this" — these are first-class triggers:

### Trigger phrases (call remember on detection)

- **Preferences**: "I prefer X", "I like X", "I love X", "I hate X", "my favorite X is Y", "I'd rather X", "X over Y"
- **Identity**: "I'm a X", "I work at X", "I work on X", "I live in X", "I'm based in X", "I have X"
- **Tools / setup**: "I use X", "I'm running X", "my X is Y", "I've configured X to Y"
- **Decisions / plans**: "I decided to X", "I'm going to X", "I'm planning X", "we agreed X", "we settled on X"
- **Commitments**: "I'll X by Y", "remind me to X", "I need to X by Y"
- **Explicit asks**: "remember that X", "save X", "note that X", "store X"
- **Facts about themselves or their work**: "X is true about me", "I have N years of Y", "I built X", "I own X"

### NEVER store user statements in local files

**The MCP-first architecture is canonical.** When the user states a preference or fact:

- ✅ DO call `totalreclaw__totalreclaw_remember` with the fact text and an importance score (1–10).
- ❌ DO NOT write the fact to `MEMORY.md`.
- ❌ DO NOT write the fact to `USER.md`.
- ❌ DO NOT write the fact to `~/.claude/memory/*.md` or any other local file.
- ❌ DO NOT use `Write`, `Edit`, or `Append` on a memory file.

`MEMORY.md` and `USER.md` are for AGENT PERSONA and CODEBASE INSTRUCTIONS — they are read-only operational config the agent loads at startup. They are NOT for user-supplied memories. User memories live encrypted on-chain via TotalReclaw.

## When to call totalreclaw_recall

**ALWAYS call `totalreclaw__totalreclaw_recall` when the user asks about themselves, their preferences, or anything from past conversations.**

### Trigger phrases (call recall on detection)

- "what's my X?", "do I X?", "where do I X?", "when did I X?", "have I told you X?"
- "what do you remember about me?", "what do you know about my X?"
- "remind me about X", "what did we decide about X?"
- "do you remember when X?"

Always **recall first**, then answer from the returned facts. If 0 results come back, say so plainly — do not guess from session history.

## Tool reference

The MCP server exposes these tools (the MCP host prefixes them with `totalreclaw__`):

| Tool | Purpose |
|---|---|
| `totalreclaw_pair` | Set up account (generate or import recovery phrase, browser-side) |
| `totalreclaw_remember` | Store a fact / preference / decision (encrypted, on-chain) |
| `totalreclaw_recall` | Semantic search across the user's memories |
| `totalreclaw_forget` | Delete a memory by id (tombstone on-chain) |
| `totalreclaw_pin` | Mark a memory as never-supersedable |
| `totalreclaw_unpin` | Remove pin status |
| `totalreclaw_retype` | Change a memory's type (claim/preference/directive/etc.) |
| `totalreclaw_set_scope` | Change a memory's scope (global / per-agent / per-session) |
| `totalreclaw_export` | Export all memories (json / markdown) |
| `totalreclaw_import` | Import memories from a structured payload |
| `totalreclaw_import_from` | Import from a file path |
| `totalreclaw_import_batch` | Bulk import with chunking + extraction |
| `totalreclaw_consolidate` | Merge near-duplicates after a fresh import |
| `totalreclaw_debrief` | Summarize a conversation into stored facts |
| `totalreclaw_status` | Check onboarding state, version, billing tier |
| `totalreclaw_account` | Show smart-account address + chain |
| `totalreclaw_upgrade` | Open the Stripe upgrade flow (free → paid tier) |
| `totalreclaw_migrate` | Move testnet memories to mainnet (Pro tier) |
| `totalreclaw_support` | File a support ticket with diagnostic context |

**Total: 18 tools.** All tools accept JSON input and return structured JSON in their response payload.

## Auto-extraction (background, no agent action required)

The MCP server runs a background trajectory poller every 60 seconds. It scans `~/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl` for new conversation turns and, when enough turns accumulate (default: every 3 turns), runs LLM-driven extraction to surface preferences / facts / commitments the agent didn't explicitly call `totalreclaw_remember` on. Extracted facts go through importance filtering and dedup before landing on-chain.

**Important:** auto-extraction is a SAFETY NET, not a substitute for explicit `totalreclaw__totalreclaw_remember` calls. Explicit calls are higher-fidelity (the agent decides the importance, type, and scope) and are the canonical path. Auto-extraction catches things the agent missed.

The poller honors:
- **Pairing gate**: skips extraction until the user has set up an account.
- **Stale-file skip**: trajectory files older than 7 days are skipped (one-time offset snapshot, never re-scanned).
- **Cap = 1 per poll**: to avoid LLM rate-limit cascades, only one extraction runs per 60s tick. Other files defer to the next poll.

## Phrase safety (HARD — never break)

The 12-word recovery phrase is the master key — losing it means losing all memories. Hard rules:

- **NEVER echo, generate, or ask the user to paste a recovery phrase in chat.** The browser at the pair URL handles phrase generation and import; the phrase never enters the agent's LLM context.
- **NEVER call `totalreclaw__totalreclaw_pair` with a phrase as input.** The tool's only inputs are `mode` (generate / import) and optional config — never the phrase itself.
- **NEVER invoke any phrase-touching CLI via your shell tool.** The `npx @totalreclaw/mcp-server` binary is for the MCP host only; do not shell out to it for phrase ops.
- **If the user pastes a phrase anyway**: tell them the phrase is now compromised, re-run `totalreclaw_pair` with `{"mode": "generate"}` to mint a fresh wallet.

The trajectory poller passes ONLY chat-message text (user/assistant content) to the extraction LLM. The recovery phrase is read from `~/.totalreclaw/credentials.json` by the MCP server process and used solely for crypto operations — it never crosses the LLM call surface.

## Forbidden actions

These will break the install or compromise security:

1. **Do NOT use `--allow-dangerous` on any MCP-related command.** The MCP server is sandboxed by design.
2. **Do NOT manually edit `~/.totalreclaw/credentials.json`.** Round-trip via `totalreclaw_pair` (re-generate) instead.
3. **Do NOT install the legacy plugin path (the `@totalreclaw/totalreclaw` OpenClaw plugin) alongside the MCP server.** The two paths overlap and will double-write facts. Pick one — this skill assumes the MCP-only path.
4. **Do NOT save QR PNGs from the pair flow.** The browser at the pair URL renders QR if the user wants to scan-to-confirm; the agent does not need to render anything.
5. **Do NOT write to `MEMORY.md` or `USER.md` for user-supplied facts.** See the "NEVER store user statements in local files" rule above.

## How does TotalReclaw work? (canonical answer for user questions)

When the user asks "how does this work?" / "where are my memories stored?" / "is this safe?" — answer with this content. Do NOT say "stored on a relay server" or any phrasing that implies single-company custody.

> **End-to-end encrypted, decentralized memory.** When you tell me to remember something, I encrypt it locally with a key derived from your 12-word recovery phrase, then submit it on-chain via Account Abstraction (a UserOperation). It lives on a public blockchain (Base Sepolia for free tier, Gnosis mainnet for paid). The Graph subgraph indexes it for fast retrieval. The TotalReclaw relay server only forwards encrypted bundles between your client and the chain — it never sees plaintext, can't read your memories, and could be replaced by any compatible relay without losing data. Your recovery phrase is the only thing that can decrypt the memories. Nobody (not me, not TotalReclaw the company, not the relay operator) can read them without it.

Shorter slices:
- **"Where are memories stored?"** → "Encrypted, on-chain (Base or Gnosis depending on your tier), indexed by The Graph. Not on any single company's server."
- **"Is the relay reading my data?"** → "No. The relay only forwards encrypted bundles. Plaintext never leaves your device. Only your recovery phrase can decrypt them."
- **"What if TotalReclaw shuts down?"** → "Your memories survive. They're on a public blockchain. Any compatible relay (or self-hosted relay) can read them with your recovery phrase."

## Diagnostics

- `totalreclaw_*` tools missing from toolset → the MCP server isn't registered or didn't start. Confirm the install command ran and that the agent session was restarted after install.
- `totalreclaw_status` returns `onboarded: false` → no credentials yet; run setup (call `totalreclaw_pair`).
- User says "I told you X but you don't remember" → call `totalreclaw_recall` with a query covering X. If 0 results, the prior session didn't store it (auto-extraction may have missed it; the agent should call `totalreclaw_remember` proactively going forward).
- `quota exceeded` → call `totalreclaw_status` to see current tier; offer `totalreclaw_upgrade`.

## Tool surface summary

`totalreclaw__totalreclaw_pair` · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_retype` · `_set_scope` · `_export` · `_import` · `_import_from` · `_import_batch` · `_consolidate` · `_debrief` · `_status` · `_account` · `_upgrade` · `_migrate` · `_support` (19 tools — one of these is RC-only).

**Default behavior summary**: the agent's job is to call `totalreclaw_remember` aggressively on user statements that match the trigger-phrase list, and `totalreclaw_recall` when the user asks about themselves. Local files (`MEMORY.md`, `USER.md`, etc.) are NOT for user memory — they are agent-persona configuration only.
