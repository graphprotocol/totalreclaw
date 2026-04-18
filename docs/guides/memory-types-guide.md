# Understanding Memory Types

**Target audience:** end users who want to know what is being stored and how to influence it.
**Applies to:** TotalReclaw v1 (all clients).

This guide explains the v1 memory model in plain English. You do not need to think about any of this to use TotalReclaw — the agent picks the right type automatically. Read this when you want to understand what is being stored, or when you want to override the agent's choice.

---

## The big picture

Every memory TotalReclaw keeps has four pieces of information attached:

1. **Type** — what kind of statement it is (6 options).
2. **Source** — who wrote it (user, user-inferred, assistant, external, derived).
3. **Scope** — what area of your life it belongs to (work, personal, health, ...).
4. **Volatility** — how long it is likely to stay true (stable, updatable, ephemeral).

You never set these manually. The agent infers them from context during extraction. You only override them when you notice the agent got something wrong — and you override by speaking naturally, not by editing JSON.

---

## The six memory types

Each type maps to a different kind of statement you make in conversation. The examples below use natural phrasing you might actually say to an agent.

### `claim`

A descriptive statement about yourself or the world.

- "I live in Lisbon."
- "The database runs on Postgres 16."
- "My daughter's name is Maya."
- "I chose Postgres for the analytics store because the data is relational."  ← stored as `claim` with a `reasoning` field.

Use `claim` when something just *is*. If the statement changes (job, city, relationship status), that is fine — `claim` can be superseded by a later `claim`.

### `preference`

An expression of taste, like/dislike, or style.

- "I prefer dark mode in all my editors."
- "I don't like cilantro."
- "I like my coffee black."
- "Sci-fi over fantasy, always."

`preference` is about taste. If you want the agent to follow a rule, use `directive` (below). If you want to record a fact about yourself, use `claim`.

### `directive`

A rule the agent should follow going forward.

- "Always cite a source when you make a factual claim."
- "Never auto-commit to my git repo."
- "When writing SQL, prefer CTEs over subqueries."
- "Don't use dark UI for data-dense screens."

`directive` is imperative: you are telling the agent how to behave. This is different from `preference` (which is just taste — "I like dark mode" does not mean "always produce dark UI").

### `commitment`

A future-facing intent — something you are going to do.

- "I'll ship v2 on Friday."
- "I'm going to start training for a marathon next month."
- "I'll call the dentist tomorrow."

A `commitment` about an event that already happened becomes an `episode` (see below).

### `episode`

A notable event that happened at a specific time.

- "Deployed v1.0 on March 15."
- "We flew to Bangkok in November."
- "Had a great meeting with Maya's teacher on Tuesday."

An `episode` is a `narrative` speech act — it tells the story of something that occurred. When you recall by time ("what happened last March?") you are searching episodes.

### `summary`

A derived synthesis, not a single-turn extraction. The agent creates these automatically at session end or before compaction. You do not usually author `summary` memories yourself — they are computed from longer conversation threads.

---

## Boundary cases

The agent picks the right type by asking a structural question, not by matching keywords. Here are the tests:

- **claim vs preference:** "I live in Lisbon" = claim. "I prefer Portuguese over Spanish" = preference. *Test: would replacing this affect a decision, or just taste?*
- **claim vs directive:** "Postgres handles my analytics workload" = claim. "Always use Postgres for analytics" = directive. *Test: is this descriptive, or commanding future behavior?*
- **directive vs preference:** "I prefer dark mode" = preference. "Never use dark UI for data-dense screens" = directive. *Test: do you want this enforced, or just considered?*
- **commitment vs claim:** "Shipping v2 Friday" = commitment. "v2 shipped Friday" = episode.
- **episode vs claim:** "Deployed v1.0 on March 15" = episode (event). "v1.0 is deployed" = claim (state).

---

## Source — who wrote it

Every memory is tagged with who authored the content. This matters for recall: user-authored claims rank higher than anything else.

