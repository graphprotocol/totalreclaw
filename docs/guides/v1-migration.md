# Upgrading to TotalReclaw v1

**Target audience:** existing TotalReclaw users on any client (OpenClaw, MCP, NanoClaw, Hermes, ZeroClaw).
**Version landing in:** core 2.0.0, plugin 3.0.0, mcp-server 3.0.0, nanoclaw 3.0.0, python 2.0.0, ZeroClaw (`totalreclaw-memory`) 2.0.0.
**TL;DR:** v1 is a bigger + cleaner memory model. Your wallet, recovery phrase, memories, and privacy model do not change. Upgrade the package and you are done.

---

## What is v1?

v1 is the first cross-client memory taxonomy shipped by TotalReclaw: **six speech-act-grounded memory types** plus **three orthogonal axes** (source, scope, volatility) that fix the "everything is just a fact" pathology in every prior memory system.

The short version:

- Memory types collapse from 8 ambiguous categories to **6 clean ones**: `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`.
- Every memory now carries **provenance** (who said it: user, user-inferred, assistant, external, derived).
- Every memory now carries **scope** (work, personal, health, family, creative, finance, misc, unspecified).
- Every memory now carries **volatility** (stable, updatable, ephemeral).
- Retrieval uses `source` to rank results so user-authored claims consistently win over assistant-regurgitated noise.

See the full spec in [`docs/specs/totalreclaw/memory-taxonomy-v1.md`](../specs/totalreclaw/memory-taxonomy-v1.md) and the retrieval change in [`docs/specs/totalreclaw/retrieval-v2.md`](../specs/totalreclaw/retrieval-v2.md).

---

## What stays the same

Nothing about TotalReclaw's security or portability model changes.

- **E2E encryption** — XChaCha20-Poly1305, keys derived from your 12-word recovery phrase. Server-blind.
- **Recovery phrase** — same phrase keeps working. No re-registration, no re-derivation.
- **Wallet / Smart Account address** — same deterministic address across chains.
- **Storage** — same DataEdge contract, same subgraph, same chains (Base Sepolia free, Gnosis Pro).
- **Plain-text export** — same one-click JSON/Markdown export tools.
- **Import adapters** — ChatGPT, Claude, Gemini, Mem0, MCP Memory all continue to work.

---

## What changed

### 1. Memory types — from 8 to 6

The old 8-type list (`fact`, `preference`, `decision`, `episodic`, `goal`, `context`, `summary`, `rule`) mixed three different things together (content class, temporal scope, and operation). v1 splits them cleanly.

| v0 type | v1 equivalent | Notes |
|---|---|---|
| `fact` | `claim` | descriptive statements ("I live in Lisbon") |
| `context` | `claim` with `volatility: ephemeral` | short-lived context now expressed via volatility axis |
| `decision` | `claim` with `reasoning` populated | reasoning moves to its own field |
| `preference` | `preference` | unchanged (expressive speech act) |
| `rule` | `directive` | renamed (imperative: "always do X") |
| `goal` | `commitment` | renamed (commissive: "I will do X") |
| `episodic` | `episode` | renamed |
| `summary` | `summary` | unchanged — restricted to source in `{derived, assistant}` |

All clients do the v0 → v1 mapping automatically when reading existing memories. You do not need to rewrite anything.

### 2. Three new axes every memory carries

- **source** — who authored the memory
  - `user` — user explicitly said it (highest recall weight)
  - `user-inferred` — extractor inferred it from user signals
  - `assistant` — assistant authored it (heavily down-weighted at recall)
  - `external` — imported from another system
  - `derived` — computed (digests, debrief, consolidation)
- **scope** — life domain the memory belongs to (work, personal, health, family, creative, finance, misc, unspecified)
- **volatility** — how long the memory is expected to stay true (stable, updatable, ephemeral)

### 3. New MCP tools

Three new tools (on `@totalreclaw/mcp-server@3.0.0` + inherited by NanoClaw + IronClaw):

- `totalreclaw_pin` — lock a memory against auto-supersession.
- `totalreclaw_retype` — change a memory's type (e.g. "that is actually a directive, not a preference").
- `totalreclaw_set_scope` — assign a memory to a scope (work, personal, health, ...).

These are invoked via natural language: "pin that", "that was actually a rule, not a preference", "file that under work". The host LLM picks the right tool.

See the [memory types guide](./memory-types-guide.md) for examples.

