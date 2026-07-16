# TotalReclaw Skill for OpenClaw

> **End-to-end encrypted memory + knowledge graph for AI agents -- portable, yours forever.**
>
> Your AI remembers everything. Your server sees nothing.

TotalReclaw gives any [OpenClaw](https://github.com/openclaw/openclaw) agent persistent, encrypted long-term memory. Preferences, decisions, commitments, rules, and context carry across every conversation -- fully end-to-end encrypted so the server **never** sees plaintext.

**Memory Taxonomy v1**: every memory is typed (`claim` / `preference` / `directive` / `commitment` / `episode` / `summary`) and tagged with source, scope, and volatility. Recall uses source-weighted reranking so user-authored claims consistently rank above assistant-regurgitated noise. See [`docs/guides/memory-types-guide.md`](../../docs/guides/memory-types-guide.md).

## Installation

### ClawHub (recommended)

Tell your OpenClaw agent:

> "Install the TotalReclaw skill from ClawHub"

Or via terminal:

```bash
openclaw skills install totalreclaw
```

Then set one environment variable:

```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

That's it. TotalReclaw hooks into your agent automatically. The server URL defaults to `https://api.totalreclaw.xyz` (managed service) -- only set `TOTALRECLAW_SERVER_URL` if you are self-hosting. See the [env vars reference](../../docs/guides/env-vars-reference.md) for the full (short) list.

### Alternative: npm

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

---

## Why TotalReclaw?

Most AI memory solutions force a tradeoff: **good recall OR privacy**. TotalReclaw eliminates that tradeoff.

| | Recall@8 | Privacy | Encryption | Portable Export |
|---|:---:|:---:|:---:|:---:|
| **TotalReclaw (E2EE)** | **98.1%** | **100%** | XChaCha20-Poly1305 | Yes |
| Plaintext vector search | 99.2% | 0% | None | Varies |
| Mem0 (hosted) | ~95% | 0% | At-rest only | No |
| Native OpenClaw QMD | ~90% | 50% | Partial | No |

**98.1% recall with 100% privacy** -- tested against 8,727 real-world memories. The server never sees your data, yet search quality is within 1.1% of plaintext alternatives.

### Key Differentiators

- **True end-to-end encryption**: XChaCha20-Poly1305 encryption, Argon2id key derivation, HKDF-SHA256 auth. The server is cryptographically unable to read your memories.
- **Near-plaintext recall**: LSH blind indices with client-side BM25 + cosine + RRF reranking achieve 98.1% recall@8.
- **No vendor lock-in**: One-click plaintext export in JSON or Markdown. Your data is always yours.
- **Works everywhere**: Any MCP-compatible AI agent, not just OpenClaw.

---

## Features

- **End-to-End Encryption**: XChaCha20-Poly1305 ensures the server never sees plaintext memories
- **Memory Taxonomy v1**: 6 speech-act types + source / scope / volatility axes on every memory. [Learn more](../../docs/guides/memory-types-guide.md)
- **Intelligent Extraction**: G-pipeline — single merged-topic LLM call, provenance filter, comparative rescoring, volatility heuristic. v1 is the only write path.
- **Semantic Search**: LSH blind indices with client-side BM25 + cosine + RRF fusion reranking
- **Retrieval v2 Tier 1**: Source-weighted reranking — user=1.0, user-inferred=0.9, derived/external=0.7, assistant=0.55
- **Lifecycle Hooks**: Seamlessly integrates with OpenClaw's agent lifecycle (before_agent_start, agent_end, before_compaction, before_reset)
- **Natural-language overrides**: "pin that", "that was actually a rule, not a preference", "file that under work" — the agent runs the matching `tr` curation command automatically
- **Portable Export**: One-click plaintext export -- no vendor lock-in
- **Decay Management**: Automatic memory decay with configurable thresholds

---

## Quick Start

### 1. Install

Tell your OpenClaw agent:

> "Install the TotalReclaw skill from ClawHub"

Or via terminal:

```bash
openclaw skills install totalreclaw
```

Alternative (npm):

```bash
openclaw plugins install @totalreclaw/totalreclaw
```

### 2. Configure

You have three ways to set up TotalReclaw, depending on where your OpenClaw gateway runs.

**Local gateway (laptop / workstation):** run the CLI wizard on the same machine:

```bash
openclaw totalreclaw onboard
```

The wizard generates or accepts a 12-word BIP-39 TotalReclaw account key directly on your terminal. The phrase never touches the LLM, the chat transcript, or the network -- it's written straight to `~/.totalreclaw/credentials.json` (mode 0600).

**Remote gateway (VPS, home server, shared / team):** use QR-pairing (new in v3.3.0).

On the gateway host:

```bash
openclaw totalreclaw pair           # generate a new account key
openclaw totalreclaw pair import    # import an existing TotalReclaw key
```

You'll see a QR code, a 6-digit secondary code, and a URL. Scan the QR with your phone's camera or open the URL on any modern browser. The browser page:

1. Asks you to enter the 6-digit code (prevents a bystander from hijacking the session).
2. Generates or accepts your 12-word account key in-page.
3. Encrypts it end-to-end (x25519 + ChaCha20-Poly1305, key derived from a DH shared secret the relay never sees) and delivers it to your gateway.

The phrase never enters the LLM, the chat transcript, or the relay server in plaintext. The pairing URL embeds the gateway's ephemeral public key in the URL fragment -- this is TLS-MITM resistant and invisible to any server on the path. See `CHANGELOG.md` §3.3.0 for the full threat model.

Browser support: Safari 17+, Chrome 123+, Firefox 130+ (these ship WebCrypto x25519 + ChaCha20-Poly1305).

**Legacy / self-hosted:** set the env var directly (useful for containers / CI):

```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

**That's it.** v1 is the default extraction and write path. Extraction cadence, importance floor, candidate pool size, and dedup thresholds are all server-tuned via the relay's billing response -- no client env vars to set. See [env vars reference](../../docs/guides/env-vars-reference.md).

For self-hosted relays:

```bash
export TOTALRECLAW_SERVER_URL="http://your-totalreclaw-server:8080"
export TOTALRECLAW_SELF_HOSTED=true
```

### 3. Use

Once installed, TotalReclaw hooks into your agent lifecycle automatically. No code changes needed.

Your agent will:
- **Load relevant memories** before processing each message (`before_agent_start`)
- **Extract and store facts** after each turn (`agent_end`)
- **Flush all memories** before context compaction (`before_compaction`)

You can also use the tools directly in conversation:

```
"Remember that I prefer dark mode in all editors"
"What do you know about my programming preferences?"
"Forget the memory about my old email address"
"Export all my memories as JSON"
```

---

## How it works

TotalReclaw is OpenClaw's **native `kind:"memory"` provider** — it does not expose a bespoke `totalreclaw_remember` / `totalreclaw_recall` / … agent-tool surface. The shipped surface is:

- **Recall is native.** Use OpenClaw's standard `memory_search` / `memory_get` tools (the same surface the `active-memory` sub-agent uses). Relevant memories are surfaced automatically before the agent processes each message.
- **Capture is automatic.** The plugin extracts facts from the conversation in the background — you do not call a tool on every preference the user states.
- **Explicit capture + curation run through the `tr` CLI**, e.g. `tr remember "…"`, `tr pin` / `tr unpin`, `tr retype`, `tr set_scope`, `tr export`, `tr status`. The legacy `totalreclaw_*` agent tools and `tr recall` are retired — recall is `memory_search`, explicit capture is `tr remember`.

The plugin wires into the OpenClaw lifecycle automatically (`before_agent_start` → auto-recall, `agent_end` → auto-extract, `before_compaction` / `before_reset` → flush). No code changes are needed.

For the full, authoritative agent surface — install, QR pairing, the `tr` CLI, and the autonomous restart flow — see [`SKILL.md`](./SKILL.md) and the [setup guide](../../docs/guides/openclaw-setup.md).

---

## Configuration

### Environment Variables

See [`docs/guides/env-vars-reference.md`](../../docs/guides/env-vars-reference.md)
for the complete, authoritative list. The v1-launch cleanup reduced the
user-facing surface to 5 vars plus LLM provider keys. The short version:

| Variable | Required | Default | Description |
|----------|:---:|---------|-------------|
| `TOTALRECLAW_RECOVERY_PHRASE` | **Yes** | -- | 12-word BIP-39 recovery phrase (never sent to server) |
| `TOTALRECLAW_SERVER_URL` | No | `https://api.totalreclaw.xyz` | Relay URL (override for self-hosted / staging) |
| `TOTALRECLAW_SELF_HOSTED` | No | `false` | Set `true` if running against a self-hosted PostgreSQL server |
| `TOTALRECLAW_CREDENTIALS_PATH` | No | `~/.totalreclaw/credentials.json` | Credential file location |
| `TOTALRECLAW_CACHE_PATH` | No | `~/.totalreclaw/cache.enc` | Encrypted cache file location |

Tuning knobs (extraction interval, importance threshold, cosine thresholds)
now come from the relay billing response. Self-hosted operators can still
set the env-var equivalents as fallbacks — see the env vars reference.

### Configuration Sources (Priority Order)

Configuration is loaded from multiple sources. Higher priority overrides lower:

1. **Default values** -- Built-in defaults
2. **OpenClaw config** -- `agents.defaults.totalreclaw.*`
3. **Environment variables** -- `TOTALRECLAW_*`
4. **Explicit overrides** -- Passed to constructor

### OpenClaw Configuration

Add to your OpenClaw configuration file:

```json
{
  "agents": {
    "defaults": {
      "totalreclaw": {
        "serverUrl": "http://your-server:8080",
        "autoExtractEveryTurns": 3,
        "minImportanceForAutoStore": 6,
        "maxMemoriesInContext": 8,
        "forgetThreshold": 0.3
      }
    }
  }
}
```

---

## Memory Types

Memory Taxonomy v1 — every memory is one of six speech-act types, plus `source` (provenance), `scope` (life domain), and `volatility` axes:

| Type | Description | Example |
|------|-------------|---------|
| `claim` | Stated-as-true information | "User works at Acme Corp" |
| `preference` | Likes / dislikes | "User prefers dark mode" |
| `directive` | Instruction or rule | "Always run tests before commit" |
| `commitment` | Promise or obligation | "User will send the report Friday" |
| `episode` | Event or experience | "User attended PyCon 2024" |
| `summary` | Condensed context | "Project migrated onto Gnosis mainnet" |

Legacy v0 entries (`fact` / `preference` / `decision` / `episodic` / `goal`) are read-compatible and normalized to v1 on recall; v1 is the only write path. See the [memory types guide](../../docs/guides/memory-types-guide.md).

## Importance Scoring

Memories are scored on a 1-10 scale:

| Score | Level | Description |
|-------|-------|-------------|
| 1-3 | Trivial | Small talk, pleasantries |
| 4-6 | Useful | Tool preferences, working style |
| 7-8 | Important | Key decisions, major preferences |
| 9-10 | Critical | Core values, safety info |

---

## Encryption Details

All cryptographic operations are powered by [`@totalreclaw/core`](https://www.npmjs.com/package/@totalreclaw/core) -- a unified Rust/WASM module that ensures byte-for-byte consistency across all TotalReclaw clients.

TotalReclaw uses end-to-end encryption:

1. **Key Derivation**: Recovery phrase is processed through Argon2id to derive encryption keys. The phrase is never sent to the server.
2. **Encryption**: All memories are encrypted client-side using XChaCha20-Poly1305 before transmission.
3. **Search**: LSH blind indices (SHA-256 hashed) enable server-side search without exposing plaintext.
4. **Decryption**: Memories are decrypted client-side after retrieval.
5. **Authentication**: HKDF-SHA256 for authentication tokens.

The server is cryptographically unable to read your memories, embeddings, or search queries.

---

## Performance Targets

| Metric | Target |
|--------|--------|
| Search latency (p95) | < 140ms for 1M memories |
| Recall accuracy | >= 93% of true top-250 |
| Storage overhead | <= 2.2x vs plaintext |
| Extraction latency | < 500ms |

---

## Architecture

```
+-------------------+     +-------------------+     +-------------------+
|   OpenClaw Agent  |     |  TotalReclaw Skill |     | TotalReclaw Server |
+-------------------+     +-------------------+     +-------------------+
        |                         |                         |
        | before_agent_start      |                         |
        |------------------------>| recall()                |
        |                         |------------------------>|
        |                         |<------------------------|
        |<------------------------|                         |
        |                         |                         |
        | [Agent processes]       |                         |
        |                         |                         |
        | agent_end               |                         |
        |------------------------>| extract + store()       |
        |                         |------------------------>|
        |<------------------------|                         |
```

---

## Troubleshooting

### "Skill not initialized"

Call `await skill.init()` before using any methods.

### "Failed to load reranker model"

The reranker model is optional. If not found, vector scores are used as fallback.

### "Memory not found"

The fact ID may be incorrect, or the memory may have been evicted due to decay.

### Slow searches

- Ensure the TotalReclaw server is properly indexed
- Check network latency to the server
- Consider increasing `maxMemoriesInContext` for better recall

---

## Development

### Setup

```bash
git clone https://github.com/p-diogo/totalreclaw
cd totalreclaw/skill
npm install
```

### Build

```bash
npm run build
```

### Test

```bash
npm test

# With coverage
npm test -- --coverage

# Watch mode
npm run test:watch
```

### Lint

```bash
npm run lint
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit changes: `git commit -am 'Add my feature'`
4. Push to the branch: `git push origin feature/my-feature`
5. Submit a pull request

## License

MIT — see the repository LICENSE file.

## Links

- [TotalReclaw Documentation](https://github.com/p-diogo/totalreclaw)
- [Claw Hub Listing](https://clawhub.ai/skills/totalreclaw)
- [OpenClaw](https://github.com/openclaw/openclaw)
- [Issue Tracker](https://github.com/p-diogo/totalreclaw/issues)
