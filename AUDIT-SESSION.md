# Audit Session — 2026-03-07

## Context
Comprehensive security, code quality, and product audit of TotalReclaw v1.0-beta.
Branch: `audit-fixes` (worktree at `/tmp/totalreclaw-audit`)

## Fixes Applied

### Done
- [x] **Server input validation** — Added SHA-256 format validation (`^[a-f0-9]{64}$`) for blind indices in `store.py` and trapdoors in `search.py`. Also validates `content_fp`.
- [x] **Billing cache typed** — Replaced `any` with `BillingCacheData` interface + `isBillingCacheData()` type guard in `before-agent-start.ts` and `totalreclaw-skill.ts`.
- [x] **Error handling in before-agent-start** — Verified `client.recall()` is already in try/catch; switched `console.error` to `debugLog()` for production quietness.
- [x] **README messaging** — Softened "Decentralized" claim to "Optionally anchor on-chain". Added "Why TotalReclaw?" section acknowledging Mem0/Zep competitors.
- [x] **Subgraph mode default** — Changed from opt-in (`=== 'true'`) to opt-out (`!== 'false'`) in `mcp/src/index.ts`, `mcp/src/subgraph/store.ts`, `skill/plugin/subgraph-store.ts`.
- [x] **Beta guide updated** — Added Storage Modes section, on-chain step in How It Works, subgraph note in Known Limitations, version bumped to v1.0-beta.

### TODO (remaining from audit)
- [x] **Crypto parity test** — Created `tests/parity/crypto-parity.test.ts` (42 tests, all passing). Verifies HKDF parity, Argon2id parameter agreement, BIP-39 path, cross-encryption round-trip.
- [x] **MCP silent .catch()** — Fixed 4 instances in `mcp/src/index.ts` to log errors instead of swallowing. Left `fs.mkdir` catch (legitimate).
- [ ] **Client build fix** — `client/package.json` has `"build": "tsc || true"` which silently swallows 30 TS errors from `ox` dependency. Need to either fix ox compat or properly exclude it.
- [ ] **MCP strict mode** — `mcp/tsconfig.json` has `strict: false`. Enable and fix resulting type errors.
- [ ] **Registration rate limit** — Tighten from 10/hr to 5/hr per IP in server config.

### Nice-to-Have (post-beta)
- [ ] Extract `@totalreclaw/crypto` shared package (3 copies of crypto code)
- [ ] Add CONTRIBUTING.md
- [ ] Add SECURITY.md for responsible disclosure
- [ ] Database connection retry logic (exponential backoff)
- [ ] Graceful shutdown (SIGTERM handler in uvicorn)
- [ ] Load testing (<140ms p95 for 1M memories)

## Audit Grades

| Dimension | Grade |
|-----------|-------|
| Cryptography | A- |
| E2EE (Server-Blind) | A |
| Code Organization | A- |
| Code Quality | B- |
| Product Clarity | B |
| Beta Readiness | B- |
| Security | A- |

## Files Changed
- `README.md` — messaging fixes, competitor acknowledgment
- `server/src/handlers/store.py` — blind index + content_fp validation
- `server/src/handlers/search.py` — trapdoor validation
- `skill/src/triggers/before-agent-start.ts` — BillingCacheData interface, type guard, debugLog
- `skill/src/totalreclaw-skill.ts` — same billing type fixes
- `mcp/src/index.ts` — subgraph mode default (opt-out), silent catch fixes
- `mcp/src/subgraph/store.ts` — isSubgraphMode() default
- `skill/plugin/subgraph-store.ts` — isSubgraphMode() default
- `docs/guides/beta-tester-guide.md` — storage modes, on-chain docs, version bump
- `tests/parity/crypto-parity.test.ts` — NEW: 42 crypto parity tests
