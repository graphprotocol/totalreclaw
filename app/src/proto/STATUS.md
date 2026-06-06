# Vault SPA — work status (living doc)

> **Monitor this file.** It's the single source of truth for where the SPA work stands.
> Updated on every prototype commit. Branch: `prototype/spa-look-and-feel`.

**Last updated:** 2026-06-06 · **Branch HEAD:** see latest commit on `prototype/spa-look-and-feel`
**One-line:** Design-validated look-and-feel prototype is underway. **No functional SPA yet** (no auth/crypto/relay).

---

## Branches

| Branch | What | State |
|--------|------|-------|
| `prototype/spa-look-and-feel` | Look-and-feel prototype, `app/src/proto/`, routes `/proto/*`. Non-functional, seed data. Real routes (`/pair`, `/vault`, `/claim`) untouched. | Pushed. The active design branch. |
| `spec/memory-review-surface` | Spec/docs only, off `main`. | Pushed → **draft PR #308**. |
| `main` | — | Untouched by this work. |

## What the prototype contains

`/proto` is the gallery front door. Run: `npm --prefix app run dev` → `http://localhost:5173/proto`.

- **Onboarding** (`/proto/onboarding`) — create-a-vault, generates a real BIP-39 phrase, backup gate, confirm 4 words, passkey, tour.
- **Unlock** (`/proto/pair`) — passkey-first; recovery phrase = new-device/lost-passkey fallback.
- **Review** (`/proto/review`) — **the hero.** Memory-health "Watchtower" feed: *Needs you* (conflict, still-true?) + *Handled for you* (changed, secret). One-tap actions. Per-card honesty legend.
- **Lineage** (`/proto/lineage/:id`) — the only graph in the product: one belief's typed evolution (replaced-by / contradicts / led-to).
- **Memory** (`/proto/timeline`, `/proto/session/:id`) — session timeline + Crystal headlines + curation (pin/retype/delete+undo). `?empty` → cold-start.
- **Search** (`/proto/search`, global header icon) — instant lexical search over decrypted memories (match highlight, type/source/scope/age). SPA does retrieval; a written answer is an "ask your paired agent" hand-off (synthesis = agent).
- **Cold-start activation arc** — empty Memory (`?empty`, on-ramp + ghosted glimpse) → first-memory "aha" (`?first`, confirm/correct the first captured memory) → warming-up (`?warming`, taking shape) → full. Plus fresh Review (`/proto/review?empty`, teaches the card types).
- **Pair-an-agent** (`/proto/pair-agent`) — **visual stub** (faux QR + code). NOT real pairing.
- **Import guide** (`/proto/import`) — SPA hosts the how-to (per-source export steps + exact agent command); the agent runs the import.

Nav = **Memory · Review · Lineage**. Mind-map + Explore demoted to gallery-only.

## Locked design decisions

1. **Review (memory health) is the hero, not graph exploration.** Grounded in agent-memory market research: users adopt for retrieval correctness + trust; a user-facing graph UI is a loved feature for *no* competitor.
2. **Graph survives only as the narrow Lineage lens** (typed edges, single belief, scales). Generic mind-map/explore demoted.
3. **SPA = decrypt / view / curate / instruct. The agent does anything needing the LLM** (extract, import, generate). Corollary: Review *actions* (resolve/forget/pin = writes/tombstones) are fine in the SPA; extraction/import/generation are agent-only.
4. **Import is an agent capability.** The SPA hosts the how-to (`/proto/import`) but never runs an import. Privacy note flips curated-memory (pattern, no LLM) vs full-history (cleartext to the agent's LLM, with disclosure).
5. Cold-start empty states are **activation surfaces** (single on-ramp = pair an agent).
6. Timeline/session default = **By source** (provenance); 6-type taxonomy demoted to `rule/to-do/preference` badges.

## Specs (in draft PR #308)

- `docs/specs/totalreclaw/memory-review-surface.md` — Phase 3.1.1 Review + Lineage design + card→backend honesty map.
- `docs/specs/totalreclaw/conflict-resolution.md` §12 (Layer 5) — persist unresolved contradictions (the backend dependency).
- Registered in `CLAUDE.md` spec index.

## GitHub roadmap items (product repo `p-diogo/totalreclaw`)

| # | Type | Status |
|---|------|--------|
| **#306** | `[backend]` Persist unresolved contradictions for review | Open. The ONE backend feature this design needs; not previously on roadmap. `track:memq`, `risk-tier:L2`. |
| **#307** | `[web]` Build the Conflict card | Open, **blocked by #306**. |
| **#309** | `[spec]` Review surface (Phase 3.1.1) umbrella | Open. |
| **PR #308** | Spec/docs | Draft. |

## Deliberately NOT done / stubbed / gated

- No real **auth / crypto / relay** anywhere in the prototype.
- **Pair-an-agent** is a visual stub — real pairing = **PRD-01** (Hermes auth-hardening, session keys), deferred.
- **Conflict card** is designed but **gated on backend #306** (engine auto-resolves + discards contradictions today).
- **Import** is guide-only; the agent executes (`totalreclaw_import_from`).
- **Not built:** the home/return loop + "still true?" on-open ritual; semantic/embedding ranking in search (lexical only today — embedding rank would need the model, likely agent-side).

## Open design backlog (next candidates)

1. **Home loop + "still true?" ritual** — make Review a habit, not a one-time visit; landing logic (Review when it needs you, else Memory) + Review tab badge + on-open check-in.
2. **Mobile / responsive pass** — the prototype is desktop-centered (max-w-2xl); a memory-review product is a phone-checked surface.

**Recently done:** vault search (SPA finds, agent answers) — `0625299` · first-memory "aha" + warming-up timeline — `1cafb9a`.

## Reference

- Prototype design decisions + routes: `app/src/proto/README.md`.
- Real Phase-1 SPA spec (source of truth for the real build): `totalreclaw-internal/docs/specs/web/spa-phase1.md`.
- This prototype is the **design reference**, not the implementation.

---

## Changelog (prototype branch)

- `0625299` — vault search (`/proto/search`): SPA-local lexical find + highlight; "ask your agent" for a written answer (synthesis = agent). Global header search icon.
- `1cafb9a` — first-memory "aha" + warming-up timeline (cold-start activation arc: empty → first → warming → full).
- `4ba5eba` — import guide (`/proto/import`): SPA hosts the how-to, agent runs the import.
- `90f9743` — fix: import is an agent capability, not an SPA action (removed misleading in-app import button).
- `fc8da9c` — cold-start / empty-vault surfaces + pair-an-agent stub.
- `3570f01` — Review surface + Lineage lens; demote mind-map from nav.
- (earlier) `a97c8e3` polish · `fc45740` onboarding + passkey unlock · `39714bf` pair + explore redesign · `437f491` gallery · `df5a224` warm "Keeper" prototype.
