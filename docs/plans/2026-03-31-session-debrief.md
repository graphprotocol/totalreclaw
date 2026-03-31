> **Prerequisites:** Read `CLAUDE.md` and `docs/specs/totalreclaw/client-consistency.md` in the repo root first.

# Session Debrief — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a "session debrief" capability that captures broader context, outcomes, and relationships that turn-by-turn fact extraction misses — available across ALL client implementations regardless of lifecycle hook support.

**Architecture:** A single `totalreclaw_debrief` tool + a canonical debrief prompt that any client can use. For agents with lifecycle hooks (Hermes, OpenClaw, NanoClaw), the debrief runs automatically at session/compaction end. For agents without hooks (MCP, IronClaw, ZeroClaw), the prompt instructs the agent to call the tool proactively at conversation end.

**Tech Stack:** Same LLM call pattern as extraction (auto-detect provider, use cheap model). The debrief prompt receives already-extracted facts as context to avoid duplication. Results stored as `type: "summary"` or `type: "context"` with importance 7-8.

---

## Design: The Two-Path Architecture

The core challenge: not all agent frameworks have "conversation ending" signals.

| Agent | Has end-of-session hook? | Debrief trigger |
|-------|--------------------------|-----------------|
| Hermes | Yes (`on_session_end`) | Automatic via hook |
| OpenClaw | Yes (`pre_compaction`, `before_reset`) | Automatic via hook |
| NanoClaw | Yes (`pre_compact`) | Automatic via hook |
| MCP/Claude Desktop | No | Agent calls `totalreclaw_debrief` tool (prompt-guided) |
| IronClaw | No (cron-based) | Cron routine or agent calls tool |
| ZeroClaw | No (`Memory` trait only) | ZeroClaw calls `debrief()` method (framework integration) |

**Key principle:** The debrief logic is implemented ONCE in each language (TypeScript, Python, Rust), then wired to the appropriate trigger per platform. The prompt, parsing, and storage are identical everywhere.

---

## The Debrief Prompt (canonical — must be identical across all implementations)

```
You are reviewing a conversation that just ended. The following facts were
already extracted and stored during this conversation:

{already_stored_facts}

Your job is to capture what turn-by-turn extraction MISSED. Focus on:

1. **Broader context** — What was the conversation about overall? What project,
   problem, or topic tied the discussion together?
2. **Outcomes & conclusions** — What was decided, agreed upon, or resolved?
3. **What was attempted** — What approaches were tried? What worked, what didn't, and why?
4. **Relationships** — How do topics discussed relate to each other or to things
   from previous conversations?
5. **Open threads** — What was left unfinished or needs follow-up?

Do NOT repeat facts already stored. Only add genuinely new information that provides
broader context a future conversation would benefit from.

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "summary|context", "importance": N}]

- Use type "summary" for conclusions, outcomes, and decisions-of-the-session
- Use type "context" for broader project context, open threads, and what-was-tried
- Importance 7-8 for most debrief items (they are high-value by definition)
- Maximum 5 items (debriefs should be concise, not exhaustive)
- Each item should be 1-3 sentences, self-contained

If the conversation was too short or trivial to warrant a debrief, return: []
```

---

## Phase 1: Core Debrief Module (TypeScript — MCP Server)

### Task 1: Debrief prompt + parser module

**Files:**
- Create: `mcp/src/tools/debrief.ts`

**Step 1: Create the debrief module**

This module contains:
- The canonical debrief system prompt (exported as `DEBRIEF_SYSTEM_PROMPT`)
- `generateDebrief(conversation, alreadyStoredFacts, config)` — calls LLM, parses response
- `parseDebriefResponse(response)` — validates JSON, filters, caps at 5 items
- Uses the same `chatCompletion` from `../llm-client.ts` (or `skill/plugin/llm-client.ts` pattern)

```typescript
// mcp/src/tools/debrief.ts
import { resolveLLMConfig, chatCompletion } from '../../skill/plugin/llm-client.js';
// Note: MCP server doesn't have its own LLM client — it relies on the host agent.
// For MCP, the debrief tool receives conversation_summary as a parameter
// and the HOST AGENT (Claude, Cursor) runs the extraction using its own model.
```

**IMPORTANT DESIGN DECISION for MCP:** The MCP server can't make its own LLM calls (it doesn't know the host agent's model/API key). Instead, the `totalreclaw_debrief` tool:
1. Receives `conversation_summary` as a text parameter (the host agent summarizes)
2. OR receives `facts` as a JSON array (the host agent extracts)
3. The tool stores whatever the agent passes — the agent IS the LLM

This is different from OpenClaw/Hermes/NanoClaw where the plugin makes its own LLM call. For MCP, we trust the host agent to do the extraction — we just provide the prompt guidance in `SERVER_INSTRUCTIONS`.

**Step 2: Write tests**

```typescript
// mcp/tests/debrief.test.ts
- parseDebriefResponse with valid JSON
- parseDebriefResponse with empty array
- parseDebriefResponse with markdown fences
- parseDebriefResponse caps at 5 items
- parseDebriefResponse filters importance < 6
- parseDebriefResponse validates type (summary|context only)
```

