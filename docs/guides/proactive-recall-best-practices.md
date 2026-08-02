# Proactive Recall Best Practices for Personal Agents

> **Audience:** LLM agents running TotalReclaw as a memory provider (Hermes, OpenClaw, Claude Desktop / Claude Code via MCP, etc.).
> **Scope:** When an agent should call `totalreclaw_recall` *itself*, on top of whatever automatic recall its host performs.

## How automatic recall actually works

TotalReclaw's automatic recall is **narrower than most agents assume** — knowing exactly what it does (and does not) do is the whole point of this guide.

### Hermes (Python client)

Hermes wires TotalReclaw through lifecycle hooks in `totalreclaw/hermes/hooks.py`:

- **`pre_llm_call` — auto-recall, first turn only.** On the *first* turn of a session (`is_first_turn`), the plugin runs `auto_recall(user_message, state, top_k=8)` and injects the result into that turn's context as a `## Relevant memories from TotalReclaw` block. The query is the user's **first message, verbatim**.
- **`post_llm_call` — auto-extraction, every N turns.** This writes memories (`turn_count % extraction_interval == 0`); it does **not** read or inject anything. Extraction ≠ recall — don't confuse the two.

There is **no per-turn automatic recall.** After turn 1, memories enter the conversation only when the agent explicitly calls `totalreclaw_recall`. There is no background prefetch, no recall cache, and no `<memory-context>` refresh on later turns.

### OpenClaw plugin

OpenClaw's `before_agent_start` hook auto-recalls before each message (see `skill/plugin/README.md`). This is broader than Hermes (per-message, not first-turn-only), but it is still **query-driven** by the current message.

### MCP hosts (Claude Desktop, Claude Code)

No lifecycle hooks at all — the host agent must call `totalreclaw_recall` explicitly for every recall. All of the practices below apply with full force.

### What every path shares

Recall is **query-driven**: it searches for memories semantically related to whatever text it was handed. The retrieval pipeline (BM25 + cosine + RRF + source-weighted rerank, `top_k` default 8/16 by tier) is sharp on specific queries, and it has a **server-side broadened fallback** — when the trapdoor search returns zero hits for a vague query, the relay returns the owner's recent facts instead. But nothing decides *for* the agent that a recall is worth doing on turn 5, or that a standing directive should be loaded before the user re-triggers it.

## The gaps automatic recall leaves

1. **Low-signal session openers.** First-turn recall keys on the first message. "continue", "hey", "where were we?" carry no topical signal, so the recall is weak (the broadened fallback returns *recent* facts, not necessarily the *relevant* ones).
2. **No mid-session recall (Hermes/MCP).** After turn 1, a topic shift to something discussed weeks ago surfaces nothing automatically. The agent has to ask for it.
3. **Directives and standing preferences never load on their own.** A `directive` like "never mention X" or a preference like "always answer concisely" is only surfaced if a query happens to match it — yet these should shape *every* turn. This is the single biggest source of user-visible re-corrections.

## Best practices (do these today — no code changes)

### 1. Session-start recall when the opener is low-signal

If the first message has no topical hook ("continue", "where were we?", a bare greeting), the automatic first-turn recall has nothing to work with. Call recall yourself with a broad profile query:

```
totalreclaw_recall(query="user preferences directives active projects recent commitments", top_k=8)
```

Skip this when the first message already has clear topical signal — the automatic first-turn recall (Hermes) or `before_agent_start` (OpenClaw) already covered it. Don't duplicate it.

### 2. Preference / directive check before acting on the user's behalf

Before a recommendation or an action that depends on the user's standing preferences or rules, recall that domain — even if nothing in the current context mentions it:

```
totalreclaw_recall(query="user preferences for <domain>; always/never rules", top_k=5)
```

Examples: recommending a restaurant → dietary restrictions; suggesting a library → tech-stack preferences; drafting a message → tone/style directives.

### 3. Topic-shift recall (especially mid-session)

Because there is no automatic recall after turn 1 on Hermes/MCP, **you** are the only thing that will surface history when the user pivots. Recall when:

- the user moves to a domain you haven't touched this session,
- they name a project / person / place you have no context for,
- they say "we talked about this" / "do you remember…".

```
totalreclaw_recall(query="<topic> decisions next steps context", top_k=8)
```

### 4. When recall returns 0, broaden — don't give up

If `totalreclaw_recall` returns nothing for a query where memories should exist, retry once with a **broader** query (fewer, more general terms). The pipeline's server-side broadened fallback already widens vague queries, so a second, looser call often surfaces what a narrow one missed. There is no separate "session search" tool — broadening the recall query *is* the fallback.

### 5. Read what's already injected first

On the turn where auto-recall fired (turn 1 on Hermes; every turn on OpenClaw), a `## Relevant memories from TotalReclaw` block is already in your context. Read it before deciding whether an extra recall is warranted — if it covers the topic, don't spend a tool call repeating it.

## Anti-patterns

- ❌ **Recalling on every turn regardless.** Wastes tokens and calls. Recall when the situation (session-start / preference-dependent / topic-shift) calls for it.
- ❌ **Recalling with the user's message verbatim** when auto-recall already ran on it — that duplicates the automatic pass. Use a *broader* or *different* angle (profile / directives) instead.
- ❌ **Ignoring the injected `## Relevant memories` block** and recalling blind.
- ❌ **Recalling without acting on the result.** If it won't change your answer, don't call it.
- ❌ **Recall chains** (recall → recall → recall in one turn). One or two targeted calls per turn is the ceiling.

## Decision matrix

| Situation | Action | Priority |
|-----------|--------|----------|
| New session, low-signal opener ("continue") | Broad profile recall (§1) | High |
| User asks about their preferences / history | `totalreclaw_recall` before answering | High |
| About to recommend / act on a preference | Preference-check recall (§2) | High |
| User shifts to a topic from a past session | Topic-shift recall (§3) | Medium |
| Auto-injected block seems thin | One supplementary recall, broader query | Medium |
| Recall returned 0 unexpectedly | Retry once, broader query (§4) | Medium |
| First message already had clear topical signal | Nothing — auto-recall covered it | — |

## Future: automated session-start sweep (proposed, not implemented)

The §1 practice is a *manual* fix for the low-signal-opener and directive-loading gaps. The automated version is a small, well-scoped enhancement — **not the new subsystem it might sound like**, because the infrastructure already exists:

- First-turn recall already runs in `pre_llm_call` (`is_first_turn`), and its result is already injected via the returned `{"context": …}`.
- The only change: on the first turn, **also** run one broad recall for durable, always-relevant context (`directive` + `preference` + active `commitment` memories) and merge it with the message-keyed recall — so standing rules load regardless of what the opener says.

This is deliberately the *minimal* option:

- **Adopt:** one extra broad recall on turn 1, scoped to directive/preference/commitment types, reusing the existing injection path.
- **Reject for v1:** running 2–3 parallel recalls on *every* session start (fixed latency + relay load on the many sessions that don't need it); and passing recall results through an extra LLM call to synthesize a "user brief" (adds per-session latency/cost and front-loads a dense personal profile into every system prompt — a token and privacy cost even for throwaway sessions).

Tracked as a follow-up; until it ships, §1 is the way to close the gap.
