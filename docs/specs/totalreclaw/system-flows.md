# TotalReclaw — System Flow Reference

**Audience:** developers, contributors, and curious users who want a visual mental model of how TotalReclaw's moving parts connect.

**Scope:** this document shows the sequence of events for each major user-visible capability. Data-model references point to `docs/specs/totalreclaw/architecture.md`; decision-tree references point to `docs/plans/2026-04-13-phase-2-design.md`.

All diagrams are written in Mermaid. GitHub, VS Code, and most modern Markdown renderers display them natively — if you're reading the raw file, look for the ASCII fallback in the "What's happening here" section under each diagram.

---

## Contents

1. [Write path — storing a new fact (no conflict)](#1-write-path--no-conflict)
2. [Write path — silent auto-resolution of a contradiction](#2-write-path--auto-resolution)
3. [User override — pinning a claim that was just auto-superseded](#3-user-override--pin-after-auto-resolution)
4. [Voluntary pinning — pinning an untouched claim](#4-voluntary-pin)
5. [Weight-tuning loop — how the system learns from overrides](#5-weight-tuning-loop)
6. [Read path — digest injection at session start](#6-read-path--digest-injection)
7. [OpenClaw Wiki integration — reading TotalReclaw claims from Wiki](#7-wiki-integration--read)
8. [OpenClaw Wiki integration — ingesting Wiki's curated pages into TotalReclaw](#8-wiki-integration--write)

---

## 1. Write path — no conflict

**User story:** "I just told my agent I prefer dark mode. The agent extracts the fact and stores it. Nothing conflicts."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent as AI Agent
    participant Plugin as TR Client
    participant Core as TR Core (WASM)
    participant Relay
    participant Chain as Subgraph + Chain

    User->>Agent: "I prefer dark mode"
    Agent->>Agent: extract fact via LLM<br/>{text, type, entities, confidence}
    Agent->>Plugin: store fact
    Plugin->>Core: buildCanonicalClaim(fact)
    Core-->>Plugin: canonical Claim JSON
    Plugin->>Core: encrypt(blob) + generate<br/>blind indices + entity trapdoors
    Plugin->>Chain: searchSubgraph(entity trapdoors)
    Chain-->>Plugin: no similar existing claims
    Plugin->>Relay: submitFactBatch(new fact)
    Relay->>Chain: UserOp signing + submission
    Chain-->>Relay: confirmed
    Relay-->>Plugin: tx hash
    Plugin-->>Agent: stored
```

**What's happening here:** the plugin builds a canonical `Claim` blob, encrypts it, generates trapdoors, checks for existing claims about the same entities, finds none, and writes. Phase 1 behavior. No Phase 2 primitives are exercised.

---

## 2. Write path — auto-resolution

**User story:** "Three months ago I told my agent I preferred Vim. Today I said I prefer VS Code. The system should notice the contradiction and silently pick the right answer."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent as AI Agent
    participant Plugin as TR Client
    participant Core as TR Core (WASM)
    participant Chain as Subgraph + Chain
    participant DecLog as ~/.totalreclaw/<br/>decisions.jsonl

    User->>Agent: "I prefer VS Code now"
    Agent->>Plugin: store extracted fact
    Plugin->>Plugin: build Claim + trapdoors
    Plugin->>Chain: search by entity trapdoor "editor"
    Chain-->>Plugin: existing claim "prefers Vim"
    Plugin->>Plugin: decrypt + extract embedding
    Plugin->>Core: detectContradictions(<br/>new, [existing])
    Core-->>Plugin: Contradiction{sim=0.45}<br/>(in contradiction band 0.3-0.85)
    Plugin->>Core: resolvePair(new, existing,<br/>weights from weights.json)
    Core-->>Plugin: ResolutionOutcome{<br/>winner=new, score_delta=0.12,<br/>winner_components, loser_components}
    Plugin->>DecLog: append auto-resolution row<br/>(includes all 8 component values)
    Plugin->>Chain: batch(tombstone old + write new)
    Chain-->>Plugin: tx hash
    Plugin-->>Agent: stored, 1 claim superseded
    Agent-->>User: (next session digest)<br/>"I noticed you switched from<br/>Vim to VS Code — updated my memory"
```

**What's happening here:** when the dedup pass doesn't catch it (similarity below the dedup threshold), contradiction detection runs. The core formula picks a winner using weights loaded from `weights.json`. Both score breakdowns are persisted to `decisions.jsonl` — critical for the tuning loop, which needs the component-level data if the user later overrides.

**Component breakdown** in the log row:
```json
{
  "winner_components": {"confidence": 0.90, "corroboration": 1.0,  "recency": 0.81, "validation": 0.7, "weighted_total": 0.83},
  "loser_components":  {"confidence": 0.80, "corroboration": 1.73, "recency": 0.33, "validation": 0.7, "weighted_total": 0.73}
}
```

---

## 3. User override — pin after auto-resolution

**User story:** "The system picked VS Code but that's wrong — I still use Vim for quick edits. I tell my agent, and it pins the Vim claim back."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent
    participant Plugin
    participant DecLog as decisions.jsonl
    participant FBLog as feedback.jsonl
    participant Chain

    Note over User,Chain: PRE-CONDITION: auto-resolution from<br/>Diagram 2 already happened.

    User->>Agent: "wait, I still use Vim for<br/>quick edits"
    Agent->>Agent: LLM recognizes override intent<br/>(from prior digest mention)
    Agent->>Plugin: totalreclaw_pin(vim_claim_id)
    Plugin->>DecLog: find most recent row where<br/>loser_claim_id == vim_claim_id
    DecLog-->>Plugin: row with full components
    Plugin->>FBLog: append FeedbackEntry<br/>(user_decision=pin_a,<br/>winner_components, loser_components)
    Plugin->>Plugin: rebuild canonical Claim<br/>with st="p" + sup=old_id
    Plugin->>Plugin: regenerate trapdoors<br/>(critical so pinned claim<br/>is still searchable)
    Plugin->>Chain: batch(tombstone current + write pinned)
    Chain-->>Plugin: tx hash
    Plugin-->>Agent: pinned
    Note over User,FBLog: Feedback row awaits the next<br/>digest compile to be consumed<br/>by the tuning loop (Diagram 5).
```

**What's happening here:** the pin tool is smarter than a simple status flip. It searches `decisions.jsonl` for the most recent auto-resolution that listed the pinned claim as a loser. If found, it writes a counterexample row to `feedback.jsonl` — this is the signal that lets the tuning loop adjust weights later. The new pinned claim gets fresh trapdoors so it stays findable via normal recall.

---

## 4. Voluntary pin

**User story:** "I want my agent to never forget my favorite color. I pin the claim directly — no contradiction, no override, just a reinforcement."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent
    participant Plugin
    participant DecLog as decisions.jsonl
    participant FBLog as feedback.jsonl
    participant Chain

    User->>Agent: "please always remember:<br/>my favorite color is teal"
    Agent->>Plugin: totalreclaw_pin(teal_claim_id)
    Plugin->>DecLog: lookup by fact_id
    DecLog-->>Plugin: not found
    Note over Plugin,FBLog: No prior auto-resolution → this is a<br/>voluntary pin, not a counterexample.<br/>No feedback row written.
    Plugin->>Plugin: rebuild Claim with st="p"<br/>+ regen trapdoors
    Plugin->>Chain: batch(tombstone + write pinned)
    Chain-->>Plugin: tx hash
    Plugin-->>Agent: pinned
    Note over User,Chain: Weights unchanged — there was no<br/>formula decision to learn from.
```

**What's happening here:** voluntary pinning still writes the on-chain status change (so the pin propagates across devices) but does NOT generate a tuning signal. The feedback log is reserved for real counterexamples — cases where the formula made a decision the user disagreed with. A voluntary pin isn't a disagreement.

---

## 5. Weight-tuning loop

**User story:** "After a week of corrections, the system should have learned that I care more about recency than about how many times a fact was extracted."

```mermaid
sequenceDiagram
    autonumber
    participant Plugin
    participant FBLog as feedback.jsonl
    participant Weights as weights.json
    participant Core as TR Core (WASM)

    Note over Plugin,Core: Runs at digest-compile time,<br/>async, non-blocking.

    Plugin->>Weights: read current weights<br/>+ last_tuning_ts
    Weights-->>Plugin: {weights, last_tuning_ts}
    Plugin->>FBLog: read entries where ts > last_tuning_ts
    FBLog-->>Plugin: [FeedbackEntry, ...]

    loop for each feedback entry
        Plugin->>Core: feedbackToCounterexample(entry)
        Core-->>Plugin: Counterexample or null
        alt counterexample is not null
            Plugin->>Core: applyFeedback(weights, cx)
            Core-->>Plugin: adjusted_weights
        else
            Note over Plugin: skip — user agreed with formula
        end
    end

    Plugin->>Weights: write adjusted weights<br/>+ new last_tuning_ts

    Note over Plugin,Core: NEXT contradiction detection<br/>uses the adjusted weights.
```

**What's happening here:** the tuning loop is a pure function sequence. `feedbackToCounterexample` returns null if the user's decision agreed with the formula (no gradient signal). For real counterexamples, `applyFeedback` runs a small gradient step — at most ±0.02 per component — clamped so weights stay in `[0.05, 0.60]` and sum near 1.0. After 50 corrections, a user whose preferences differ from the defaults converges on personalized weights.

The loop is idempotent via `last_tuning_ts` — re-running the same feedback entries does nothing. Safe to trigger on every digest compile.

**What DOESN'T happen here:**
- No cross-user aggregation (each user's weights are private, per-device initially)
- No uploads to a server
- No reading of claim text — only scores and IDs are in the feedback log

---

## 6. Read path — digest injection

**User story:** "When I start a new conversation, the agent should already know who I am without having to search my whole memory."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent
    participant Plugin
    participant Chain
    participant Core

    User->>Agent: new session starts
    Agent->>Plugin: before_agent_start hook fires
    Plugin->>Chain: search by DIGEST_TRAPDOOR
    Chain-->>Plugin: latest digest claim (or none)

    alt digest exists
        Plugin->>Plugin: decrypt digest blob
        Plugin->>Chain: query max(createdAt) of user's facts
        Chain-->>Plugin: current_max_ts
        alt digest is stale (guard allows)
            Plugin->>Plugin: fire-and-forget recompile<br/>(async, not blocking)
        end
        Plugin-->>Agent: inject digest.prompt_text
    else no digest yet
        Plugin->>Plugin: fall back to individual-fact recall<br/>(legacy Phase 1 path)
        Plugin->>Plugin: fire-and-forget first-time compile
        Plugin-->>Agent: inject top-8 fact list
    end

    Agent-->>User: "Hi Pedro! I know you're a<br/>software engineer in Lisbon, ..."
```

**What's happening here:** digest injection is the fast path. One decryption + one prompt insertion replaces N search queries + N decryptions on every session start. Staleness is checked cheaply (single subgraph query for `max(createdAt)`). Recompilation happens in the background so the user never waits on it. First-time users fall through to the legacy per-fact recall path and get their first digest compiled asynchronously for next session.

---

## 7. Wiki integration — read

**User story:** "I use OpenClaw daily but sometimes I chat with Claude Code. When I browse my Wiki in OpenClaw, I want to see facts Claude Code extracted — not just the ones memory-core saw."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Agent as OpenClaw Agent
    participant MemCore as memory-core
    participant Wiki as memory-wiki
    participant TRSupp as TR Corpus Supplement
    participant Relay
    participant Chain

    User->>Agent: "what do I know about PostgreSQL?"
    Agent->>MemCore: memory_search corpus=all
    MemCore->>MemCore: search own corpus<br/>(MEMORY.md, daily notes)
    MemCore->>Wiki: corpus supplement: search
    Wiki-->>MemCore: local Markdown hits
    MemCore->>TRSupp: corpus supplement: search
    TRSupp->>TRSupp: generate trapdoors from query
    TRSupp->>Relay: searchSubgraph(owner, trapdoors)
    Relay->>Chain: GraphQL query
    Chain-->>Relay: encrypted facts
    Relay-->>TRSupp: encrypted facts
    TRSupp->>TRSupp: decrypt + rerank<br/>(local, client-side)
    TRSupp-->>MemCore: MemoryCorpusSearchResult[]<br/>with provenanceLabel<br/>"TotalReclaw: from Claude Code"
    MemCore-->>Agent: merged results from<br/>all 3 sources
    Agent-->>User: answer including cross-agent facts
```

**What's happening here:** TotalReclaw registers as a `MemoryCorpusSupplement` via OpenClaw's existing public SDK API. Wiki's compile pass calls our supplement alongside its own sources. Results are tagged with provenance so the user can see "this came from Claude Code yesterday" without Wiki needing to know anything about TotalReclaw. No schema sharing — we translate from our `Claim` format to Wiki's `MemoryCorpusSearchResult` shape at the boundary.

---

## 8. Wiki integration — write

**User story:** "My OpenClaw Wiki just compiled into nice curated entity pages. I want Claude Code to see those curated pages next time it queries, not raw extractions."

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant CLI as openclaw wiki compile
    participant Wiki as memory-wiki
    participant Cache as .openclaw-wiki/cache/<br/>claims.jsonl
    participant Hook as after_tool_call
    participant TRPlugin
    participant Core
    participant Chain

    User->>CLI: openclaw wiki compile
    CLI->>Wiki: invoke compile pipeline
    Wiki->>Wiki: read memory-core public<br/>artifacts (MEMORY.md, etc.)
    Wiki->>Wiki: synthesize entity + concept pages
    Wiki->>Cache: write claims.jsonl<br/>(one curated claim per line)
    Wiki-->>CLI: compile done
    CLI-->>Hook: after_tool_call(toolName="wiki_compile")
    Hook->>TRPlugin: onWikiCompileComplete()
    TRPlugin->>Cache: read claims.jsonl
    Cache-->>TRPlugin: synthesized claims
    loop for each synthesized claim
        TRPlugin->>Core: build canonical Claim<br/>sourceAgent="openclaw-wiki-compile"<br/>confidence=0.95
        TRPlugin->>Core: store-time dedup check
        alt matching raw claim exists
            Note over TRPlugin: Wiki-curated supersedes raw<br/>(higher confidence wins)
            TRPlugin->>Chain: batch(tombstone raw + write curated)
        else no match
            TRPlugin->>Chain: batch(write curated)
        end
    end
```

**What's happening here:** after every Wiki compile, the `after_tool_call` hook fires. The plugin reads the newly written `claims.jsonl` (documented stable path) and ingests each synthesized claim. High confidence (0.95) means these curated claims naturally win store-time dedup supersession against raw auto-extracted claims about the same entities — so non-OpenClaw agents like Claude Code see the Wiki-curated version in their next recall, even though they never ran Wiki themselves.

**Critical detail:** when ingesting, we preserve the **original extraction timestamp** from Wiki's claim rows, not `Date.now()`. This prevents recency weighting from treating recompiled old claims as "newer" than fresh cross-agent claims. See `P2-10` in the Phase 2 design doc.

---

## Where to read more

- **Data model** (`Claim`, `Entity`, `Digest`, `ClaimStatus`): `rust/totalreclaw-core/src/claims.rs`
- **Contradiction formula + weight tuning**: `rust/totalreclaw-core/src/contradiction.rs` + `docs/plans/2026-04-13-phase-2-design.md` §P2-3
- **Pin/unpin semantics**: `docs/plans/2026-04-13-phase-2-design.md` §P2-4
- **Digest compilation**: `rust/totalreclaw-core/src/digest.rs` + `docs/specs/totalreclaw/architecture.md` §4.4
- **Corpus supplement + Wiki bridge**: `docs/plans/2026-04-13-phase-2-design.md` §P2-10
- **Encryption, trapdoors, blind indices**: `docs/specs/totalreclaw/architecture.md`
- **Per-client feature matrix**: `CLAUDE.md` "Feature Compatibility Matrix"
