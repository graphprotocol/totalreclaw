---
name: TotalReclaw Vault
description: The warm, personal web vault where you read and groom your AI's memory
colors:
  clay: "#C16240"
  clay-deep: "#A54B2E"
  clay-tint: "#F7E7DD"
  warm-white: "#FBFAF8"
  surface: "#FFFFFF"
  ink: "#2B2824"
  ink-muted: "#685E57"
  hairline: "#E7E3DF"
typography:
  display:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "clamp(2rem, 5vw, 3.25rem)"
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "clamp(1.5rem, 3vw, 2rem)"
    fontWeight: 500
    lineHeight: 1.15
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Figtree, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "Figtree, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "Figtree, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.01em"
  mono:
    fontFamily: "JetBrains Mono, Fira Code, ui-monospace, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
rounded:
  sm: "8px"
  md: "12px"
  lg: "16px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.clay}"
    textColor: "{colors.warm-white}"
    rounded: "{rounded.md}"
    padding: "12px 20px"
    typography: "{typography.label}"
  button-primary-hover:
    backgroundColor: "{colors.clay-deep}"
    textColor: "{colors.warm-white}"
  button-ghost:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "12px 16px"
  chip:
    backgroundColor: "{colors.clay-tint}"
    textColor: "{colors.clay-deep}"
    rounded: "{rounded.full}"
    padding: "5px 12px"
    typography: "{typography.label}"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "16px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "10px 14px"
---

# Design System: TotalReclaw Vault

## 1. Overview: The Keeper

**Creative North Star: "The Keeper"**

The vault is a calm, trusted archivist of your life. Not a database you administer, a collection you visit. The system behaves like a well-kept personal library: everything has a place, the light is soft, and the act of tidying is quietly satisfying. Warmth comes from the things that carry feeling, a clay-toned accent, a humanist serif for the things worth reading slowly, generous rounding, gentle shadow, and never from decoration for its own sake.

It is built on restraint, because the product's real promise is trust. End-to-end encryption is conveyed by clarity and honesty, not by lock icons or scare-copy. The chain, the wallet, the ciphertext underneath are invisible. A person should open this and feel *this is mine, and it's safe*, closer to a personal journal than an admin console.

This system explicitly rejects three things. It is **not a generic SaaS dashboard**: no identical card grids, no hero-metric tiles, no Linear/Vercel chrome. It is **not a crypto wallet**: no neon-on-dark, no decorative gradients, no glassmorphism, no chain language. It is **not a raw developer tool**: no JSON dumps, no terminal aesthetic, no monospace-everything, no unstyled tables. The current build is bare Tailwind defaults and leans toward that third trap; this spec is the climb out of it.

**Key Characteristics:**
- Warm minimalism: restraint with a pulse, never sterility.
- A single clay accent carries the warmth; the surface stays a true warm-white, never cream or beige.
- A humanist serif (Fraunces) for memory text and headings; a humanist sans (Figtree) for UI.
- Soft and tactile: gentle shadows, rounded corners, pill chips. Things look touchable.
- Phone-first and glanceable; fast on a 10,000-item vault.

## 2. Colors: The Clay-and-Warm-White Palette

A warm-white field, near-black warm ink, and one earthy clay accent. The type-badge hues are the only secondary color, and they whisper, never shout.

### Primary
- **Clay** (`#C16240` / `oklch(0.60 0.125 42)`): The brand's warmth, made of one color. Used on primary buttons, the active filter chip, the pin affordance when set, focus rings, and the small handful of "this matters" moments. Earthy and human, deliberately not SaaS-blue.
- **Clay Deep** (`#A54B2E` / `oklch(0.52 0.13 40)`): Hover and active state for clay surfaces, and clay-colored text on light tints (where the mid clay would fail contrast).
- **Clay Tint** (`#F7E7DD` / `oklch(0.95 0.03 48)`): The soft wash behind selected chips, the pinned-row highlight, and gentle clay callouts. A tint, never the body background.

### Neutral
- **Warm White** (`#FBFAF8` / `oklch(0.985 0.003 70)`): The body background. Warm enough to feel human under a phone at night, light enough to read clearly as white, not cream.
- **Surface** (`#FFFFFF` / `oklch(1 0 0)`): Cards and rows. Pure white lifting off the warm-white field by a hair of tone plus a soft shadow.
- **Ink** (`#2B2824` / `oklch(0.27 0.012 50)`): Primary text. A warm charcoal, not pure black, friendlier and easier on the eye. ~13:1 on warm-white.
- **Ink Muted** (`#685E57` / `oklch(0.48 0.012 55)`): Secondary text, metadata, source/scope labels, placeholders. Tuned to clear AA (≥4.5:1) on warm-white. Verify any further lightening in audit.
- **Hairline** (`#E7E3DF` / `oklch(0.90 0.006 60)`): Warm 1px borders and dividers. Structure without weight.

