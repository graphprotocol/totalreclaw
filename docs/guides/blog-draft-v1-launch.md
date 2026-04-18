# TotalReclaw v1 — Better Memory for AI Agents

**Status:** DRAFT. Do not publish until the phase 2 benchmark number lands (see `[BENCHMARK PLACEHOLDER]` below).
**Target:** <https://totalreclaw.xyz/blog/v1-launch>
**Word count target:** ~800.

---

## v1 is live

TotalReclaw v1 ships today across every supported client: OpenClaw, Claude Desktop (via MCP), Cursor, Windsurf, NanoClaw, IronClaw, Hermes, and ZeroClaw. If your client updates automatically, you already have it. If it pins a version, bump the package — the new versions are plugin 3.0.0, mcp-server 3.0.0, nanoclaw 3.0.0, python 2.0.0, and ZeroClaw 2.0.0.

v1 is the first release where a memory written by one client can be read by any other client with identical semantics. Same claim, same type, same source, same scope. That sounds obvious; in practice the memory systems we studied (Mem0, Zep, Mastra, Supermemory, Letta) each define their own schema and none of them interoperate.

We shipped v1 because the MCP protocol itself deliberately leaves memory semantics undefined, and someone had to write a cross-client spec. We are uniquely positioned to do it: we ship in five clients, our server is end-to-end encrypted (so we cannot unilaterally normalize memory server-side — the spec has to be client-enforced), and we have no commercial reason to diverge from a standard.

## What is new in v1

**Six memory types, grounded in speech acts.** Instead of the 8 overlapping categories we had before (`fact`, `context`, `decision`, `preference`, `rule`, `goal`, `episodic`, `summary`), v1 uses a closed set of six:

- `claim` — assertive: "X is the case."
- `preference` — expressive: "I like X."
- `directive` — imperative: "always do X."
- `commitment` — commissive: "I will do X."
- `episode` — narrative: "X happened at time T."
- `summary` — derived: session syntheses only.

Each type corresponds to one of John Searle's illocutionary classes. The mapping is intentional — it forces extractors to think "what kind of *act* is the user performing" instead of "what kind of object is this." That one change broke the importance-clustering pathology where every memory was pinned at importance 7 or 8.

**Three orthogonal axes on every memory.** `source` (user / user-inferred / assistant / external / derived), `scope` (work / personal / health / family / creative / finance / misc / unspecified), and `volatility` (stable / updatable / ephemeral). These ride alongside the type instead of being jammed into it.

**Source-weighted reranking.** The biggest lever v1 pulls on recall quality. Every memory now carries who wrote it, and the ranker penalizes assistant-authored content by default. Mem0's own team documented a case where 97.8% of their memory entries were junk because the assistant was authoring "facts" about the user that then ranked equal to things the user actually said. v1 tags and down-weights those (weight 0.55) instead of dropping them — so genuine content the assistant summarised (like a receipt you shared) is still recoverable, but opinion drift from the assistant never outranks what you said directly.

**Three new tools for overrides.** `totalreclaw_pin`, `totalreclaw_retype`, and `totalreclaw_set_scope`. You invoke them via natural language — say "pin that" or "that was actually a rule, not a preference" and the agent calls the right tool. No JSON editing.

## Why E2EE matters here

The hard design constraint on v1 was: we cannot normalize memory server-side, because we never see plaintext. So everything — type inference, source attribution, scope detection, volatility assignment, provenance filtering — happens client-side, inside the encrypted envelope.

This is the difference between a privacy promise and a structural guarantee. Mem0 and Zep store your memories in plaintext on their servers: they promise they won't read them, but they can. TotalReclaw can't, by design.

E2EE also forces a different product shape. We cannot auto-tune your retrieval from your behaviour (we can't see your behaviour). We cannot A/B-test ranker weights on your data. Every setting has to be either a universal default (same for everyone) or a natural-language override you explicitly ask for. v1 is designed around that constraint.

## Benchmarks

[BENCHMARK PLACEHOLDER — will fill in once the 500-conversation phase 2 run completes. Expected to cover Gemini Takeout + WildChat corpora. Metrics: clustering ratio, normalized type entropy, recall@8, precision@1, token-cost delta. Target headline: "v1 breaks the 51% importance clustering, keeps recall quality at baseline."]

The short version, pending final numbers: v1's source-weighted reranker preserves the recall quality of our v0.3 pipeline while the taxonomy change restructures the vault to spread memories across types instead of collapsing to 70%-clustering-on-fact.

## What does not change

Your recovery phrase, your wallet address, your memories, your ability to export plain-text JSON, and the free tier on Base Sepolia all stay the same. Upgrade the package and everything keeps working. Pre-v1 memories are read transparently — the client maps old types onto the new ones on the fly.

If you self-host, no schema migration. The inner blob format changes inside the ciphertext, which your PostgreSQL never saw anyway.

## No action required

For users: just upgrade. The full migration guide lives at [docs/guides/v1-migration.md](./v1-migration.md).

For developers using the libraries directly: core 2.0.0 ships new strict validators and a new `rerankWithConfig` entry point. The v0 paths are preserved where practical (reads are back-compat, writes are v1-only). Per-package CHANGELOGs have the details.

## What is next

Retrieval Tier 2-4 (scope pre-filter, volatility-aware decay, type boost) are designed and will ship post-v1, gated on real recall-quality data. A native web app at `app.totalreclaw.xyz` lets you inspect and bulk-edit memories out of band. And we are taking the v1 spec to the MCP spec repo as a proposed optional extension — once other memory systems align on it, your TotalReclaw memories become truly portable across the whole AI tool ecosystem.

Try it. If something is wrong, file an issue: <https://github.com/p-diogo/totalreclaw/issues>.
