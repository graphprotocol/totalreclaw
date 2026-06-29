# Memory surface redesign — A/B sandbox (seed data)

> Branch `design/spa-memory-ab` off `prototype/spa-look-and-feel`. Design-only, seed
> data, for A/B exploration. Winners port to the functional app (`feat/spa-functional`)
> on real data later. Designed via the `impeccable` skill (shape brief, 2026-06-11).

## Decisions (Pedro, 2026-06-11)
- Memory = a **mode switcher**: **List · Timeline · Graph** (segmented control, Figtree).
  **List is the locked default mode** (Pedro, 2026-06-19) — Memory lands on List; Timeline/Graph
  are opt-in toggles. (Code: `useState<Mode>("list")`.)
- **Side-panel drawer** shared across all modes (Notion-style): tap any card/dot/node →
  drawer with Crystal/summary + atomic facts + entities + outcomes/threads. Full-screen
  sheet on mobile. Deep-linkable. Replaces full-page session drill as the primary path.
- **List**: responsive **gallery grid** of summary cards. Crystal headline if present;
  **synthesized summary** otherwise (`N memories · Imported · scopes · top entities`) —
  never a fact-as-fake-title.
- **Timeline** sub-toggle **Rail · Activity** (A/B both):
  - Rail: horizontal scrollable date axis; sessions as dots/mini-cards by time.
  - Activity: GitHub-style density heatmap (weeks × days) + session list below.
- **Graph** sub-toggle **Entities · Topics** (A/B both):
  - Entities: force-directed entity graph (@xyflow/react + d3-force).
  - Topics: hierarchical tree-of-topics → sessions.
- **Facets** persist across modes: Source · Scope · Open-threads · Entity · keyword.
- **Imported** badge on import-sourced items.
- **Review Conflict card fix**: selection-driven, **pin-aware default (keep-pinned)**,
  one clay element, explicit labels. (Still backend-gated #306 — design only.)

## Transparency & fact-first (LOCKED, Pedro 2026-06-19 — from ChatGPT-memory lesson)
Lesson: users *trust* an explicit, editable list of discrete facts and resent ChatGPT's move
to opaque synthesis (Dreaming V3). That transparency is **TotalReclaw's wedge** — lean in.
- **The discrete fact is the first-class unit** — individually viewable / editable / deletable.
  Never only session-Crystals; a fact must be curatable *as a fact*, not buried under a summary.
- **Add a flat "everything you remember" lens** (the ChatGPT-style bullet list of all facts)
  alongside the session-grouped List. Cheap, high-trust, exactly what their users miss.
- **Synthesis (Crystals / summaries) is an additive skim layer, NEVER a replacement** for the
  explicit list. Show exactly what's stored; let users correct it. No opaque synthesis.
- **Entity-nav is the reliable axis; Topics is secondary.** Subgraph is server-blind — it stores
  only ciphertext + envelope (owner/decay/active/fp/seq/version/timestamps) + SHA-256 blind
  indices. Entities (per-claim) + topics (`metadata.topics_discussed`, Crystal-only) + session_id
  live *inside* the encrypted blob → derived **client-side** after full decrypt. Topic↔entity
  links aren't stored (derived by co-occurrence). So bias entity-nav over topic-grouping; topics
  are sparse + heuristic.
- **Cost caveat (under analysis):** all nav/grouping requires decrypting the whole vault
  client-side. Agent analyzing 3-mo heavy Pro (~4,500 facts) load — candidates: drop
  `encryptedEmbedding` from the browse query, `sequenceId` delta-sync + IndexedDB cache,
  progressive disclosure.

## System (locked, from DESIGN.md / PRODUCT.md)
Keeper: warm-white `#FBFAF8`, clay `#C16240` ≤10%, Fraunces (memory text) + Figtree (UI)
+ JetBrains Mono (machine), soft warm shadows, `ease-keeper`, 150–250ms, prefers-reduced-
motion fallbacks, phone-first, WCAG AA, memory type never color-only.

## Data reality (drives the variants)
- Crystals exist only for debrief/session-end clients (Hermes); **imports + MCP have none**.
- key_outcomes/open_threads/lessons are **optional best-effort** — render only when present.
- Provenance: imports currently store generic source; provider ("Gemini") + crystals on
  import are a separate backend task (handoff prompt already given to another agent).

## Instrumentation
Deferred — A/B is on seed data, no telemetry sink yet. When it ships to real data, add a
content-free event log (toggle adoption + dwell) per the E2EE/privacy constraint.

## Build order
1. **Chassis** — ModeSwitcher + SidePanel drawer + the new Memory shell (List mode first).
2. Timeline (Rail, Activity). 3. Graph (Entities, Topics). 4. Conflict-card fix.
Each verified live (dev server + screenshots) on seed data.