### The Type Palette (secondary, carried in the sidecar)
Each of the six memory types gets a soft warm tint + accessible ink pair, recognizable by hue but legible by label. `claim` (warm neutral), `preference` (calm blue), `directive` (soft violet), `commitment` (honey, a warm sibling of clay), `episode` (muted neutral, the most transient), `summary` (gentle green). Full values live in `.impeccable/design.json` (`colorMeta`). These replace the stock Tailwind `bg-blue-100 / text-blue-800` pairs currently hard-coded in `app/src/lib/types.ts`.

### Named Rules
**The One Clay Rule.** Clay appears on ≤10% of any screen. It marks primary action, active state, and "pinned". If two clay things compete for attention in one viewport, one of them is wrong.

**The No-Cream Rule.** The background is warm-*white* (`#FBFAF8`), never the cream/sand/parchment band (L 0.84–0.97, C < 0.06, hue 40–100). Warmth is carried by the clay accent and the serif, not by a beige field. If the background reads as "paper", it has drifted; pull chroma back toward 0.003.

## 3. Typography

**Display Font:** Fraunces (with Georgia, serif)
**Body Font:** Figtree (with ui-sans-serif, system-ui)
**Label/Mono Font:** JetBrains Mono (with Fira Code, ui-monospace)

**Character:** A humanist "old-style" serif paired with a gently rounded humanist sans, contrast on the serif/sans axis, not two near-identical sans. Fraunces gives memory text and headings a personal, journal-like warmth; Figtree keeps the UI friendly and quiet. JetBrains Mono is reserved strictly for machine artifacts (IDs, timestamps, hashes), the one honest place a monospace belongs.

### Hierarchy
- **Display** (Fraunces 500, `clamp(2rem, 5vw, 3.25rem)`, lh 1.05, tracking -0.02em): Page-level titles, empty-state headlines, the landing card. Use `text-wrap: balance`.
- **Headline** (Fraunces 500, `clamp(1.5rem, 3vw, 2rem)`, lh 1.15): Section headers, the claim text on a detail page, read slowly.
- **Title** (Figtree 600, 17px, lh 1.3): Row titles, dialog headers, control-group labels.
- **Body** (Figtree 400, 16px, lh 1.55): UI prose, descriptions, help text. Cap measure at 65–75ch.
- **Label** (Figtree 600, 13px, tracking 0.01em, sentence case): Chips, badges, buttons, metadata keys.
- **Mono** (JetBrains Mono 400, 13px): Claim IDs, ISO timestamps, addresses. Never body copy.

### Named Rules
**The Serif-for-Memory Rule.** The user's actual memory text is set in Fraunces, the warmest face in the system. UI chrome is Figtree. The thing worth reading slowly looks different from the controls around it.

**The Mono-Stays-Backstage Rule.** Monospace is for machine artifacts only. The moment a sentence a human wrote lands in JetBrains Mono, the UI has tipped into "developer tool". Forbidden for any human-readable content.

## 4. Elevation

Soft and tactile. Surfaces are flat at rest with a faint warm-toned shadow that says "liftable", and elevation increases on interaction and for overlays. Shadows are tinted with warm ink (`rgba(43, 40, 36, …)`), never neutral gray-black, so the lift reads warm. No glassmorphism, no backdrop blur as decoration.

### Shadow Vocabulary
- **Soft** (`box-shadow: 0 1px 2px rgba(43,40,36,0.04), 0 2px 8px rgba(43,40,36,0.06)`): Resting cards and claim rows.
- **Raised** (`box-shadow: 0 4px 16px rgba(43,40,36,0.10)`): Hover/press on interactive cards, the active filter bar.
- **Overlay** (`box-shadow: 0 8px 32px rgba(43,40,36,0.14)`): Dialogs, action sheets, the mobile detail sheet.

### Named Rules
**The Warm-Shadow Rule.** Every shadow is tinted toward ink-warm, never pure black. A neutral `rgba(0,0,0,…)` shadow on this warm field reads cold and cheap. If a card's lift looks gray, the shadow color is wrong.

## 5. Components

### Buttons
- **Shape:** Gently rounded (12px, `{rounded.md}`).
- **Primary:** Clay background, warm-white text, 12px×20px padding, Label type. The single loud control on any screen.
- **Hover / Focus:** Background shifts to Clay Deep, lifts with Raised shadow and `translateY(-1px)`; focus-visible draws a 2px Clay ring with a 2px offset. ~150ms ease-out.
- **Ghost / Secondary:** Surface (or transparent) background, Ink text, hairline border on hover; for low-emphasis actions (Cancel, secondary nav).
- **Destructive:** Ghost by default; confirmation step uses Clay Deep text, never a red wall. Delete is reversible (30-day tombstone), so the UI stays calm, not alarmist.

### Chips (filters + type badges)
- **Style:** Pill (`{rounded.full}`), soft tint background + matching deep-ink text, Label type. No borders.
- **Filter, unselected:** Surface background, ink-muted text, hairline border.
- **Filter, selected:** Clay Tint background, Clay Deep text, no border, optional 1px Clay ring.
- **Type badge:** Per-type tint + ink from the Type Palette. **The label text is mandatory**; color is reinforcement only, never the sole signal (color-blind + AA requirement).