**Step 3: Run tests, commit**

---

### Task 2: Register `totalreclaw_debrief` MCP tool

**Files:**
- Modify: `mcp/src/index.ts` — add tool definition + handler
- Modify: `mcp/src/prompts.ts` — add debrief guidance to SERVER_INSTRUCTIONS

**Tool definition:**

```typescript
const debriefToolDefinition = {
  name: 'totalreclaw_debrief',
  description:
    'Store a session debrief — broader context, outcomes, and conclusions that ' +
    'individual memory storage may have missed. Call this at the END of substantive ' +
    'conversations (not casual chat). Pass the key takeaways as facts.',
  inputSchema: {
    type: 'object',
    properties: {
      facts: {
        type: 'array',
        description: 'Array of debrief items to store',
        items: {
          type: 'object',
          properties: {
            text: { type: 'string', description: 'The debrief summary text (1-3 sentences)' },
            type: { type: 'string', enum: ['summary', 'context'], description: 'summary=conclusion/outcome, context=broader project context' },
            importance: { type: 'number', description: 'Importance 1-10 (typically 7-8 for debriefs)' },
          },
          required: ['text'],
        },
      },
    },
    required: ['facts'],
  },
};
```

**Handler:** Iterates over `facts`, encrypts each, stores on-chain with `source: "mcp_debrief"`.

**Prompt addition to SERVER_INSTRUCTIONS:**

```
### End of Conversation
When a substantive conversation is ending (the user says goodbye, the topic is resolved,
or the conversation naturally concludes), call totalreclaw_debrief with the key takeaways.

Focus on what individual memory storage missed:
- What was the conversation about overall?
- What was decided or resolved?
- What approaches were tried and what was the outcome?
- What's left unfinished?

Do NOT debrief casual conversations (greetings, simple Q&A, small talk).
Max 5 items, each 1-3 sentences, type "summary" or "context", importance 7-8.
```

---

### Task 3: Debrief in OpenClaw plugin hooks

**Files:**
- Modify: `skill/plugin/extractor.ts` — add `extractDebrief()` function
- Modify: `skill/plugin/index.ts` — wire debrief into `pre_compaction` and `before_reset` hooks

**How it works:**
1. In `pre_compaction`/`before_reset` hook, AFTER regular extraction completes:
2. Fetch the facts that were just stored in this session (from the extraction results)
3. Call `extractDebrief(allMessages, storedFacts)` — uses the canonical debrief prompt
4. Store the debrief results with `source: "openclaw_debrief"`

The debrief prompt receives the already-stored facts so it doesn't duplicate them.

**Step 1: Write `extractDebrief` in extractor.ts**

Uses the same `chatCompletion` + `resolveLLMConfig` pattern as `extractFacts`. Different system prompt (DEBRIEF_SYSTEM_PROMPT), different user prompt, different parser (max 5 items, type restricted to summary|context).

**Step 2: Wire into hooks**

In `index.ts`, in the `pre_compaction` hook handler, after `extractFacts()` completes:

```typescript
// After regular extraction...
const debriefFacts = await extractDebrief(messages, storedFactTexts);
for (const fact of debriefFacts) {
  // Store debrief items on-chain (same pipeline as regular facts)
}
```

**Step 3: Tests + commit**

---

## Phase 2: Python Client + Hermes Plugin

### Task 4: Debrief module in Python client

**Files:**
- Create: `python/src/totalreclaw/hermes/debrief.py`
- Create: `python/tests/test_debrief.py`

Same structure as `extractor.py`: canonical debrief prompt, LLM call via `llm_client.py`, response parser capped at 5 items. Falls back to empty (no heuristic fallback — debriefs require an LLM).

### Task 5: Wire debrief into Hermes hooks

**Files:**
- Modify: `python/src/totalreclaw/hermes/hooks.py`

In `on_session_end`, after regular extraction completes:
1. Collect texts of facts just stored in this session
2. Call `generate_debrief(messages, stored_fact_texts)`
3. Store debrief results with `source: "hermes_debrief"`

Only trigger if the conversation had >= 4 turns (skip trivial sessions).

### Task 6: Tests + commit

---

## Phase 3: NanoClaw

### Task 7: Wire debrief into NanoClaw pre_compact hook

**Files:**
- Modify: `skill-nanoclaw/src/hooks/pre-compact.ts`

Same pattern as OpenClaw: after regular extraction, call debrief with stored facts context.

---

## Phase 4: Rust / ZeroClaw

### Task 8: Debrief in Rust crate

**Files:**
- Create: `rust/totalreclaw-memory/src/debrief.rs`
- Modify: `rust/totalreclaw-memory/src/lib.rs` — add module
- Modify: `rust/totalreclaw-memory/src/backend.rs` — add `debrief()` method

