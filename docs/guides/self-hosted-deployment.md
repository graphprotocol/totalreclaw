# Self-hosted deployment

**Applies to:** TotalReclaw v1.
**Last updated:** 2026-07-02.

TotalReclaw's default is the **managed service** (on-chain via Gnosis + The Graph, accessed through our relay). The alternative documented here is **self-hosted mode**: you run the open-source `server/` (FastAPI + PostgreSQL) yourself, and clients talk to it over plain HTTP. The client-side E2EE pipeline is identical in both modes — **your server only ever sees ciphertext and blind indices**, never plaintext.

> **TL;DR:** run `server/` with Docker Compose, point a supported client at it with `TOTALRECLAW_SELF_HOSTED=true` + `TOTALRECLAW_SERVER_URL`, and tune knobs with client env vars. No relay, no blockchain, no billing, no quota.

---

## Client support matrix

Not every client can talk to a self-hosted server. Managed-mode clients speak the relay protocol (`/v1/subgraph`, `/v1/bundler`); self-hosted mode requires the HTTP storage protocol (`/v1/store`, `/v1/search`). Current support:

| Client | Self-hosted support | How |
|---|---|---|
| **MCP server** (`@totalreclaw/mcp-server`) | ✅ Supported | `TOTALRECLAW_SELF_HOSTED=true` switches it to HTTP mode (via `@totalreclaw/client`) |
| **Claude Desktop / Cursor / any MCP host** | ✅ Supported | Through the MCP server above |
| **NanoClaw** (`@totalreclaw/skill-nanoclaw`) | ✅ Supported | Spawns the MCP server; env vars pass through |
| **TypeScript client library** (`@totalreclaw/client`) | ✅ Supported | Calls `/v1/store`, `/v1/search`, `/v1/facts/{id}`, `/v1/sync` directly |
| **OpenClaw plugin** (`@totalreclaw/totalreclaw`) | ⚠️ Flag exists, not recently validated | `TOTALRECLAW_SELF_HOSTED=true` disables subgraph mode; the HTTP path hasn't been E2E-validated recently (client is parked) |
| **Hermes plugin** (`totalreclaw` on PyPI) | ❌ **Not supported** | The Python client speaks the managed-relay protocol only — it has no HTTP storage path. Tracked in [#364](https://github.com/p-diogo/totalreclaw/issues/364) |
| **ZeroClaw** (`totalreclaw-memory` crate) | ❌ Not supported | Same limitation (relay protocol only; client is parked) |

If you need self-hosted today, use the **MCP server** — it works with Claude Desktop, Claude Code, Cursor, Windsurf, and any other MCP-compatible host.

---

## 1. Run the server

Requirements: Docker + Docker Compose. The stack is `server/docker-compose.yml`: the FastAPI server, PostgreSQL 16, and an optional Caddy reverse proxy. All ports bind to `127.0.0.1` by default.

```bash
git clone https://github.com/p-diogo/totalreclaw.git
cd totalreclaw/server

# 1. Configure
cp .env.example .env        # set your own POSTGRES_PASSWORD

# 2. Start
docker compose up -d

# 3. Verify
curl http://localhost:8080/health
```

The schema is applied automatically (see `database/schema.sql` / `server/migrations`). For internet-facing deployments, put the server behind TLS (the bundled Caddy service, or your own proxy) and see [production-deployment.md](./production-deployment.md) for credentials-at-rest hardening on the client host.

### What the server exposes

Storage, search, and auth only — by design. Billing, relay, and on-chain proxying live in the managed service and are **not** part of the public server.

| Endpoint | Purpose |
|---|---|
| `GET /health`, `GET /ready` | Liveness / readiness |
| `POST /v1/register` | Idempotent user registration (auth-key hash + salt) |
| `POST /v1/store` | Store encrypted facts + blind indices |
| `POST /v1/search` | Trapdoor search over blind indices (returns encrypted blobs; the **client** decrypts and reranks). Accepts a per-request `max_candidates` (cap 10,000) |
| `DELETE /v1/facts/{id}` · `POST /v1/facts/batch-delete` | Delete facts (batch delete is what enables the consolidation tool) |
| `GET /v1/export` | Export all facts for an owner |
| `GET /v1/sync` | Full vault snapshot |
| `DELETE /v1/account` | Delete account + all facts |
| `GET /v1/metrics` | Per-user fact count / storage used |

There is **no** `/v1/billing/status`, no feature flags, and no server-side tuning endpoint. That's intentional — see [Configuration](#3-configure-tuning-knobs-client-env-vars).

---

## 2. Point a client at it

MCP server example (Claude Desktop / Claude Code / Cursor):

```jsonc
// MCP server config
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase here",
        "TOTALRECLAW_SELF_HOSTED": "true",
        "TOTALRECLAW_SERVER_URL": "http://localhost:8080"
      }
    }
  }
}
```

Both vars matter:

- `TOTALRECLAW_SERVER_URL` — where the server lives.
- `TOTALRECLAW_SELF_HOSTED=true` — switches the client to the HTTP storage protocol (without it, the client assumes the managed relay protocol and will fail against your server).

Registration is automatic on first use — no account creation step, no credit card, no quota.

---

## 3. Configure tuning knobs (client env vars)

On the managed service, tuning knobs (extraction interval, candidate pool, recall size, …) are delivered by the relay's billing response, so we can retune without shipping client releases. Your self-hosted server has no such endpoint — clients quietly fall back to **client-side env vars, then built-in defaults**. This degradation is graceful by design: no crash, no error, just defaults.

> **Design decision:** self-hosted configuration is **client env vars, not a server config endpoint**. Adding a `/v1/config` to the public server would recreate exactly the relay-shaped coupling self-hosting avoids. Every server-tunable knob therefore also exists as a client env var.

The knobs (full details in [env-vars-reference.md](./env-vars-reference.md)):

| Env var | Purpose | Default |
|---|---|---|
| `TOTALRECLAW_EXTRACT_INTERVAL` | Turns between auto-extractions | `3` |
| `TOTALRECLAW_MAX_FACTS_PER_EXTRACTION` | Max facts per auto-extraction batch | `15` |
| `TOTALRECLAW_RECALL_TOP_K` | Results returned per recall (after reranking) | `16` |
| `CANDIDATE_POOL_MAX_FREE` / `CANDIDATE_POOL_MAX_PRO` | Search candidate-pool size | `250` / `100` (varies by client) |
| `TOTALRECLAW_MIN_IMPORTANCE` | Min importance to auto-store a fact | `6` |
| `TOTALRECLAW_COSINE_THRESHOLD` | Min cosine similarity to surface a result | `0.15` |
| `TOTALRECLAW_SEMANTIC_SKIP_THRESHOLD` | Store-time dedup threshold | `0.85` |

Additionally, self-hosted search accepts a per-request `max_candidates` parameter on `/v1/search` — the server enforces no billing-driven pool limit.

---

## 4. What you gain / what you give up

**Gain vs managed:**
- Full data custody — the (encrypted) vault lives in your PostgreSQL.
- No quota (managed free tier is 250 memories/month) and no billing.
- **Bulk consolidation tool** — works self-hosted only, because it needs batch delete, which has no on-chain equivalent.
- HTTP `DELETE` forget (immediate), instead of on-chain tombstones.

**Give up vs managed:**
- On-chain permanence + The Graph indexing (your availability = your server's availability).
- Server-side knob tuning (use the env vars above).
- Quota warnings, Pro features delivered via billing flags, Stripe upgrade flow — all absent; clients skip them silently.
- Hermes and ZeroClaw as clients (see the support matrix above).

**Identical in both modes:** the E2EE pipeline — XChaCha20-Poly1305 encryption, LSH bucketing, SHA-256 blind indices, client-side embedding (Harrier ONNX 640d) and reranking. The server is blind either way.

---

## Related

- [env-vars-reference.md](./env-vars-reference.md) — complete env var list
- [production-deployment.md](./production-deployment.md) — credentials-at-rest hardening (LUKS, external secret managers)
- [`docs/specs/totalreclaw/server.md`](../specs/totalreclaw/server.md) — server spec
- [feature-comparison.md](./feature-comparison.md) — full feature matrix
- Issue [#364](https://github.com/p-diogo/totalreclaw/issues/364) — self-hosted parity tracking (incl. the Hermes gap)
