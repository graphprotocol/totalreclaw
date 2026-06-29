# Product

> Scope: this file governs design work on the **Vault SPA** (`app/`) — the web UI at `app.totalreclaw.xyz`.
> It is intentionally placed at the repo root so impeccable commands find it from the monorepo cwd.
> The rest of the monorepo (relay, clients, contracts) is out of scope for design.

## Register

product

## Users

People opening their **encrypted AI-memory vault** to see, groom, and trust what their agents have remembered about them.

- **Returning Hermes users (primary).** Already running an agent that auto-extracts memories. Come to the SPA to audit and curate periodically, often on a phone, in spare moments. Want to confirm "what does my AI actually know about me?" and tidy it.
- **SPA-first newcomers.** Create a vault on the web before pairing any agent. Need a calm first-run that doesn't assume crypto literacy.
- **Cross-device users.** Same vault on phone and laptop via per-device passkeys. Expect continuity, not re-setup.

The job to be done: *open my memory, read it like it's mine, fix what's wrong, trust it's private.* The core loop is **audit → curate (pin / retype / delete) → clean**. Primary device is the phone.

## Product Purpose

The Vault SPA is the **visible, human face** of an otherwise invisible memory layer. Agents write E2EE memory claims on-chain; the SPA is where a person actually *reads and curates* them in a browser, client-side decrypted, server-blind.

It exists because a memory vault you never open is a vault you don't trust. Success = users open it regularly, recognize themselves in it, and leave having pinned, retyped, or deleted something — without ever thinking about wallets, chains, or ciphertext.

It is one of three current product bets (alongside Imports and Hermes auth-hardening).

## Information Architecture (source of truth: PRD-02 + spa-phase1.md, approved 2026-05-26)

The navigation primitive is the **session**, not the flat claim. These are spec'd, locked Phase-1 scope — design must target them, not the current in-tree prototype's flat list:

- **Session timeline** (landing): recency-ordered session cards, each headlined by its **Crystal** (1–2 sentence narrative + key outcomes + open threads + counts). URL `/sessions/YYYY-MM-DD/<8-char-hash>`. (US-3/US-4, G-3)
- **Session detail**: Crystal + the atomic facts extracted that session + entity chips.
- **Mind-map / KG** (`/kg`): hierarchical tree-of-topics by default, force-directed mini-canvas on entity tap, static entity pages as the kg-3 fallback. Navigates topics → sessions → entities → related facts/summaries. (US-5, G-5)
- **Curation** everywhere: pin / retype / set-scope / tombstone, at parity with the agent tools.
- **Bootstrap + Pair** (passkey, QR/deep-link/copy-paste) and **Active devices** round out v1.

Consequence: the prototype's `VaultPage` (flat list) → `TimelineView`; `ClaimPage` → `SessionDetailView`; `PairPage` → `BootstrapView` + `PairView`. The Claim Card is the atomic unit *inside* a session, not the top level.

## Brand Personality

**Warm, personal, trustworthy.** It's *your* memory — intimate and human, not a clinical admin console. Reassuring, not alarming, about privacy. The voice speaks in plain second person ("your memory", "what your agent remembered"), never in jargon. Calm confidence over urgency; an invitation to look, not a wall of controls.

Emotional goal: a user should feel *this is mine, and it's safe* — closer to opening a personal journal than a database dashboard.

## Anti-references

The interface must **not** read as any of these:

- **Generic SaaS dashboard.** No endless identical card grids, no hero-metric tiles (big number + tiny label + gradient), no Linear/Vercel-clone chrome. Curation is a reading-and-grooming experience, not a KPI panel.
- **Crypto / web3 wallet.** No neon-on-dark, no gradients-as-decoration, no glassmorphism, no chain/Smart-Account/gas language surfaced to the user. There is ERC-4337 plumbing underneath; the user must never see it.
- **Raw developer tool.** No JSON dumps, no terminal aesthetic, no unstyled data tables, no monospace-everything. The current bare-Tailwind build risks landing here; it's the trap to climb out of.
- **agentmemory.dev clone.** That competitor may be referenced for information architecture and pattern ideas, but never copied visually. TotalReclaw carries its own warm branding.

## Design Principles

1. **Your memory, made legible.** Turn machine-extracted, typed claims into something a human enjoys reading and grooming. Plain language, recognizable structure, no schema leaking through.
2. **Trust through calm, not theater.** Convey end-to-end encryption via restraint, clarity, and honest copy — never lock icons everywhere or scare-language. Hide all chain/wallet plumbing completely.
3. **Curation is the reward.** Pin, retype, and delete must feel effortless and quietly satisfying. The product earns repeat opens by making grooming feel good, not like chores in a settings panel.
4. **Warm minimalism.** Inviting and personal without tipping into a SaaS card grid or a raw dev tool. Restraint with a pulse, not sterility.
5. **Phone-first, glanceable.** The primary device is a phone in spare moments. Scannable at a glance, fast even on a 10k-item vault, thumb-reachable actions.
6. **Transparent, not synthesized.** The discrete fact is the first-class unit: every memory is individually visible, editable, and deletable — never collapsed into an opaque "model of you." Summaries/Crystals are an additive skim layer on top, never a replacement for the explicit, correctable list. (Locked 2026-06-19, learning from ChatGPT walking back its visible memory list — the transparency users missed is our wedge.)

## Accessibility & Inclusion

Target **WCAG 2.1 AA** (confirm if a higher bar is needed).

- Body text ≥ 4.5:1 contrast; large text ≥ 3:1. No light-gray body copy on tinted near-white.
- **Memory type must never be encoded by color alone.** The six types (claim / preference / directive / commitment / episode / summary) carry text labels, not just hue — color is reinforcement, required for color-blind users.
- Every animation needs a `prefers-reduced-motion: reduce` alternative (crossfade or instant).
- Touch targets ≥ 44px; thumb-reachable primary actions on mobile.
- Full keyboard operability for list navigation and curation actions; visible focus states.
