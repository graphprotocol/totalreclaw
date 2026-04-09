<!--
Product: TotalReclaw (TotalReclaw)
Version: 1.0
Last updated: 2026-03-01
-->

# Technical Specification: MCP Auto-Memory for Generic Hosts

> **Automatic memory recall and storage for Claude Desktop, Cursor, Windsurf, VS Code, and other MCP-compatible hosts.**

**Version:** 1.0.0
**Date:** 2026-03-01
**Status:** Design Complete, Ready for Implementation

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [MCP Protocol Analysis](#2-mcp-protocol-analysis)
3. [Approach Comparison](#3-approach-comparison)
4. [Recommended Architecture](#4-recommended-architecture)
5. [Implementation Plan](#5-implementation-plan)
6. [Limitations](#6-limitations)
7. [Host Compatibility Matrix](#7-host-compatibility-matrix)

---

## 1. Problem Statement

### The Gap

TotalReclaw has two fully-automatic integrations:

1. **OpenClaw plugin** (`skill/plugin/`) -- Uses ACP hooks (`before_agent_start`, `agent_end`, `before_compaction`) to automatically recall relevant memories before each conversation turn and extract new facts after each turn. Zero user effort.

2. **NanoClaw MCP** (`skill-nanoclaw/`) -- Uses Claude Agent SDK hooks (`PreCompact`, `BeforeAgentStart`, `AgentEnd`) for the same automatic behavior. Zero user effort.

The **generic MCP server** (`mcp/`) exposes tools (`totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, `totalreclaw_export`, `totalreclaw_import`) and a prompts capability, but has **no automatic behavior**. Users in Claude Desktop, Cursor, Windsurf, and VS Code must:

- Manually ask the LLM to recall memories at the start of each conversation
- Manually ask the LLM to store facts after sharing information
- Remember to use the tools at all

This defeats the core value proposition: memory should be invisible and automatic.

### Why This Matters

The target users are non-technical. They expect memory to "just work" like it does in ChatGPT or Claude's built-in memory. Requiring manual tool invocation is a UX failure.

### What "Automatic" Means

| Behavior | OpenClaw/NanoClaw (current) | Generic MCP (goal) |
|----------|---------------------------|---------------------|
| **Auto-recall**: Fetch relevant memories at conversation start | Hook fires automatically | Need a mechanism |
| **Auto-store**: Extract & store facts after each turn | Hook fires automatically | Need a mechanism |
| **Auto-store on compaction**: Save facts before context window is truncated | Hook fires automatically | Need a mechanism |
| **No extra prompting**: User doesn't need to ask for memory | Transparent | Must be transparent |

---

## 2. MCP Protocol Analysis

### 2.1 Available MCP Primitives

The MCP specification (2025-06-18 / 2025-11-25) provides these server-to-client capabilities relevant to auto-memory:

#### Tools (model-controlled)

- **How it works**: Server declares tools via `tools/list`. The LLM discovers them and decides when to invoke them based on tool descriptions and context.
- **Auto-memory potential**: Tool descriptions can instruct the LLM on *when* to call tools (e.g., "ALWAYS call this at conversation start"). The LLM is model-controlled -- it chooses when to invoke tools based on its understanding.
- **Annotations**: `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` provide metadata about tool behavior. `readOnlyHint: true` on recall tools signals no side effects.
- **Support**: Universal -- every MCP client supports tools.

#### Resources (application-driven)

- **How it works**: Server exposes data via `resources/list` and `resources/read`. Supports subscriptions for change notifications.
- **Auto-memory potential**: Could expose a "memory context" resource that contains recent/relevant memories. However, resource inclusion is **application-driven** -- the host decides whether to include resources in context, not the model.
- **Critical limitation**: Claude Desktop does NOT auto-include resources in context. Users must explicitly select resources via the `@` mention UI. This was confirmed in a GitHub issue (modelcontextprotocol/typescript-sdk#686) -- resources are "there for the user to feed to the AI at their discretion." This severely limits auto-memory via resources.
- **Support**: Claude Desktop (manual selection only), Cursor (model-requested on-demand), Windsurf (supported), VS Code (manual selection).

#### Prompts (user-controlled)

- **How it works**: Server exposes prompt templates via `prompts/list`. Clients present them as slash commands (e.g., `/totalreclaw_recall`).
- **Auto-memory potential**: Limited. Prompts are **user-controlled** -- the spec explicitly states they are "exposed from servers to clients with the intention of the user being able to explicitly select them for use." They appear as slash commands, not auto-injected context.
- **Support**: Claude Desktop (slash commands), Cursor (slash commands via `/`), Windsurf (supported), VS Code (slash commands).

#### Server Instructions (initialize response)

- **How it works**: The server returns an `instructions` string in its `initialize` response. Clients SHOULD incorporate this into the system prompt or decision-making context.
- **Auto-memory potential**: HIGH. This is the closest thing to automatic system prompt injection in the MCP spec. The instructions are sent once at connection startup and guide the LLM's behavior for the entire session.
- **Key insight**: This field is designed to make servers "self-describing" -- guiding the LLM on when and how to use the server's tools without manual configuration.
- **Support**: Claude Desktop (supported), Claude Code (supported, configurable), Cursor (supported -- dynamic context), VS Code/Copilot (supported), Gemini CLI (recently added via PR #13432). Not all clients may use it effectively.

#### Sampling (server-initiated LLM calls)

- **How it works**: Server sends `sampling/createMessage` to request the client make an LLM call. The server provides the prompt, the client routes it to an LLM and returns the result.
- **Auto-memory potential**: Could enable server-side fact extraction -- the server asks the client's LLM to extract facts from conversation context. However, this requires the server to have access to the conversation, which it does not in the standard MCP flow.
- **Critical limitation**: (a) The server does not receive conversation messages automatically -- it only gets tool call arguments. (b) Sampling requires user approval (human-in-the-loop). (c) Support is inconsistent.
- **Support**: VS Code (full support), Claude Desktop (limited), Cursor (not fully supported), Windsurf (unknown).

#### Elicitation (server-initiated user input)

- **How it works**: Server requests structured input from the user via forms.
- **Auto-memory potential**: Minimal. Could be used for first-time setup (asking for recovery phrase), but not for auto-recall/store.
- **Support**: Cursor (supported), VS Code (supported), others (limited).

#### Notifications

- **How it works**: Server can send `notifications/resources/list_changed`, `notifications/tools/list_changed`, `notifications/prompts/list_changed`.
- **Auto-memory potential**: Could signal the client to refresh a "memory context" resource after new facts are stored. Limited utility without auto-resource-inclusion.

### 2.2 Key Finding: No Hook System in Generic MCP

The fundamental challenge: **MCP has no lifecycle hooks**. There is no `conversation_start`, `conversation_end`, `turn_start`, `turn_end`, `before_compaction`, or similar events. The server cannot know when:

- A new conversation begins
- A user message arrives
- An assistant response is complete
- Context is about to be compacted

This is by design -- MCP is a stateless tool protocol, not an agent framework. The server only receives data when a tool is explicitly called.

### 2.3 What Other Memory Servers Do

| Server | Approach | Auto-Recall? | Auto-Store? |
|--------|----------|-------------|-------------|
| **Official MCP Knowledge Graph** | Tools + system prompt instructions ("Always begin by saying 'Remembering...'") | Via system prompt instruction | Via system prompt instruction |
| **Mem0 MCP** | Tools only | No -- manual tool calls | No -- manual tool calls |
| **Recall MCP** | Claude Code hooks (session-start, observe, pre-compact, session-end) | Yes -- via hooks | Yes -- via hooks |
| **mcp-memory-service (doobidoo)** | Tools + MCP prompt handlers | Partial -- via prompt handlers | Partial -- via prompt handlers |
| **claude-memory-mcp** | Tools only (SQLite + FTS5) | No -- manual | No -- manual |
| **Basic Memory** | MCP tools + resources (Markdown files) | Partial -- via model context | Via auto-save hooks |

**Pattern recognition**: The most successful auto-memory implementations either (a) use platform-specific hooks (Recall with Claude Code hooks, OpenClaw with ACP hooks) or (b) use aggressive tool description instructions that guide the LLM to call tools proactively.

No existing server has solved the generic MCP auto-memory problem cleanly. This is an unsolved problem in the ecosystem.

---

## 3. Approach Comparison

### Approach A: Tool-Description-Driven ("Behavioral Instructions")

**Mechanism**: Use rich tool descriptions and server `instructions` to instruct the LLM to proactively call memory tools.

**Implementation**:
1. Server returns `instructions` in `initialize` response containing behavioral directives
2. Tool descriptions include "WHEN TO USE" blocks with imperative language
3. The recall tool description instructs the LLM to call it at conversation start
4. The remember tool description instructs the LLM to call it when facts are shared

**Example `instructions` field**:
```
You have access to TotalReclaw, a persistent encrypted memory vault.

CRITICAL BEHAVIORS:
1. At the START of every conversation, call totalreclaw_recall with a query summarizing the user's first message to load relevant context.
2. When the user shares preferences, personal information, corrections, or important facts, call totalreclaw_remember to store them.
3. When recalling, present memories naturally as context -- do not announce "I found these memories."
4. When storing, confirm briefly (e.g., "I'll remember that.") unless the user explicitly asked you to remember.
```

**Pros**:
- Works with ALL MCP clients (100% compatibility)
- No special protocol features needed
- Simple to implement
- The `instructions` field is specifically designed for this use case
- Proven pattern (Official MCP Knowledge Graph server uses this)

**Cons**:
- LLM compliance is not guaranteed -- the model may forget or ignore instructions
- Quality depends on the host's LLM model (smaller models follow instructions less reliably)
- No guarantee of execution -- the LLM decides whether to call tools
- Auto-store is particularly unreliable -- the LLM must decide what's worth storing
- Increases token usage (instructions consume context window)

**Reliability estimate**: ~70-85% for auto-recall, ~40-60% for auto-store (varies by model)

### Approach B: Resource-Based Context Injection

**Mechanism**: Expose a dynamic "memory context" resource that the client can include in the conversation.

**Implementation**:
1. Server exposes a resource `memory://context` that returns a pre-computed memory summary
2. Resource supports subscriptions -- updates when new facts are stored
3. Client auto-includes the resource in context (if supported)

**Example resource**:
```json
{
  "uri": "memory://context/recent",
  "name": "Recent Memory Context",
  "description": "Your most recent and important memories",
  "mimeType": "text/markdown",
  "annotations": {
    "audience": ["assistant"],
    "priority": 1.0
  }
}
```

**Pros**:
- Clean separation of concerns
- Resource subscriptions enable real-time updates
- Annotations can signal high priority to the client
- Elegant architecture

**Cons**:
- **FATAL**: Claude Desktop does NOT auto-include resources. Users must manually select them via `@` UI.
- Cursor requests resources on-demand (model-driven), not automatically
- No way for the resource to know what query to use for relevance ranking (resource is served without conversation context)
- "Recent memories" is a crude proxy for "relevant memories" -- no query-based retrieval
- Support is inconsistent across clients

**Reliability estimate**: 0% for Claude Desktop (requires manual action), ~30% for Cursor (model might request it)

### Approach C: Prompt-Based Workflow

**Mechanism**: Expose a prompt template that includes memory retrieval as part of its workflow.

**Implementation**:
1. Server exposes a prompt `totalreclaw_start` that the user invokes via slash command
2. The prompt template includes a recall step and formats the response with memory context
3. Users type `/totalreclaw_start` at the beginning of each conversation

**Pros**:
- Structured workflow
- Consistent behavior when invoked
- Good for power users

**Cons**:
- **Requires user action** -- user must remember to type the slash command
- Not automatic by any definition
- Defeats the UX goal
- Prompts are user-controlled by spec design

**Reliability estimate**: 100% when invoked, but requires user discipline (which defeats the purpose)

### Approach D: Sampling-Based Fact Extraction

**Mechanism**: Server uses MCP sampling to request LLM calls for fact extraction.

**Implementation**:
1. When `totalreclaw_remember` is called, server uses sampling to ask the LLM to extract atomic facts
2. Server-side fact extraction quality matches OpenClaw's extractor

**Pros**:
- Offloads extraction intelligence to the LLM
- Could enable sophisticated fact extraction without bundling an LLM client

**Cons**:
- Does NOT solve auto-recall or auto-store triggers -- the tool must still be called first
- Sampling requires user approval (human-in-the-loop per the spec)
- Inconsistent client support (VS Code yes, Cursor partial, Claude Desktop limited)
- Adds latency and cost per extraction
- The MCP server still does not receive conversation context -- only tool call arguments

**Reliability estimate**: N/A -- does not solve the core problem

### Approach E: Hybrid (Recommended)

**Mechanism**: Combine server `instructions` + aggressive tool descriptions (Approach A) with a resource-based memory summary (Approach B) and a prompt-based fallback (Approach C).

This is a **layered** approach where each layer increases the probability of auto-memory behavior:

```
Layer 1: Server instructions         → LLM is told to recall/store proactively
Layer 2: Tool descriptions            → Rich "WHEN TO USE" guidance
Layer 3: Composite recall tool        → Single tool combines recall + context formatting
Layer 4: Memory resource              → Pre-computed context for clients that support auto-inclusion
Layer 5: Prompt fallback              → Slash command for manual invocation when auto fails
Layer 6: Sampling for extraction      → Server-side fact extraction (where supported)
```

**Why hybrid wins**: No single mechanism guarantees auto-memory across all hosts. By layering multiple signals, we maximize the probability that the LLM will exhibit the desired behavior.

---

## 4. Recommended Architecture

### 4.1 Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP CLIENT (Host Application)                │
│  Claude Desktop / Cursor / Windsurf / VS Code / Others          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. initialize() ──→ Server returns instructions                │
│     └─ Client injects instructions into system prompt           │
│                                                                  │
│  2. LLM reads instructions ──→ Decides to call recall           │
│     └─ totalreclaw_recall({query: "..."}) ──→ Server             │
│     └─ Server returns relevant memories                         │
│                                                                  │
│  3. LLM reads conversation ──→ Decides to call remember         │
│     └─ totalreclaw_remember({facts: [...]}) ──→ Server           │
│     └─ Server stores encrypted facts                            │
│                                                                  │
│  4. (Optional) Client reads memory://context resource           │
│     └─ Server returns pre-computed memory summary               │
│                                                                  │
│  5. (Fallback) User invokes /totalreclaw_start prompt            │
│     └─ Prompt includes recall + instructions                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     MCP SERVER (Local Process)                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Capabilities:                                                   │
│    ├─ tools: { listChanged: true }                              │
│    ├─ resources: { subscribe: true, listChanged: true }         │
│    └─ prompts: { listChanged: true }                            │
│                                                                  │
│  instructions: "You have TotalReclaw. ALWAYS recall at start..." │
│                                                                  │
│  Tools:                                                          │
│    ├─ totalreclaw_recall    (read-only, high priority)           │
│    ├─ totalreclaw_remember  (batch fact storage)                 │
│    ├─ totalreclaw_forget    (delete)                             │
│    ├─ totalreclaw_export    (backup)                             │
│    └─ totalreclaw_import    (restore)                            │
│                                                                  │
│  Resources:                                                      │
│    └─ memory://context/summary  (pre-computed memory summary)   │
│                                                                  │
│  Prompts:                                                        │
│    ├─ totalreclaw_start     (recall + context setup)             │
│    └─ totalreclaw_save      (batch store from conversation)      │
│                                                                  │
│  Internal:                                                       │
│    ├─ E2EE crypto (XChaCha20-Poly1305, HKDF, blind indices)    │
│    ├─ LSH + embeddings (local Harrier-OSS-v1-270M)             │
│    ├─ BM25 + cosine + RRF reranker                             │
│    └─ TotalReclaw server client (HTTP)                           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   TotalReclaw Server (Remote/Local)               │
│                   FastAPI + PostgreSQL                            │
│                   (sees only encrypted blobs)                    │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Layer 1: Server Instructions

The `instructions` field in the `initialize` response is the most impactful single change. It provides persistent behavioral guidance to the LLM.

```typescript
const SERVER_INSTRUCTIONS = `
You have access to TotalReclaw, an end-to-end encrypted memory vault that persists across conversations.

## CRITICAL: Automatic Memory Behaviors

### At Conversation Start
ALWAYS call totalreclaw_recall at the beginning of EVERY new conversation with a brief summary of the user's first message as the query. This loads relevant context from past conversations. Present recalled memories naturally as context -- do not announce "I found these memories" or list them explicitly unless the user asks.

### During Conversation
When the user shares ANY of the following, call totalreclaw_remember to store it:
- Personal preferences ("I prefer...", "I like...", "I hate...")
- Personal information (name, location, occupation, family details)
- Decisions or goals ("I decided to...", "I want to...")
- Corrections to previous information ("Actually, I'm vegan now")
- Important events or dates ("My birthday is...", "I started a new job")
- Technical preferences (programming language, tools, frameworks)

Do NOT store:
- Temporary/transient context (weather, current task details)
- Information about third parties the user hasn't asked to remember
- Sensitive credentials (passwords, API keys, tokens)

### Memory Hygiene
- Before storing a new fact, check recalled memories to avoid duplicates
- When the user corrects information, store the correction (the system handles deduplication)
- Use importance 7-9 for core identity/preferences, 4-6 for moderate facts, 1-3 for minor details
`;
```

### 4.3 Layer 2: Enhanced Tool Descriptions

Tool descriptions are refined to reinforce the behavioral instructions and provide clear decision criteria.

**totalreclaw_recall** (enhanced):
```typescript
const RECALL_DESCRIPTION = `Search your encrypted memory vault for relevant past context.

IMPORTANT: You SHOULD call this tool at the START of every conversation with a query based on the user's first message. This ensures continuity across sessions.

Use this tool when:
- Starting a new conversation (query = summary of user's first message)
- User asks about their preferences or past information
- User references something from a previous conversation
- You need context about the user's background

Parameters:
- query: Natural language search (required). Keep it concise -- 5-15 words work best.
- k: Number of results (default 8, max 50). Use 3-5 for quick lookups, 8-12 for broad context.

The results are end-to-end encrypted. The server never sees plaintext.`;
```

**totalreclaw_remember** (enhanced with batch support):
```typescript
const REMEMBER_DESCRIPTION = `Store one or more facts in the encrypted memory vault.

Call this tool whenever the user shares personal information, preferences, decisions, or important facts worth remembering across conversations.

IMPORTANT: Extract atomic facts, not entire conversation snippets.
Good: "User is vegan"
Bad: "User said they recently became vegan and prefer organic food from local farms"

The facts parameter accepts an array, so you can store multiple facts in a single call.

Each fact needs:
- text: The atomic fact (required)
- importance: 1-10 scale (optional, default 5)
  - 9-10: Core identity (name, fundamental values)
  - 7-8: Important preferences (diet, work style)
  - 5-6: Moderate facts (schedule, minor preferences)
  - 3-4: Low priority (casual mentions)
  - 1-2: Ephemeral (likely to change)
- type: Category (optional) -- "fact", "preference", "decision", "episodic", "goal"

The vault handles deduplication automatically. If a similar fact exists, it will be updated rather than duplicated.`;
```

### 4.4 Layer 3: Batch Remember Tool

A key enhancement: change `totalreclaw_remember` to accept an array of facts instead of a single fact. This reduces tool call overhead and makes it natural for the LLM to extract multiple facts at once.

```typescript
interface RememberInput {
  facts: Array<{
    text: string;
    importance?: number;  // 1-10, default 5
    type?: 'fact' | 'preference' | 'decision' | 'episodic' | 'goal';
  }>;
  namespace?: string;
}
```

The current single-fact API remains supported for backward compatibility (if `fact` string is provided instead of `facts` array, wrap it).

### 4.5 Layer 4: Memory Context Resource

Expose a pre-computed memory summary as an MCP resource. This serves two purposes:
1. In clients that auto-include resources (future possibility), it provides passive context
2. In all clients, it is available for manual `@` inclusion

```typescript
// Resource: memory://context/summary
{
  uri: "memory://context/summary",
  name: "Memory Summary",
  title: "Your TotalReclaw Context",
  description: "A summary of your most important and recent memories. Include this for personalized responses.",
  mimeType: "text/markdown",
  annotations: {
    audience: ["assistant"],
    priority: 0.9
  }
}
```

The resource content is generated by:
1. Fetching the top ~20 facts by importance + recency
2. Decrypting and formatting them as a Markdown list
3. Caching the result for 5 minutes (refreshed on `totalreclaw_remember` calls)

When the resource is read:
```markdown
## Your Memory Context

### High Priority
- User's name is Pedro (importance: 9/10)
- User is a software engineer working on AI tools (importance: 8/10)
- User prefers TypeScript over JavaScript (importance: 7/10)

### Recent
- User started working on TotalReclaw project (2 days ago)
- User prefers dark mode in all applications (5 days ago)

*20 total memories stored. Use totalreclaw_recall for specific searches.*
```

### 4.6 Layer 5: Prompt Fallback

Two prompts for manual invocation when auto-behavior fails:

**`totalreclaw_start`** -- "Start with memory context"
```typescript
{
  name: "totalreclaw_start",
  title: "Start with Memory",
  description: "Load your memory context for this conversation. Use this if memories weren't loaded automatically.",
  arguments: [
    {
      name: "topic",
      description: "Optional topic to focus memory recall on",
      required: false
    }
  ]
}
```

When invoked, returns messages that include a recall tool call result:
```json
{
  "messages": [
    {
      "role": "user",
      "content": {
        "type": "text",
        "text": "Please recall my relevant memories about {topic || 'recent context'} and use them to personalize our conversation."
      }
    }
  ]
}
```

**`totalreclaw_save`** -- "Save conversation facts"
```typescript
{
  name: "totalreclaw_save",
  title: "Save to Memory",
  description: "Extract and save important facts from this conversation to your memory vault.",
  arguments: []
}
```

When invoked, returns a prompt that instructs the LLM to review the conversation and call `totalreclaw_remember` with extracted facts.

### 4.7 Layer 6: Sampling for Extraction (Optional, Future)

Where sampling is supported (VS Code, future clients), the server can use it for higher-quality fact extraction:

1. When `totalreclaw_remember` is called with a `conversation_context` parameter
2. The server uses `sampling/createMessage` to ask the client's LLM to extract atomic facts
3. The extracted facts are then stored through the normal E2EE pipeline

This is optional and not required for the initial implementation. The primary extraction path is the LLM in the host application doing extraction before calling `totalreclaw_remember`.

### 4.8 E2EE Constraints

All approaches maintain end-to-end encryption:

- The MCP server runs **locally** on the user's machine
- Encryption/decryption happens in the MCP server process
- The TotalReclaw backend server only receives encrypted blobs and blind indices
- The `instructions` field contains no user data -- only behavioral guidance
- Resources contain decrypted data, but this is served locally (stdio transport) between the MCP server and the host application on the same machine
- No API keys are needed -- local embeddings (Harrier-OSS-v1-270M) are used

---

## 5. Implementation Plan

### Phase 1: Core Auto-Memory (Highest Impact)

**Files to modify**:

| File | Changes |
|------|---------|
| `mcp/src/index.ts` | Add `instructions` to server initialize, add resources capability |
| `mcp/src/prompts.ts` | Rewrite `SYSTEM_PROMPT_FRAGMENT` as `SERVER_INSTRUCTIONS`, update all tool descriptions |
| `mcp/src/tools/remember.ts` | Add batch support (accept `facts` array) |
| `mcp/src/tools/recall.ts` | Add `readOnlyHint` annotation, enhance description |
| `mcp/src/resources/` | New directory -- memory context resource handler |

**Step-by-step**:

1. **Add `instructions` to server initialization** (`mcp/src/index.ts`)
   - Add `instructions` field to server constructor options or initialize response
   - The MCP SDK should support passing `instructions` in the server info

2. **Rewrite tool descriptions** (`mcp/src/prompts.ts`)
   - Replace current descriptions with enhanced versions from Section 4.3
   - Focus on imperative language for recall ("ALWAYS call at conversation start")
   - Add clear decision criteria for remember ("When user shares preferences...")

3. **Add batch remember support** (`mcp/src/tools/remember.ts`)
   - Accept `facts` array in addition to single `fact` string
   - Process each fact through the existing E2EE pipeline
   - Return batch results (created/updated/skipped per fact)

4. **Add tool annotations** (all tool files)
   - `totalreclaw_recall`: `readOnlyHint: true, idempotentHint: true`
   - `totalreclaw_remember`: `readOnlyHint: false, idempotentHint: true` (dedup)
   - `totalreclaw_forget`: `readOnlyHint: false, destructiveHint: true`

5. **Add resources capability** (`mcp/src/index.ts`, `mcp/src/resources/`)
   - Declare `resources` capability with `subscribe: true, listChanged: true`
   - Implement `resources/list` returning the memory context resource
   - Implement `resources/read` for `memory://context/summary`
   - Cache the resource content, invalidate on remember/forget

### Phase 2: Prompt Fallbacks

**Files to create/modify**:

| File | Changes |
|------|---------|
| `mcp/src/prompts.ts` | Add `totalreclaw_start` and `totalreclaw_save` prompts |
| `mcp/src/index.ts` | Register new prompts in `ListPromptsRequestSchema` handler |

**Steps**:

6. **Add `totalreclaw_start` prompt** -- returns a user message instructing recall
7. **Add `totalreclaw_save` prompt** -- returns a user message instructing fact extraction and storage

### Phase 3: Testing & Validation

8. **Manual testing** with Claude Desktop
   - Verify `instructions` are received and influence behavior
   - Test auto-recall: does Claude call `totalreclaw_recall` on first message?
   - Test auto-store: does Claude call `totalreclaw_remember` when facts are shared?
   - Test resource: can user `@` the memory context resource?
   - Test prompts: do slash commands work?

9. **Manual testing** with Cursor
   - Same test matrix as Claude Desktop
   - Verify resource subscription works

10. **Manual testing** with VS Code (if available)
    - Same test matrix
    - Test sampling capability for fact extraction

11. **Reliability measurement**
    - Run 20 conversations with various first messages
    - Measure: % of times recall is called automatically
    - Measure: % of facts correctly stored after explicit sharing
    - Target: >80% auto-recall, >60% auto-store

### Phase 4: Advanced Features (Future)

12. **Sampling-based extraction** -- use `sampling/createMessage` for fact extraction in supported clients
13. **Conversation context tracking** -- track tool calls within a session to detect conversation flow and trigger storage
14. **Auto-compaction detection** -- detect when the host is about to lose context (heuristic: long gap between tool calls) and trigger a save
15. **Multi-namespace support** -- auto-detect namespace from conversation context

### File Structure After Implementation

```
mcp/
├── src/
│   ├── index.ts              # Server setup, instructions, capability negotiation
│   ├── prompts.ts            # Tool descriptions, server instructions, prompt templates
│   ├── server.ts             # Server export
│   ├── tools/
│   │   ├── index.ts          # Tool exports
│   │   ├── remember.ts       # Enhanced with batch support
│   │   ├── recall.ts         # Enhanced description + annotations
│   │   ├── forget.ts         # + annotations
│   │   ├── export.ts         # Unchanged
│   │   └── import.ts         # Unchanged
│   └── resources/
│       ├── index.ts          # Resource handler exports
│       └── memory-context.ts # Memory summary resource
├── tests/
│   ├── auto-memory.test.ts   # New: test instructions, batch remember
│   └── resources.test.ts     # New: test memory context resource
└── package.json
```

---

## 6. Limitations

### 6.1 Fundamental Limitations (Cannot Be Solved)

| Limitation | Reason | Mitigation |
|-----------|--------|------------|
| **No guaranteed auto-recall** | LLM decides whether to call tools; no lifecycle hooks in MCP | Multi-layered instructions + resource fallback |
| **No guaranteed auto-store** | LLM must decide what's worth storing | Rich description guidance + prompt fallback |
| **No conversation access** | Server only receives tool call arguments, not full conversation | LLM must extract and pass relevant facts |
| **No compaction hook** | MCP has no pre-compaction event | Cannot detect context loss; user must manually save or LLM must be proactive |
| **Model quality dependency** | Smaller/weaker models follow instructions less reliably | Instructions written for lowest-common-denominator comprehension |

### 6.2 Comparison with Hook-Based Systems

| Feature | OpenClaw (hooks) | NanoClaw (hooks) | Generic MCP (instructions) |
|---------|-----------------|------------------|---------------------------|
| Auto-recall reliability | ~99% (hook fires) | ~99% (hook fires) | ~70-85% (LLM discretion) |
| Auto-store reliability | ~95% (hook fires) | ~95% (hook fires) | ~40-60% (LLM discretion) |
| Compaction protection | Yes (before_compaction hook) | Yes (PreCompact hook) | No (no equivalent) |
| Fact extraction quality | LLM-based (server-side) | LLM-based (server-side) | LLM-based (host-side) |
| Zero user effort | Yes | Yes | Mostly (may need occasional prompting) |

### 6.3 Per-Host Limitations

- **Claude Desktop**: Resources not auto-included. No sampling. Instructions field is the primary mechanism.
- **Cursor**: 100-tool limit across all MCP servers. Tool search may defer TotalReclaw tools. Dynamic context reduces token overhead but may truncate instructions.
- **Windsurf**: 100-tool limit. No confirmed sampling or elicitation support.
- **VS Code/Copilot**: Most complete MCP support, but agent mode behavior varies by Copilot version.

---

## 7. Host Compatibility Matrix

### Feature Support

| Feature | Claude Desktop | Cursor | Windsurf | VS Code/Copilot | Claude Code | Generic |
|---------|---------------|--------|----------|-----------------|-------------|---------|
| **Tools** | Full | Full | Full | Full | Full | Full |
| **Server instructions** | Yes | Yes (dynamic) | Unknown | Yes | Yes (configurable) | Varies |
| **Resources** | Manual (@) | On-demand | Yes | Manual | Yes | Varies |
| **Resource subscriptions** | Unknown | Yes | Unknown | Unknown | Yes | Varies |
| **Prompts** | Slash cmds | Slash cmds (/) | Yes | Slash cmds | Slash cmds | Varies |
| **Sampling** | Limited | No | Unknown | Full | N/A (has hooks) | Varies |
| **Elicitation** | Unknown | Yes | Unknown | Yes | N/A | Varies |
| **Tool annotations** | Yes | Yes | Unknown | Yes | Yes | Varies |

### Auto-Memory Effectiveness Prediction

| Host | Auto-Recall | Auto-Store | Best Mechanism |
|------|------------|-----------|----------------|
| **Claude Desktop** | Good (~80%) | Moderate (~50%) | Instructions + tool descriptions |
| **Cursor** | Good (~80%) | Moderate (~55%) | Instructions + tool descriptions |
| **Windsurf** | Good (~75%) | Moderate (~45%) | Tool descriptions (instructions support unknown) |
| **VS Code/Copilot** | Good (~85%) | Good (~60%) | Instructions + sampling for extraction |
| **Claude Code** | Excellent (~99%) | Excellent (~95%) | Native hooks (not MCP auto-memory) |
| **Generic MCP client** | Variable | Variable | Tool descriptions (broadest compatibility) |

### Recommended Configuration per Host

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_SERVER_URL": "http://localhost:8080",
        "TOTALRECLAW_RECOVERY_PHRASE": "your-recovery-phrase"
      }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_SERVER_URL": "http://localhost:8080",
        "TOTALRECLAW_RECOVERY_PHRASE": "your-recovery-phrase"
      }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`):
```json
{
  "servers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_SERVER_URL": "http://localhost:8080",
        "TOTALRECLAW_RECOVERY_PHRASE": "your-recovery-phrase"
      }
    }
  }
}
```

---

## Appendix A: Comparison with Claude Code Hooks

For Claude Code specifically, the MCP auto-memory approach described here is unnecessary. Claude Code has native hooks that provide 99%+ reliability:

```bash
# Claude Code hooks (settings.json or .claude/settings.json)
# SessionStart → inject memory context
# PreCompact → save facts before compaction
# PostToolUse → observe changes for memory capture
```

The Recall MCP project (recallmcp.com) demonstrates this approach. TotalReclaw should similarly provide a Claude Code hooks integration separate from the generic MCP server, using the same underlying client library.

This is out of scope for this specification but noted for completeness.

---

## Appendix B: Security Considerations

### Prompt Injection Risk

The `instructions` field could theoretically be exploited if a malicious MCP server injects harmful instructions. However:
1. TotalReclaw's MCP server runs locally and is user-installed
2. The instructions only affect memory behavior, not system-level operations
3. All data remains encrypted -- even if instructions are tampered with, the server cannot access plaintext

### Data Exfiltration via Tool Descriptions

Tool descriptions are visible to the LLM and could be read by other MCP servers in the same session. However:
1. Descriptions contain no user data -- only behavioral guidance
2. The actual memories are only accessible via authenticated tool calls

### Resource Content Privacy

The memory context resource contains decrypted memory summaries. This data is served over stdio (local pipe) and never transmitted over the network. The risk is equivalent to any other local file access.

---

## References

- [MCP Specification (2025-06-18)](https://modelcontextprotocol.io/specification/2025-06-18)
- [MCP Specification (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP Resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)
- [MCP Prompts](https://modelcontextprotocol.io/specification/2025-06-18/server/prompts)
- [MCP Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP Sampling](https://modelcontextprotocol.io/specification/2025-06-18/client/sampling)
- [MCP Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [Official MCP Knowledge Graph Memory Server](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
- [Recall MCP (Claude Code hooks)](https://recallmcp.com/)
- [mcp-memory-service (doobidoo)](https://github.com/doobidoo/mcp-memory-service)
- [Mem0 MCP](https://github.com/mem0ai/mem0-mcp)
- [VS Code Full MCP Spec Support](https://code.visualstudio.com/blogs/2025/06/12/full-mcp-spec-support)
- [Cursor MCP Features](https://cursor.com/docs/context/mcp)
- [Windsurf MCP Integration](https://docs.windsurf.com/windsurf/cascade/mcp)
- [Claude Desktop MCP Resources Issue](https://github.com/modelcontextprotocol/typescript-sdk/issues/686)
