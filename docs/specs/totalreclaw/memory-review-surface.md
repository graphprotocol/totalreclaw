<!--
Product: TotalReclaw
Spec: Memory Review surface (Watchtower) + Lineage lens
Phase: 3.1.1 (Vault SPA — curation/health surface)
Status: Spec — design validated via prototype; backend dependency tracked separately
-->

# Memory Review Surface ("Watchtower") + Lineage Lens

**Phase:** 3.1.1 (Vault SPA) · **Status:** Design validated (prototype), backend dependency tracked
**Author:** Claude (with Pedro Diogo) · **Date:** 2026-06-06
**Prototype:** `app/src/proto/` (`/proto/review`, `/proto/lineage/:id`) on branch
`prototype/spa-look-and-feel` — non-functional, seed data, warm "The Keeper" direction.
**Depends on:** `conflict-resolution.md` §12 (Layer 5 — persist unresolved contradictions).
**Relates to:** PRD-02 (SPA), PRD-MEMQ (memory quality), `2026-05-05-kg-web-app-roadmap.md`
(Phase 3.1.1 curation), `memory-taxonomy-v1.md`, `retrieval-v2.md`.

---

## 1. Why this exists (rationale)

Market research across the agent-memory category (mem0, Zep/Graphiti, Supermemory, Letta, cognee,
agentmemory.dev, Honcho) is consistent: **users adopt memory products for retrieval correctness +
latency, not for graph exploration.** No competitor — including the direct lookalike
agentmemory.dev — has a user-facing knowledge-graph UI that is a loved, adoption-driving feature.
The one validated unmet need is **legibility / trust**: "show me what you actually know about me, and
let me correct it" (Honcho's "low transparency" finding).

This reframes the SPA's memory surface away from "explore a graph" toward **a memory health review** —
the 1Password-Watchtower move for AI memory. The Keeper proactively surfaces what needs a human
(conflicts, stale beliefs, what changed, secrets caught), each with a one-tap action. This is also
the answer to "why would anyone open this app?": **to keep their AI accurate.** Every confirmation
improves retrieval — which is the thing the market actually rewards.

The graph is not deleted; it is **demoted to a precision instrument** (the Lineage lens, §4) and
removed from primary navigation. A global force-directed mind-map remains only as an optional ambient
glance, never a workspace.

## 2. Information architecture

Primary navigation collapses to three surfaces:

| Tab | Surface | Role |
|-----|---------|------|
| **Memory** | session timeline + Crystal headlines + curation (existing Phase 3.1.1 list browser) | trust foundation — "see everything, clearly" |
| **Review** | the Watchtower feed (this spec, §3) | **hero** — "what needs a human" |
| **Lineage** | one belief's typed evolution (this spec, §4) | "why does my agent believe this?" |

Demoted out of nav (gallery-only / future ambient): the global mind-map and the generic graph-first
"Explore" drill. Lineage supersedes them as the meaningful, scalable graph.

## 3. The Review feed

A single feed, grouped by whether the item needs a human decision or is just keeping the user in the
loop. Keeper voice (first person). Each item is a card with one-tap actions; resolving a card removes
it with a soft exit. Empty state: "All clear — I'll keep watch."

- **Needs you** — requires a human decision/confirmation: `conflict`, `stale`.
- **Handled for you** — the Keeper already acted; FYI: `changed`, `secret`.

### 3.1 Card taxonomy → backend primitive

Each card type maps to an engine primitive. **Honesty matters**: only ship a card when its data is
real. The prototype renders an honesty legend per card (shipped / detector-only / needs-backend).

