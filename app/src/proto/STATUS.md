# Vault SPA — work status (living doc)

> **Monitor this file.** It's the single source of truth for where the SPA work stands.
> Updated on every prototype commit. Branch: `prototype/spa-look-and-feel`.

**Last updated:** 2026-06-07 · **Branch HEAD:** see latest commit on `prototype/spa-look-and-feel`
**One-line:** Design surface complete. **No functional SPA yet** (no auth/crypto/relay). **Next = functional build; kickoff ready (see below).**

---

## Next: functional build — kickoff ready (2026-06-07)

**Decision (Pedro):** ADOPT this Keeper prototype as the design + IA **source of truth** for the real functional SPA. Build it for real on a **fresh branch off `main`** (NOT this prototype branch); rework the old Phase-1 reads pages (#236) to the Keeper design; this branch stays as reference.

- **Carry forward (locked):** Nav = Memory · Review + settings gear; Review = hero; Lineage = drill-in. Unlock = passkey-first, phrase = recovery fallback. Import = agent capability (SPA hosts how-to). No SPA search oracle. Source-forward. Conflict card gated on #306.
- **THE CRUX (settle before wiring):** WebAuthn/passkey = *authentication*, not key derivation. E2EE keys derive from the recovery phrase. To kill the typed mnemonic without breaking E2EE (server never sees plaintext/master key), pick: **(a)** WebAuthn PRF → wrap/unwrap seed client-side, or **(b)** passkey-authorized session-key delegation (per `session-key-delegation` cred work).
- **Process:** assess (lead with uncertainties) → decide architecture WITH Pedro (brainstorming, no code) → design doc → port screens + wire ↔ relay. Tests = STAGING only. Secrets = L3 (never print/log/cross-context).
- **Read order:** this file → `README.md` → memory `project_spa_warm_prototype.md` → `totalreclaw-internal/docs/specs/web/spa-phase1.md` → `session-key-delegation.md` + PRD-01 + PRD-02 → `2026-05-23-spa-auth-passkey-flow.md`.

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
- **Home loop** — Review nav **badge** (always-visible needs-you count) + on-open **"still true?" check-in** ritual (`/proto/checkin`, 1-3 confirmations) + landing logic (returning unlock → check-in → Review; new onboarding → empty vault).
- **Review** (`/proto/review`) — **the hero.** Memory-health "Watchtower" feed: *Needs you* (conflict, still-true?) + *Handled for you* (changed, secret). One-tap actions. Per-card honesty legend.
- **Lineage** (`/proto/lineage/:id`) — the only graph in the product: one belief's typed evolution (replaced-by / contradicts / led-to).
- **Memory** (`/proto/timeline`, `/proto/session/:id`) — session timeline + Crystal headlines + curation (pin/retype/delete+undo). `?empty` → cold-start.
- **Settings / account** (`/proto/settings`, header gear) — account (vault/plan/usage), security & recovery (passkey, reveal phrase), **paired agents** (named instances "John (Hermes)" + revoke), **export** (.json/.md with unencrypted-file heads-up), import link, danger zone.
- **Find** — a keyword **filter inside Memory** (narrows what's shown) + an "ask your agent for an answer" pointer. No standalone search page: semantic answers need the LLM (agent). Imported sessions show their origin ("Imported · ChatGPT").
- **Cold-start activation arc** — empty Memory (`?empty`, on-ramp + ghosted glimpse) → first-memory "aha" (`?first`, confirm/correct the first captured memory) → warming-up (`?warming`, taking shape) → full. Plus fresh Review (`/proto/review?empty`, teaches the card types).
- **Pair-an-agent** (`/proto/pair-agent`) — **visual stub** (faux QR + code). NOT real pairing.
- **Import guide** (`/proto/import`) — SPA hosts the how-to (per-source export steps + exact agent command); the agent runs the import.

Nav = **Memory · Review**. Lineage is a drill-in (from Review/Memory), not a nav tab. Mind-map + Explore are gallery-only.

## Locked design decisions

1. **Review (memory health) is the hero, not graph exploration.** Grounded in agent-memory market research: users adopt for retrieval correctness + trust; a user-facing graph UI is a loved feature for *no* competitor.
2. **Graph survives only as the narrow Lineage lens** (typed edges, single belief, scales). Generic mind-map/explore demoted.
3. **SPA = decrypt / view / curate / instruct. The agent does anything needing the LLM** (extract, import, generate). Corollary: Review *actions* (resolve/forget/pin = writes/tombstones) are fine in the SPA; extraction/import/generation are agent-only.
4. **Import is an agent capability.** The SPA hosts the how-to (`/proto/import`) but never runs an import. Privacy note flips curated-memory (pattern, no LLM) vs full-history (cleartext to the agent's LLM, with disclosure).
5. Cold-start empty states are **activation surfaces** (single on-ramp = pair an agent). Onboarding lands on the empty vault (`?empty`), not a pre-filled timeline.
6. **Source-forward only** — type/source toggle dropped; 6-type taxonomy is just a badge.
7. **No SPA search oracle** — semantic answers need the LLM (agent); the SPA keeps only a keyword filter + "ask your agent" pointer.
8. **Imported memories show their origin** in the UI; deeper agent-identity provenance ("John (Hermes)") is backend — issue #317.
9. **Lineage is a drill-in, not a nav tab.**

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
- **Not built (functionally):** everything is non-functional UI. Export, revoke, reveal-phrase, delete are visual only. Deeper agent-identity provenance is backend (#317). Mobile/responsive not yet done.

## Open design backlog (next candidates)

The design surface feels complete for a look-and-feel prototype. Remaining work is mostly
implementation, not design:

1. **Make it functional** — wire the screens to real auth/crypto/relay (the real Phase-1 build, per `spa-phase1.md`). Deferred functional tracks now on GitHub: PRD-01 (pairing/auth), #306/#307 (conflict persistence + card), #317 (agent-identity provenance), **#323 (functional export)**.
2. (Optional design) revisit if new needs surface — notifications outside the app, an ambient "constellation" graph, etc.

**Mobile:** verified responsive at 390px (zero overflow) — `e3d983b`.

**Recently done:** mobile pass — `e3d983b` · settings/account + export home — `c0c7b17` · home loop — `e2f37f9` · search→filter + import-origin + audit fixes — `777249d`.

## Reference

- Prototype design decisions + routes: `app/src/proto/README.md`.
- Real Phase-1 SPA spec (source of truth for the real build): `totalreclaw-internal/docs/specs/web/spa-phase1.md`.
- This prototype is the **design reference**, not the implementation.

---

## Changelog (prototype branch)

- `e3d983b` — mobile/responsive pass: verified zero overflow at 390px; import command stacks + wordmark shrinks on narrow screens.
- `c0c7b17` — settings/account corner (`/proto/settings`): account/usage, security & recovery, paired agents (named + revoke), export (.json/.md + heads-up), danger zone. Header gear.
- `e2f37f9` — home loop: Review nav badge + on-open "still true?" check-in ritual (`/proto/checkin`) + landing logic (returning unlock → check-in → Review).
- `777249d` — drop standalone search → keyword filter in Memory; show import origin ("Imported · ChatGPT"); audit fixes (source-only, Lineage drill-in, onboarding→empty). Issue #317 filed (agent-identity provenance).
- `0625299` — vault search (`/proto/search`): SPA-local lexical find + highlight; "ask your agent" for a written answer (synthesis = agent). Global header search icon. **(superseded by 777249d)**
- `1cafb9a` — first-memory "aha" + warming-up timeline (cold-start activation arc: empty → first → warming → full).
- `4ba5eba` — import guide (`/proto/import`): SPA hosts the how-to, agent runs the import.
- `90f9743` — fix: import is an agent capability, not an SPA action (removed misleading in-app import button).
- `fc8da9c` — cold-start / empty-vault surfaces + pair-an-agent stub.
- `3570f01` — Review surface + Lineage lens; demote mind-map from nav.
- (earlier) `a97c8e3` polish · `fc45740` onboarding + passkey unlock · `39714bf` pair + explore redesign · `437f491` gallery · `df5a224` warm "Keeper" prototype.