Add a `pub async fn debrief(&self, conversation: &[Message], stored_facts: &[&str]) -> Result<Vec<DebriefItem>>` method to `TotalReclawMemory`.

ZeroClaw calls this from its consolidation phase or session-end handler. The method:
1. Calls the configured LLM (Ollama, provider API, or ZeroClaw's own model)
2. Parses the response
3. Stores each debrief item via the existing `store` pipeline

### Task 9: Tests

Parity test: verify the same conversation produces equivalent debrief output across Python and TypeScript.

---

## Phase 5: Documentation + Consistency

### Task 10: Update client-consistency.md

Add the debrief section to `docs/specs/totalreclaw/client-consistency.md`:
- Canonical debrief prompt
- Max 5 items rule
- Types: summary, context
- Importance: 7-8
- Minimum conversation length: 4 turns
- Source tag: `{client}_debrief`

### Task 11: Update CLAUDE.md feature matrix

Add `Session debrief` row to the Platform Support table.

### Task 12: Update website

If relevant, mention session debrief as a feature on the pricing page or feature list.

---

## Execution Order & Dependencies

```
Phase 1 (TypeScript/MCP):  Tasks 1-3  — sequential
Phase 2 (Python/Hermes):   Tasks 4-6  — parallel with Phase 1
Phase 3 (NanoClaw):        Task 7     — after Phase 1 (shares TS code)
Phase 4 (Rust/ZeroClaw):   Tasks 8-9  — parallel with Phases 1-3
Phase 5 (Docs):            Tasks 10-12 — after all phases
```

**Parallelizable:** Phases 1, 2, and 4 can run simultaneously (different languages, different files).

---

## Key Design Decisions

### 1. MCP: Tool-based, not hook-based

The MCP server has no lifecycle hooks. The host agent (Claude, Cursor) calls `totalreclaw_debrief` when the conversation ends. The prompt in `SERVER_INSTRUCTIONS` guides this behavior. This is consistent with how MCP already handles proactive memory — via prompt guidance.

### 2. Debrief requires LLM — no heuristic fallback

Unlike fact extraction (which has a regex fallback), debrief is inherently an LLM task — synthesizing broader context from a conversation. If no LLM is available, the debrief simply doesn't run. This is acceptable: the user still gets turn-by-turn extraction.

### 3. Dedup via "already stored" context

The debrief prompt receives a list of facts already stored in the session. This prevents the "summary duplicates facts" problem. The LLM is explicitly told: "Do NOT repeat facts already stored."

### 4. Maximum 5 items

Debriefs should be concise. 5 items is enough to capture the essence of any conversation without bloating the vault. Each item is 1-3 sentences.

### 5. Minimum conversation length: 4 turns

Trivial conversations (1-2 turns, simple Q&A) don't warrant a debrief. The threshold is 4 turns (8 messages), checked before calling the LLM.

### 6. Source tagging

Debrief items use `source: "{client}_debrief"` (e.g., `hermes_debrief`, `mcp_debrief`, `openclaw_debrief`). This allows filtering/identifying debrief items separately from extracted facts.

---

## Definition of Done

- [ ] `totalreclaw_debrief` tool registered in MCP server (11 → 12 tools)
- [ ] SERVER_INSTRUCTIONS updated with end-of-conversation guidance
- [ ] OpenClaw plugin runs debrief in pre_compaction/before_reset hooks
- [ ] NanoClaw runs debrief in pre_compact hook
- [ ] Hermes plugin runs debrief in on_session_end hook
- [ ] Rust crate exposes `debrief()` method for ZeroClaw
- [ ] Canonical debrief prompt identical across all implementations
- [ ] Response parser: max 5 items, type=summary|context, importance filter
- [ ] Dedup: already-stored facts passed as context to debrief prompt
- [ ] Minimum 4-turn threshold before triggering debrief
- [ ] `client-consistency.md` updated with debrief spec
- [ ] CLAUDE.md feature matrix updated
- [ ] Cross-client test: verify debrief items stored by one client are readable by another

---

## Appendix: Reference Files

| Phase | Files to Read Before Starting |
|-------|-------------------------------|
| Phase 1 (MCP) | `mcp/src/prompts.ts`, `mcp/src/index.ts:1371-1700`, `mcp/src/tools/remember.ts`, `skill/plugin/extractor.ts`, `skill/plugin/llm-client.ts` |
| Phase 2 (Python) | `python/src/totalreclaw/hermes/extractor.py`, `python/src/totalreclaw/hermes/hooks.py`, `python/src/totalreclaw/hermes/llm_client.py` |
| Phase 3 (NanoClaw) | `skill-nanoclaw/src/hooks/pre-compact.ts`, `skill-nanoclaw/src/extraction/prompts.ts` |
| Phase 4 (Rust) | `rust/totalreclaw-memory/src/backend.rs`, `rust/totalreclaw-memory/src/store.rs`, `rust/totalreclaw-memory/src/embedding.rs` |
| Phase 5 (Docs) | `docs/specs/totalreclaw/client-consistency.md`, `CLAUDE.md` |
