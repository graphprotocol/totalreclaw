# Memory surface redesign — A/B sandbox (seed data)

> Branch `design/spa-memory-ab` off `prototype/spa-look-and-feel`. Design-only, seed
> data, for A/B exploration. Winners port to the functional app (`feat/spa-functional`)
> on real data later. Designed via the `impeccable` skill (shape brief, 2026-06-11).

## Decisions (Pedro, 2026-06-11)
- Memory = a **mode switcher**: **List · Timeline · Graph** (segmented control, Figtree).
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