| Card | What it says | Backend primitive | Status today |
|------|--------------|-------------------|--------------|
| **Conflict** | "Two things I believe disagree — which is true?" | unresolved-contradiction record | **NOT shipped** — gated on `conflict-resolution.md` §12 (Layer 5). Engine auto-resolves + discards today. |
| **Still true?** | "You told me X 7 months ago, still true?" | `volatility` (stable/updatable/ephemeral) + `createdAt` | Shipped fields; UI applies an age heuristic. Volatility-aware decay ranking is designed-not-shipped (`tiered-retrieval.md` §3) but not required for v1 of this card. |
| **I changed my mind** | "I moved your July trip Lisbon → Porto." | `superseded_by` chain in the encrypted blob | Shipped. SPA decrypts client-side and walks the chain. |
| **Kept safe** | "I caught an API key and locked it away." | `secrets.rs` (14 detectors) | Detector shipped in core; auto-wiring into extraction + the credentials surface is plumbing work (P6 in the agentmemory port roadmap). |

### 3.2 The "Still true?" ritual

On open (and/or weekly), the Review surfaces 1-3 gentle confirmations (`Still true · Update · Forget`).
Low-effort, high-trust; this is the recurring reason to return. Each confirmation is a cheap, high-value
signal that improves retrieval. v1 uses an age + volatility heuristic; it does not require the decay
ranker to ship first.

### 3.3 Interaction + a11y

- All actions are real buttons with visible focus rings; cards stagger in (55ms) and exit on resolve.
- Color is never the sole signal: every card leads with an icon + text label. Conflict uses the clay
  accent (attention); "Kept safe" uses the green type-summary tone (handled/safe); others are neutral.
- Respects `prefers-reduced-motion` (existing global rule).

## 4. The Lineage lens

The **only** graph surface in the product. Scoped to a single belief thread, so it never becomes a
hairball and scales regardless of vault size. Rendered as a directed vertical thread (not a force
graph) with **typed edges**:

- `replaced by` (supersession) · `contradicts` (unresolved conflict) · `led to` (derived-from).

Nodes show claim text (serif), source, age, and pin state. A `contradicts` edge between a pinned
incumbent and a newer challenger renders the same resolve bar as the Conflict card. Reached from a
Conflict or "changed my mind" card ("See the full history / See why").

Backed by `superseded_by` + `pin_status` + (for the contradiction edge) Layer 5. The supersede/pin
threads are buildable today; the contradiction edge is gated on §12.

## 5. Privacy / E2EE

No new plaintext exposure. Conflict records, supersession links, pin status, source, and volatility
are referenced by `fact_id` / stored inside the encrypted blob; the SPA decrypts client-side to render.
The server learns only "two of this user's facts were flagged as conflicting" — strictly less than
blind indices already leak (see `conflict-resolution.md` §9, §12.3).

## 6. What ships when

- **Now (no backend work):** Memory spine, the **Still true?**, **I changed my mind**, and **Kept
  safe** cards (the last pending detector wiring), the **Lineage** lens for supersede/pin threads.
- **Gated on Layer 5 (`conflict-resolution.md` §12):** the **Conflict** card and the `contradicts`
  Lineage edge. Build the frontend once the backend persists unresolved contradictions. Tracked as a
  blocked-by roadmap item.

## 7. Open questions

| Question | Recommendation |
|----------|----------------|
| Weekly cadence vs on-open for the "Still true?" ritual? | On-open, capped at 3/session; revisit with usage data. |
| Should "Kept safe" live in Review or a dedicated Credentials tab (P6)? | Review surfaces the *event*; the Credentials tab (later) owns management. |
| Does "Needs you" warrant push/notification outside the app? | Out of scope for 3.1.1; note for a later notification pass. |
| Global ambient "constellation" graph — keep or cut? | Keep cut from nav; reconsider only if a real glance-value use case appears. |

## 8. References

- Prototype: `app/src/proto/README.md` · `/proto/review`, `/proto/lineage/:id`.
- Backend dependency: `conflict-resolution.md` §12.
- Market rationale: agent-memory competitor research (this design cycle); `project_agentmemory_competitor`.
- IA precedent: `2026-05-05-kg-web-app-roadmap.md` (Phase 3.1.1 curation), PRD-02, PRD-MEMQ.
