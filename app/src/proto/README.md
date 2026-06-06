# Vault SPA — look-and-feel prototype (`app/src/proto/`)

A throwaway **UI/UX prototype** for the Vault SPA, built via the `impeccable` skill to test
the look and feel of the warm **"The Keeper"** direction before the real Phase-1 SPA is built.

- **Branch:** `prototype/spa-look-and-feel` (off `main`). Not for merge as-is.
- **NOT functional:** no auth, no crypto, no relay, no real vault. All screens run on **seed
  data** (`seed.ts`, `graph-data.ts`) and live under `/proto/*` so the real routes
  (`/pair`, `/vault`, `/claim`) are untouched.
- **Design context (repo root):** `PRODUCT.md`, `DESIGN.md`, `.impeccable/design.json`.

## Run it

```bash
npm --prefix app run dev      # from repo root → http://localhost:5173/proto
```

`/proto` is the **gallery** (front door) — links to every screen + variant.

## Routes

Primary nav is **Memory · Review · Lineage**. The global mind-map + graph-first Explore are demoted
out of nav (gallery-only) — Lineage replaced them as the meaningful, scalable graph.

| Route | Screen |
|-------|--------|
| `/proto` | Gallery index |
| `/proto/onboarding` | Create-a-vault: generate real BIP-39 phrase → backup gate → confirm 4 words → passkey → tour |
| `/proto/pair` | Unlock — passkey-first; recovery phrase = new-device/lost-passkey fallback |
| `/proto/pair-agent` | **Pair-an-agent stub** — faux QR + 6-digit code + "waiting…". Visual only; NOT real pairing (PRD-01). The cold-start on-ramp destination. |
| `/proto/review` | **Review (hero)** — memory health / Watchtower feed: Needs-you (conflict, still-true?) + Handled-for-you (changed, secret). One-tap actions. See `docs/specs/totalreclaw/memory-review-surface.md`. |
| `/proto/lineage/:id` | **Lineage** — one belief's typed evolution (`replaced by` / `contradicts` / `led to`). The only graph in the product. |
| `/proto/timeline` | Session timeline (= "Memory" tab); filters (scope/type/source/open-threads + tap-entity); `?view=type` toggles presentation (default **By source**). `?empty` → cold-start empty vault. |
| `/proto/review?empty` | **Cold-start Review (fresh)** — day-1 "nothing to review yet" that teaches the four card types; distinct from the cleared "all clear" state. |
| `/proto/session/:id` | Session detail: Crystal + curatable Claim Cards (pin/retype/delete + 10s undo) |
| `/proto/kg` | Mind-map (React Flow) — **demoted**, ambient/gallery-only |
| `/proto/explore` | Graph-first explorer — **demoted**, superseded by Review + Lineage |

## Locked design decisions (with Pedro)

- Warm/personal "The Keeper": warm-white `#FBFAF8` bg (never cream), single **clay** accent
  `#C16240`, **Fraunces** serif for memory text + **Figtree** UI + JetBrains Mono.
- **Review is the hero.** Market research: users adopt memory products for retrieval correctness, not
  graph exploration. Reframed the surface to a memory-health Watchtower ("what needs a human"). The
  graph is demoted to the narrow **Lineage** lens (typed, per-thread, scales). See
  `docs/specs/totalreclaw/memory-review-surface.md` (Phase 3.1.1).
- **Conflict card is gated on backend.** Engine auto-resolves + discards contradictions today; the
  card needs `conflict-resolution.md` §12 (Layer 5: persist unresolved contradictions). Tracked as a
  blocked-by roadmap item. Every other card rides shipped data the SPA decrypts client-side.
- **React Flow** is the KG engine (Cinematic/force-graph engine was evaluated then dropped).
- **Explore** is graph-first, sessions-first (no aggregated fact dump); Workspace mode dropped.
- Timeline + session detail default to **By source** (provenance), type demoted to plain
  `rule / to-do / preference` badges.
- Auth/onboarding lands on the **timeline**.

## Known gaps / deferred (see internal notes)

- **Pair-an-agent** screen (SPA↔Hermes QR/deep-link handoff) — not built. See
  `totalreclaw-internal/docs/notes/2026-06-02-spa-prototype-deferred-pairing.md`.
- Real auth (passkey wrap, session-key delegation) is **PRD-01 / PRD-02** work, not this proto.
- **Open-threads** cross-session lifecycle is a PRD-MEMQ gap — see
  `totalreclaw-internal/docs/notes/2026-06-02-open-threads-lifecycle-gap.md`.
- **Reagraph requires React 19** (`@react-three/fiber@9`) — do not add it on this React-18 app.

## Screenshot loop

`app/*-shot.mjs` are Playwright capture scripts (output `app/proto-shots/`, gitignored).
The impeccable live helper injects a script that breaks Playwright `networkidle` — use
`domcontentloaded`, and `node .claude/skills/impeccable/scripts/live-server.mjs stop` to strip it.
