# Environment Variables Reference

**Applies to:** TotalReclaw v1 (all clients).
**Last updated:** 2026-04-18.

v1 deliberately removes every env var that was an internal knob pretending to be user configuration. The list below is the **complete** set of env vars that end users or deployment operators should ever need to set.

If you had one of the removed vars set (see bottom of this page), you can delete it — clients will silently ignore it.

---

## User-facing env vars

### `TOTALRECLAW_RECOVERY_PHRASE`

**Required.** Your 12-word BIP-39 recovery phrase. All encryption keys are derived from this. Never sent to any server.

```bash
export TOTALRECLAW_RECOVERY_PHRASE="your twelve word recovery phrase here"
```

**Default:** none — you must set this.
**When to override:** always set it once during setup.
**Security:** treat this like a seed phrase. Losing it means losing access to every memory you have ever stored.

### `TOTALRECLAW_SERVER_URL`

**Optional.** Override the relay server URL. Defaults to the managed service.

```bash
# Default (do not set)
# TOTALRECLAW_SERVER_URL=https://api.totalreclaw.xyz

# Self-hosted
export TOTALRECLAW_SERVER_URL="http://localhost:8080"
```

**Default:** `https://api.totalreclaw.xyz` (managed service).
**When to override:** pointing at a self-hosted server, or at the staging relay for development (`https://api-staging.totalreclaw.xyz`).

### `TOTALRECLAW_SELF_HOSTED`

**Optional.** Flag that tells the client it is talking to a self-hosted PostgreSQL server instead of the managed on-chain service.

```bash
export TOTALRECLAW_SELF_HOSTED=true
```

**Default:** `false` (managed service).
**When to override:** when running your own server.
**What changes:** dedup and forget use HTTP endpoints (not on-chain tombstones); consolidation tool is available.

### `TOTALRECLAW_CREDENTIALS_PATH`

**Optional.** Override the path where the setup wizard writes the local credentials file (smart account address, registration state).

```bash
# Default (macOS / Linux)
# ~/.totalreclaw/credentials.json
export TOTALRECLAW_CREDENTIALS_PATH="$HOME/.config/totalreclaw/creds.json"
```

**Default:** `~/.totalreclaw/credentials.json`.
**When to override:** sandboxed environments, XDG-compliant layouts, multi-tenant deployments.

### `TOTALRECLAW_CACHE_PATH`

**Optional.** Override the path to the client's encrypted on-disk cache (fact store, hot cache, etc.).

```bash
# Default
# ~/.totalreclaw/cache.enc
export TOTALRECLAW_CACHE_PATH="$HOME/.config/totalreclaw/cache.enc"
```

**Default:** `~/.totalreclaw/cache.enc`.
**When to override:** same as `TOTALRECLAW_CREDENTIALS_PATH` — sandboxed environments, XDG layouts, multi-tenant deployments.

### LLM provider keys

**Required if the client needs an LLM** (auto-extraction, LLM-guided dedup, import processing).

The client reads whichever provider key is present. It picks the LLM automatically — you no longer select a model with an env var.

| Provider | Env var |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Z.AI / GLM | `ZAI_API_KEY` |
| Groq | `GROQ_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Google Gemini | `GOOGLE_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| xAI | `XAI_API_KEY` |
| Together | `TOGETHER_API_KEY` |

**On OpenClaw:** the plugin reads OpenClaw's own provider config, no separate LLM key needed.

---

## Removed in v1

These env vars existed in earlier versions and are now silently ignored. Delete them from your config if present.

| Removed var | Why it is gone |
|---|---|
| `TOTALRECLAW_CHAIN_ID` | Chain is auto-detected from your tier. Free = Base Sepolia (84532), Pro = Gnosis mainnet (100). |
| `TOTALRECLAW_EMBEDDING_MODEL` | Harrier-OSS-v1-270M (640d) is the only supported model in v1. |
| `TOTALRECLAW_STORE_DEDUP` | Store-time near-duplicate detection is always on. |
| `TOTALRECLAW_LLM_MODEL` / `TOTALRECLAW_EXTRACTION_MODEL` | LLM is picked automatically from the available provider. Model choice is not user-tunable. |
| `TOTALRECLAW_SESSION_ID` | Sessions are computed internally. |
| `TOTALRECLAW_TAXONOMY_VERSION` | v1 is the only format. No opt-in needed. |
| `TOTALRECLAW_CLAIM_FORMAT` | Same reason — the v0 `{text, metadata}` shape is gone. |
| `TOTALRECLAW_DIGEST_MODE` | Digest behaviour is no longer user-configurable. |
| `TOTALRECLAW_AUTO_RESOLVE_MODE` | Contradiction-resolution policy is managed by the shared core. |
| `TOTALRECLAW_HOT_RELOAD` | Kept for OpenClaw plugin development only — see plugin README. |
| `TOTALRECLAW_TWO_TIER_SEARCH` | Merged into the default search path. |

---

## Advanced / self-hosted env vars

These are for deployment operators, not end users. They live in the server or relay environment, not your local shell. See:

- [Self-hosted server setup](../specs/totalreclaw/server.md) for PostgreSQL deployment env vars.
- [Monitoring setup](./monitoring-setup.md) for observability env vars.
- [Relay configuration](https://github.com/p-diogo/totalreclaw-relay) (private repo) for tier tuning, Pimlico, Stripe.

Self-hosted deployments can still set the following as env-var fallbacks — on managed service, the relay billing response delivers these and env vars are ignored:

| Env var | Purpose | Default |
|---|---|---|
| `TOTALRECLAW_COSINE_THRESHOLD` | Minimum cosine similarity to surface a result | `0.15` |
| `TOTALRECLAW_RELEVANCE_THRESHOLD` | Auto-injection relevance cutoff | `0.3` |
| `TOTALRECLAW_SEMANTIC_SKIP_THRESHOLD` | Store-time dedup similarity threshold | `0.85` |
| `TOTALRECLAW_MIN_IMPORTANCE` | Minimum extracted-fact importance to auto-store | `6` |
| `TOTALRECLAW_CACHE_TTL_MS` | Hot-cache TTL | `300000` |
| `TOTALRECLAW_TRAPDOOR_BATCH_SIZE` | Trapdoors per subgraph query | `5` |
| `TOTALRECLAW_SUBGRAPH_PAGE_SIZE` | Graph Studio page size | `1000` |
| `TOTALRECLAW_EXTRACT_INTERVAL` | Turns between auto-extractions | `3` |
| `TOTALRECLAW_DATA_EDGE_ADDRESS` | DataEdge contract address (self-hosted chain only) | Built-in |
| `TOTALRECLAW_ENTRYPOINT_ADDRESS` | ERC-4337 EntryPoint (self-hosted chain only) | v0.7 address |
| `TOTALRECLAW_RPC_URL` | Alternative RPC URL (self-hosted chain only) | Relay bundler |

See [client-consistency spec](../specs/totalreclaw/client-consistency.md) for the full resolution order.

### Internal kill-switches (not public config)

| Env var | Purpose |
|---|---|
| `TOTALRECLAW_AUTO_RESOLVE_MODE` | Internal emergency kill-switch for the auto-contradiction-resolution loop (`active` / `shadow` / `off`). Not documented to end users; reserved for incident response. |

---

## Related

- [v1 migration guide](./v1-migration.md)
- [Client setup guide](./client-setup-v1.md)
- [OpenClaw setup](./openclaw-setup.md)
- [Claude Desktop setup](./claude-code-setup.md)
- [Hermes setup](./hermes-setup.md)
