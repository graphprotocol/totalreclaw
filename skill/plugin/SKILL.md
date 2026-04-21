---
name: totalreclaw
description: "End-to-end encrypted memory for AI agents — portable, yours forever. XChaCha20-Poly1305 E2EE: server never sees plaintext."
version: 3.3.1-rc.1
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
---

# TotalReclaw Skill

## Installation

TotalReclaw ships as an OpenClaw plugin. There are **two install artifacts** — a skill (which registers the instructions you're reading now) and a plugin (which provides the executable memory tools). Both are needed, and you install them in this order:

```sh
# 1. Install the skill (registers instructions + tool surface)
openclaw skills install totalreclaw

# 2. Install the plugin (activates routes, hooks, and tool implementations)
openclaw plugins install @totalreclaw/totalreclaw

# 3. Restart the gateway for the plugin to bind HTTP routes + hooks
#    On native install:
openclaw restart
#    Inside Docker:
docker restart openclaw-qa
```

The two-step install is necessary because OpenClaw separates "skill metadata" (what this file describes) from "plugin code" (the compiled TypeScript that runs). Skipping step 2 leaves the user with instructions but no tools. Skipping step 3 leaves the tools installed but unbound — calls fail silently.

Some OpenClaw versions support a single-step `openclaw plugins install @totalreclaw/totalreclaw` that auto-registers the skill; if so, you can skip step 1.

---

## Recovery Phrase — CRITICAL SAFETY RULES

### RULE 1 — Never generate, display, or transmit a recovery phrase in chat.

The recovery phrase is the **only** key to the user's encrypted memories. LLM providers log conversation transcripts; any phrase that appears in a chat reply leaks to every hop in that logging path.

**You MUST NOT:**
- Produce a 12-word phrase yourself and paste it into the chat. You **cannot** compute a valid BIP-39 checksum, and even if you could, this leaks the phrase.
- Ask the user to paste their recovery phrase into the chat. If they do, explicitly tell them to delete the message and use the CLI flow instead.
- Display a phrase that another tool returned via chat output.

The phrase lives **only** in the user's terminal and in `~/.totalreclaw/credentials.json` (mode 0600).

### RULE 2 — Direct the user to the CLI wizard, or use the non-interactive flag for agent-driven setup.

There are exactly two correct onboarding paths:

**A. Interactive (human at a TTY):**

```sh
openclaw totalreclaw onboard
```

This runs a terminal wizard. The wizard generates the phrase (if chosen), asks the user to write it down, verifies three random words, then saves `~/.totalreclaw/credentials.json`. The phrase never leaves the user's terminal.

**B. Agent-driven (Claude / another AI agent setting up TotalReclaw for the user):**

```sh
openclaw totalreclaw onboard --non-interactive --json --mode generate
```

Returns structured JSON: `{"ok": true, "action": "generate", "scope_address": "0x...", "credentials_path": "..."}`.

The phrase is **not** in the payload. It was written to `credentials_path` (mode 0600). Tell the user: "Your recovery phrase is at `~/.totalreclaw/credentials.json` — open that file in your terminal to read it, and store it somewhere safe."

For restore:

```sh
openclaw totalreclaw onboard --non-interactive --json --mode restore --phrase "word1 word2 ..."
```

### RULE 3 — Remote gateways use QR pairing, not phrase paste.

If the user is running OpenClaw on a VPS, Docker host, home server, or anywhere you can't see the terminal, run:

```sh
openclaw totalreclaw pair generate
# or for agent-driven:
openclaw totalreclaw pair generate --json
```

The CLI prints (or emits JSON with) a QR code, a URL, and a 6-digit PIN. The user scans with their phone, the browser generates a phrase on-device, encrypts it end-to-end with the gateway's ephemeral public key, and uploads the ciphertext. The phrase never touches chat, the LLM, or the relay.

---

## Tools

Every tool below is available once onboarding is complete (credentials file exists + state = active) AND the gateway has been restarted post-install. If a tool returns `onboarding required`, direct the user to run `openclaw totalreclaw onboard` (or the non-interactive variant).

### totalreclaw_remember

Store a new fact or preference in long-term memory.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| text | string | Yes | The fact or information to remember |
| type | string | No | Type of memory: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`. Default: `claim` |
| importance | integer | No | 1-10. Default: auto-detected by extraction LLM |

**Returns:** `{ factId, status: "stored", importance, encrypted: true }`

### totalreclaw_recall

Search and retrieve relevant memories from long-term storage.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | Yes | Natural language query |
| k | integer | No | Results to return. Default 8, max 20 |

**Returns:** `{ memories: [{ id, text, type, importance, score }], count }`

### totalreclaw_forget

Soft-delete a specific fact.

**Parameters:** `{ factId: string }` — the UUID of the fact to delete.

### totalreclaw_pin

Pin a memory so auto-resolution can never supersede it. Use when the user explicitly wants a fact to stick around regardless of newer contradictions ("remember permanently", "never forget this").

**Parameters:** `{ factId: string, reason?: string }`

### totalreclaw_unpin

Remove a pin, returning the memory to normal decay / resolution.

**Parameters:** `{ factId: string }`

### totalreclaw_retype

Change the v1 taxonomy type of an existing memory (e.g. reclassify a misdetected `claim` as a `preference`).

**Parameters:** `{ factId: string, newType: "claim"|"preference"|"directive"|"commitment"|"episode"|"summary" }`

### totalreclaw_set_scope

Set the memory scope — `personal` (private to this user) or `shared` (available to delegates).

**Parameters:** `{ factId: string, scope: "personal"|"shared" }`

### totalreclaw_export

Export all memories in plaintext.

**Parameters:** `{ format?: "json"|"markdown" }` — default `json`

### totalreclaw_status

Check billing + subscription status.

**Parameters:** `{}` (no arguments)

**Returns:** `{ tier, quota, usage, resetsAt, upgradeUrl? }`

### totalreclaw_upgrade

Get a Stripe checkout URL to upgrade to Pro (unlimited memories on Gnosis mainnet).

**Parameters:** `{}`

### totalreclaw_migrate

Migrate testnet (Base Sepolia) memories to mainnet (Gnosis) after upgrading to Pro.

**Parameters:** `{ confirm?: boolean }` — dry-run by default; set `confirm: true` to execute.

### totalreclaw_import_from

Import memories from other agent-memory tools (Mem0, MCP Memory Server, etc.).

**Parameters:** `{ source, api_key?, source_user_id?, content?, file_path?, namespace?, dry_run? }`

### totalreclaw_consolidate

Scan all memories and merge near-duplicates.

**Parameters:** `{ dry_run?: boolean }`

---

## When to Use Each Tool

### totalreclaw_remember

Use when:
- The user explicitly asks you to remember something ("remember that...", "note that...", "don't forget...")
- You detect a significant preference, decision, or fact useful in future conversations
- The user corrects or updates previous information about themselves
- You observe important context about the user's work, projects, or preferences

Do NOT use for:
- Temporary info only relevant to the current turn
- Things the user explicitly says are temporary
- Generic knowledge that isn't user-specific

### totalreclaw_recall

Use when:
- The user asks about their past preferences, decisions, or history
- You need context about their projects, tools, or working style
- The user asks "do you remember..." or "what did I tell you about..."
- You're unsure about a preference and want to check before assuming
- Starting a new conversation to load relevant context

Do NOT use for:
- Every single message — use sparingly, at most once per conversation start or when explicitly relevant
- General knowledge questions unrelated to the user

### totalreclaw_pin / totalreclaw_unpin

Use `pin` when the user says something like "remember this permanently", "always keep this", or "this is important — don't forget". Use `unpin` when they say "you can forget that", "it's no longer relevant", etc.

### totalreclaw_set_scope

Use when the user indicates a memory should be shared with delegates ("share this with my team", "make this visible to everyone I work with") or scoped back to personal ("only for me", "private").

---

## Configuration

All configuration lives under `plugins.entries.totalreclaw.config.*` in the OpenClaw config. The full 3.3.1 schema:

```yaml
plugins:
  entries:
    totalreclaw:
      config:
        # Public URL for QR pairing (optional — auto-detected if Tailscale or LAN)
        publicUrl: https://gateway.example.com:18789

        # Extraction tuning (all optional)
        extraction:
          enabled: true                       # default true
          interval: 3                         # turns between auto-extractions
          maxFactsPerExtraction: 15           # hard cap per turn
          model: glm-4.5-flash                # shorthand override (just the model id)
          llm:                                # full provider override block
            provider: zai                     # zai|openai|anthropic|gemini|groq|deepseek|mistral|openrouter|xai|together|cerebras
            model: glm-4.5-flash
            apiKey: <your-key>
            baseUrl: https://api.z.ai/api/coding/paas/v4   # self-hosted / custom gateway only
```

### LLM Provider Auto-Resolution

TotalReclaw needs a small LLM to extract facts from conversations. Resolution order (highest priority first):

1. **Plugin config** — `plugins.entries.totalreclaw.config.extraction.llm.{provider,apiKey}`
2. **OpenClaw provider config** — `api.config.models.providers`
3. **OpenClaw auth profiles** — keys stored in `~/.openclaw/agents/<agent>/agent/auth-profiles.json`. This is where most users have their provider keys; 3.3.1 added it as a resolution tier.
4. **Environment variables** — `ZAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, `TOGETHER_API_KEY`, `CEREBRAS_API_KEY`

If none of these resolve, auto-extraction is cleanly disabled and a single INFO message is logged at startup — manual `totalreclaw_remember` still works.

### QR Pairing URL Resolution

For `openclaw totalreclaw pair generate`, the gateway's externally-reachable URL is resolved in this order:

1. `plugins.entries.totalreclaw.config.publicUrl` — explicit override
2. `gateway.remote.url` — OpenClaw's own remote-gateway URL
3. `gateway.bind === 'custom'` + `gateway.customBindHost`
4. Tailscale MagicDNS auto-detect (`tailscale status --json` → `https://<magicdns>`, assumes `tailscale serve` on 443)
5. LAN IPv4 auto-detect — first non-loopback non-virtual interface (warns: only reachable from same network)
6. `http://localhost:<port>` fallback (warns: only works on this machine)

---

## Security

1. **E2EE** — all memories are encrypted client-side with XChaCha20-Poly1305. The server never sees plaintext.
2. **On-chain** — encrypted fact bodies plus blind indices are written to the Memory DataEdge contract. Free tier = Base Sepolia (84532); Pro tier = Gnosis mainnet (100).
3. **Recovery phrase stays local** — it lives only in `~/.totalreclaw/credentials.json` with mode 0600 and in the user's own backup. Never in chat, never in the session transcript, never in an LLM request.
4. **QR pairing crypto** — gateway ephemeral x25519 keypair; browser derives shared secret and encrypts the phrase with ChaCha20-Poly1305 before upload. Gateway private key never leaves disk.

### What NOT to do

- Do NOT write facts or preferences to `MEMORY.md`. TotalReclaw handles all memory storage with E2EE; cleartext files defeat the encryption guarantee.
- Do NOT call `totalreclaw_remember` for temporary or in-session context.
- Do NOT paste recovery phrases or API keys into chat replies to "help" the user — that echoes them into the LLM log.

---

## Memory Types (v1 Taxonomy)

TotalReclaw v1 uses six canonical types:

| Type | Description | Example |
|------|-------------|---------|
| claim | Objective assertion about the user / world | "Lives in Lisbon, Portugal" |
| preference | Likes, dislikes, choices | "Prefers dark mode in all applications" |
| directive | Instruction the user gave to remember / enforce | "Always use TypeScript for new projects" |
| commitment | Promise or commitment the user made | "Will deploy v1 to mainnet by end of Q1" |
| episode | Notable event or experience | "Deployed v1.0 to production on March 15" |
| summary | Key outcomes from discussions | "Agreed to use phased rollout for mainnet migration" |

The extraction LLM auto-selects the type. Use `totalreclaw_retype` if you detect a classification error.

---

## Troubleshooting

- **`plugins.allow is empty`** — OpenClaw warning, not a TotalReclaw bug. Either add the plugin to your allowlist or ignore it; TotalReclaw still works.
- **`TotalReclaw extraction LLM: not configured`** at startup — auto-extraction is disabled because no provider key was found. Configure a provider in `~/.openclaw/agents/<agent>/agent/auth-profiles.json`, or set `plugins.entries.totalreclaw.config.extraction.llm.{provider,apiKey}`. Manual `totalreclaw_remember` still works.
- **Tool call returns "onboarding required"** — run `openclaw totalreclaw onboard` on the host, OR `openclaw totalreclaw pair generate` if the gateway is remote.
- **`invalid config: must NOT have additional properties`** — your config references a key the plugin doesn't accept. The 3.3.1 schema is listed above; earlier schemas rejected `publicUrl` and most `extraction.*` keys (fixed in 3.3.1).
- **Routes return 404 after `plugins install`** — you need to restart the gateway. `openclaw restart` or `docker restart openclaw-qa`.

---

## Plugin architecture (informational)

- `index.ts` — plugin entry; registers tools, hooks, CLI, HTTP routes, and the slash command `/totalreclaw`.
- `llm-client.ts` + `llm-profile-reader.ts` — LLM auto-resolution cascade (3.3.1).
- `gateway-url.ts` — Tailscale / LAN host autodetect for pairing URLs.
- `pair-http.ts` — `/plugin/totalreclaw/pair/{finish,start,respond,status}` HTTP routes.
- `pair-cli.ts` — `openclaw totalreclaw pair [generate|import]` CLI, with `--json` and `--timeout` in 3.3.1.
- `onboarding-cli.ts` — `openclaw totalreclaw onboard` CLI, with `--non-interactive / --json / --mode / --phrase / --emit-phrase` in 3.3.1.
- `config.ts` — centralized env-var reads (keeps scanner surface clean).

See `CHANGELOG.md` for the per-release fix history.