| Source | What it means | Recall weight |
|---|---|:-:|
| `user` | You said it directly | 1.00 |
| `user-inferred` | Extracted from your signals (but not a direct quote) | 0.90 |
| `derived` | Computed (debrief, digest, summary) | 0.70 |
| `external` | Imported from another tool (ChatGPT export, Mem0, etc.) | 0.70 |
| `assistant` | The assistant said it and you didn't affirm it | 0.55 |

The `assistant` weight is deliberately low. It prevents a failure mode documented in other memory systems: the assistant makes an inference in its response, that inference gets extracted as a "fact", and then it ranks the same as things you actually said. TotalReclaw tags it but down-weights it instead of dropping it entirely, so genuine content you shared (e.g. a receipt the assistant summarised) is still recoverable.

---

## Scope — what area of your life

Memories are grouped into 8 scopes:

- `work` — your job, colleagues, projects, tools.
- `personal` — things about you that are not work-related.
- `health` — medical, fitness, diet, allergies.
- `family` — partner, kids, parents, relatives.
- `creative` — writing, music, art, side projects.
- `finance` — money, budgets, taxes, investments.
- `misc` — doesn't fit elsewhere.
- `unspecified` — the agent didn't identify a clear scope.

The agent picks the scope automatically from conversation context. You can override: "that's actually a work thing" or "file that under health".

---

## Volatility — how long it stays true

- `stable` — unlikely to change for years (your name, allergies, birthplace, unchanging preferences).
- `updatable` — changes occasionally (job, active project, partner's name, current city).
- `ephemeral` — short-lived (today's task, this week's itinerary, a specific question you had yesterday).

Volatility is assigned by the agent during extraction. Ephemeral memories can auto-expire (14 or 30 days depending on type); stable and updatable do not expire.

You can pin any memory regardless of volatility. Pinning overrides auto-expiry.

---

## Overriding the agent — natural language

You never edit memories by hand. You talk to the agent and it picks the right tool.

### Pin a memory ("never forget this")

Say: *"pin that last thing"* or *"remember this forever"* or *"never forget I'm allergic to shellfish"*.

The agent calls `totalreclaw_pin` behind the scenes. Pinned memories cannot be auto-superseded and do not expire.

### Unpin

Say: *"unpin the shellfish allergy"* or *"it's fine to update that if things change"*.

### Re-type a memory

Say: *"that was actually a rule, not a preference"* or *"file that as a commitment, not a claim"*.

The agent calls `totalreclaw_retype`.

### Re-scope a memory

Say: *"that's a work thing, not personal"* or *"file that under health"*.

The agent calls `totalreclaw_set_scope`.

### Forget a memory

Say: *"forget what I said about the old project"* or *"delete my diet notes from last week"*.

The agent calls `totalreclaw_forget`. On the managed service this writes a tombstone on-chain; on self-hosted it deletes the row.

---

## Explicit remember — optional

If you want to write something into memory without a natural conversation, you can ask directly: *"remember that I prefer PostgreSQL for analytics"*. The agent calls `totalreclaw_remember` with type + importance + scope set from your statement. This works the same way as auto-extraction but skips the conversation-extraction step.

---

## Where the settings live

Every one of these fields lives **inside the encrypted memory blob**. The server sees only ciphertext; the type, source, scope, and volatility are all readable to you (locally, after decryption) but invisible to the server.

This is the same privacy model TotalReclaw has always had — v1 adds more fields but keeps them all end-to-end encrypted.

---

## Related

- [v1 migration guide](./v1-migration.md) — what changed from v0.
- [Memory types spec](../specs/totalreclaw/memory-taxonomy-v1.md) — full normative spec (for implementers).
- [Tiered retrieval](../specs/totalreclaw/tiered-retrieval.md) — how `source` affects recall.
- [Feature comparison](./feature-comparison.md) — which clients support which features.