### 4. Source-weighted reranking (Retrieval v2 Tier 1)

Recalls now respect `source` when ranking. User-authored claims consistently beat assistant-regurgitated restatements, which structurally solves the "97.8% junk" problem reported in other memory systems.

Full detail in [`docs/specs/totalreclaw/tiered-retrieval.md`](../specs/totalreclaw/tiered-retrieval.md).

### 5. Protobuf v4 outer wrapper

The on-chain wire format bumps from v3 to v4 to signal that the encrypted blob now contains v1 JSON (not v0 binary). Subgraph schema is unchanged — no re-indexing required.

Details: [`totalreclaw-internal/docs/plans/2026-04-18-protobuf-v4-design.md`](https://github.com/p-diogo/totalreclaw-internal/) (internal).

### 6. Environment variables removed

Six env vars that were internal/experimental knobs are gone. If you had them set they are now silently ignored:

- `TOTALRECLAW_CHAIN_ID` — chain is auto-detected from billing tier.
- `TOTALRECLAW_EMBEDDING_MODEL` — Harrier 640d is the default, no alternatives shipped.
- `TOTALRECLAW_STORE_DEDUP` — always on (best-match near-duplicate detection at core).
- `TOTALRECLAW_LLM_MODEL` — LLM picked automatically from your provider's default.
- `TOTALRECLAW_SESSION_ID` — computed internally.
- `TOTALRECLAW_TAXONOMY_VERSION` — v1 is the only format.
- `TOTALRECLAW_CLAIM_FORMAT` — same reason.
- `TOTALRECLAW_DIGEST_MODE` / `TOTALRECLAW_AUTO_RESOLVE_MODE` — internal behaviour no longer user-configurable.

See the [env vars reference](./env-vars-reference.md) for the current short list.

---

## Breaking changes summary

For application developers using the libraries directly (not via a host agent):

- `@totalreclaw/core@2.0.0` — `rerank()` preserved for back-compat; new `rerankWithConfig()` is the v1 entry point (source-weighted).
- `@totalreclaw/totalreclaw@3.0.0` (plugin) — `buildCanonicalClaim` always emits v1 JSON. No legacy fallback.
- `@totalreclaw/mcp-server@3.0.0` — tool schemas gain v1 fields; 4 new tools (pin / unpin / retype / set_scope).
- `totalreclaw@2.0.0` (Python) — `VALID_MEMORY_TYPES` is now the 6-item v1 list; `ExtractedFact` carries `source`, `scope`, `volatility`, `reasoning`.
- `totalreclaw-memory@2.0.0` (Rust/ZeroClaw) — new `store_v1(V1StoreInput)` method and `Memory::store_v1()` trait method.

If you write code against these APIs, review the per-package CHANGELOG. If you use TotalReclaw via a host agent (OpenClaw, Claude Desktop, Cursor, etc.) — no code change on your side.

---

## Action required

**For users on managed service (default):** nothing. Upgrade your client package, continue using memory.

```bash
# OpenClaw
openclaw skills update totalreclaw

# Claude Desktop / Cursor / Windsurf (MCP)
# The npx invocation pulls the latest automatically

# NanoClaw
# Your deployment pins @totalreclaw/skill-nanoclaw — bump to ^3.0.0

# Python / Hermes
pip install -U totalreclaw

# Rust / ZeroClaw
# Bump `totalreclaw-memory = "2.0.0"` in your Cargo.toml
```

**For self-hosted users:** no schema migration needed. Your PostgreSQL keeps working — the inner blob format changes inside the ciphertext (server never reads plaintext, so it never noticed).

**Pre-v1 vaults:** reads continue working. Old memories are automatically mapped into v1 types when returned. New writes are v1.

---

## Cross-client guarantee

v1 is designed so the same memory written by any client can be read by any other client with identical semantics. If you store a `directive` from OpenClaw, recall from Claude Desktop + MCP returns the same memory with the same type, source, scope, and reasoning.

This is the first release where cross-client parity is specified and tested — not just inherited from shared crypto.

---

## Getting help

- [Memory types guide](./memory-types-guide.md) — user-facing explanation of types, scopes, and volatility.
- [Environment variables reference](./env-vars-reference.md) — current env vars after cleanup.
- [Client setup guide](./client-setup-v1.md) — install instructions per client at v1.
- [Feature comparison](./feature-comparison.md) — what works on each client.
- GitHub: <https://github.com/p-diogo/totalreclaw/issues>
