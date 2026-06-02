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

| Route | Screen |
|-------|--------|
| `/proto` | Gallery index |
| `/proto/onboarding` | Create-a-vault: generate real BIP-39 phrase → backup gate → confirm 4 words → passkey → tour |
| `/proto/pair` | Unlock — passkey-first; recovery phrase = new-device/lost-passkey fallback |
| `/proto/timeline` | Session timeline; filters (scope/type/source/open-threads + tap-entity); `?view=type` toggles presentation (default **By source**) |
| `/proto/session/:id` | Session detail: Crystal + curatable Claim Cards (pin/retype/delete + 10s undo) |
| `/proto/kg` | Mind-map (React Flow) |
| `/proto/explore` | Graph-first explorer: tap node → its sessions → open one → its memories (in place) |

## Locked design decisions (with Pedro)

- Warm/personal "The Keeper": warm-white `#FBFAF8` bg (never cream), single **clay** accent
  `#C16240`, **Fraunces** serif for memory text + **Figtree** UI + JetBrains Mono.
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