### Cards / Containers (the Claim Card is the signature)
- **Corner Style:** 16px (`{rounded.lg}`).
- **Background:** Surface white on the warm-white field.
- **Shadow Strategy:** Soft at rest, Raised on hover (see Elevation).
- **Border:** None by default; the shadow does the lifting. A hairline only where rows meet with no gap.
- **Internal Padding:** 16px (`{spacing.md}`).
- **Pinned state:** Clay Tint background wash + a small filled Clay pin glyph, top-right. The one place a row changes color.
- Nested cards are forbidden. A claim row is a single surface.

### Inputs / Fields
- **Style:** Surface background, 1px hairline border, 12px radius, 10px×14px padding.
- **Focus:** Border shifts to Clay, a 2px Clay ring at 2px offset. No heavy glow, no shadow bloom.
- **Search:** Same field with a leading inline magnifier SVG (ink-muted). Placeholder uses Ink Muted (must still clear 4.5:1).
- **Error / Disabled:** Error text in Clay Deep with a short, human message; disabled drops to 55% opacity and removes the shadow.

### Navigation
- **Style:** Sticky top bar, solid Warm White with a bottom Hairline. No translucency, no blur.
- **Typography:** Wordmark in Fraunces; controls in Figtree Title.
- **States:** Active route marked by Ink text + a 2px Clay underline; inactive in Ink Muted.
- **Mobile:** The filter bar collapses into a single "Filter" sheet trigger; primary action is thumb-reachable bottom-right.

### Signature Components — Session Card, Claim Card, Entity Chip, Mind-map

The authoritative IA (PRD-02 + `spa-phase1.md`) is **session-first**: the landing is a recency-ordered timeline of sessions, each headlined by its **Crystal** (a 1–2 sentence narrative + key outcomes + open threads). The session is the heartbeat; the atomic claim lives inside it.

- **Session Card (top-level signature).** A session rendered as a page from a journal. Crystal narrative in Fraunces (Headline), a quiet counts pill in Figtree Label (`N facts · M entities · K open threads`), relative date ("3 days ago"). Soft surface, Soft shadow at rest → Raised on hover. Tapping opens the Session Detail at `/sessions/YYYY-MM-DD/<hash>`. This is the unit the warm direction has to win on first — it's the competitive wedge (visible, encrypted, portable memory).
- **Claim Card (atomic unit inside a session).** A single memory rendered to read and groom, not a table row. Text in Fraunces, a type pill, source/scope as Ink-Muted Label metadata, exact ISO timestamp in Mono on expand. Curation at the edges: pin toggles in place with an optimistic Clay fill; retype/delete from a calm overflow affordance (delete = 10s undo toast, reversible in tone).
- **Entity Chip.** A small Figtree-Label pill (hairline border at rest) linking to an entity page / mind-map drill-down at `/kg/entity/<slug>`. Hover lifts to clay-tint. Quiet by default — entities are navigation, not decoration.
- **Mind-map (`/kg`).** Hierarchical tree-of-topics by default (mobile-feasible), force-directed mini-canvas on entity tap (≤30 nodes), static entity list as the kg-3 fallback. Nodes carry warmth through clay-on-warm-white, never the neon-graph cliché. Edges are hairline; the active/focused node is the one clay moment.

Curation actions (pin / retype / set-scope / tombstone) must reach parity with the agent tool surface (`totalreclaw_pin`/`unpin`/`retype`/`set_scope`/`forget`), and feel effortless — the reward loop.

## 6. Do's and Don'ts

### Do:
- **Do** keep the background Warm White (`#FBFAF8`); carry warmth through the Clay accent and Fraunces, not a tinted field.
- **Do** set the user's memory text in Fraunces and UI chrome in Figtree, so what's worth reading looks different from the controls.
- **Do** keep Clay to ≤10% of any screen (the One Clay Rule): primary action, active state, pinned.
- **Do** tint every shadow toward warm ink (`rgba(43,40,36,…)`), never pure black.
- **Do** always show the type *label* text on badges; color reinforces, it never stands alone.
- **Do** write privacy and curation copy in plain second person ("your memory", "only you can read this").
- **Do** keep delete calm and reversible in tone (30-day recovery), not a red alarm.

### Don't:
- **Don't** build a generic SaaS dashboard: no identical card grids, no hero-metric tiles (big number + tiny label + gradient), no Linear/Vercel chrome.
- **Don't** let it read as a crypto wallet: no neon-on-dark, no decorative gradients, no glassmorphism, and never surface chain / wallet / gas / Smart-Account language to the user.
- **Don't** let it slump into a raw developer tool: no JSON dumps, no terminal aesthetic, no monospace for human-readable content, no unstyled data tables.
- **Don't** clone agentmemory.dev's look; reference its information architecture if useful, never its visuals.
- **Don't** use a cream / sand / beige body background, gradient text, side-stripe `border-left` accents, or glass cards. All forbidden.
- **Don't** put two competing Clay elements in one viewport.
