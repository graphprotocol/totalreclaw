<!--
Product: TotalReclaw
Formerly: tech specs/nanoclaw/TS: TotalReclaw Skill for NanoClaw.md
Version: 1.0
Last updated: 2026-02-24
-->

# Technical Specification: TotalReclaw Skill for NanoClaw

> **End-to-end encrypted memory integration for NanoClaw agents**
> **Uses the generic TotalReclaw MCP Server**

**Version:** 0.3.0
**Date:** 2026-02-23
**Status:** Ready for Implementation
**Author:** TotalReclaw Team
**Depends On**: [TS: TotalReclaw MCP Server](../mcp/TS: TotalReclaw MCP Server.md)

---

## Table of Contents

1. [Overview](#overview)
2. [What NanoClaw Is](#what-nanoclaw-is)
3. [Why Integrate TotalReclaw](#why-integrate-totalreclaw)
4. [Library Reuse Analysis](#library-reuse-analysis)
5. [Integration Architecture](#integration-architecture)
6. [Agent Instruction Mechanism](#agent-instruction-mechanism)
7. [Implementation Details](#implementation-details)
8. [Key Management](#key-management)
9. [Configuration](#configuration)
10. [Testing Strategy](#testing-strategy)
11. [Migration Path](#migration-path)

---

## Overview

This specification describes how to integrate the **generic TotalReclaw MCP Server** into NanoClaw.

### Key Insight: Generic MCP + NanoClaw Skill

```
┌─────────────────────────────────────────────────────────────────┐
│                     GENERIC (reusable)                           │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  TotalReclaw MCP Server                                       │ │
│  │  - totalreclaw_remember tool                                  │ │
│  │  - totalreclaw_recall tool                                    │ │
│  │  - totalreclaw_forget tool                                    │ │
│  │  - totalreclaw_export tool                                    │ │
│  │  │  - totalreclaw_import tool                                    │ │
│  │  - System prompt fragment                                    │ │
│  │  - Namespace support                                         │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                           │                                      │
│                           │ MCP Protocol                         │
│                           ▼                                      │
├─────────────────────────────────────────────────────────────────┤
│                     NANOCLAW-SPECIFIC                            │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  /add-totalreclaw Skill Package                               │ │
│  │  - before-agent-start hook → calls totalreclaw_recall         │ │
│  │  - agent-end hook → calls totalreclaw_remember                │ │
│  │  - pre-compact hook → full extraction                        │ │
│  │  - namespace = groupFolder ("main", "work", "family")        │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Why This Split?

| Aspect | Generic MCP Server | NanoClaw Skill |
|--------|--------------------|-----------------|
| **Reusability** | Works with Claude Desktop, etc. | NanoClaw-specific |
| **Namespace** | Generic concept | Mapped to `groupFolder` |
| **Hooks** | N/A | NanoClaw lifecycle integration |
| **Tool Instructions** | Generic descriptions | Enhanced for NanoClaw context |

---

## What NanoClaw Is

### Core Characteristics

NanoClaw is a lightweight personal Claude assistant that runs agents in isolated Linux containers.

| Aspect | NanoClaw | OpenClaw |
|--------|----------|----------|
| **Code size** | ~3,900 lines | ~434,000 lines |
| **Processes** | 1 Node.js | 4-5 different processes |
| **Configuration** | Minimal (env vars) | 8+ config files |
| **Dependencies** | ~10 | 45+ |
| **Channels** | 1-2 (pluggable) | 15+ abstractions |
| **Security** | Container isolation | Application-level ACLs |
| **Memory** | CLAUDE.md + SQLite | QMD (proprietary) |

### Memory System

NanoClaw uses a hierarchical file-based memory:

1. **CLAUDE.md Files** - Primary persistent memory (per-group)
2. **SQLite Database** - Messages, groups, tasks, scheduler state
3. **Session Storage** - Claude Agent SDK transcripts

### Group Folders (Why Namespaces)

NanoClaw organizes work by **group folders**:

```
/workspace/
├── main/           # Primary group
│   └── CLAUDE.md
├── family/         # Family conversations
│   └── CLAUDE.md
└── work/           # Work projects
    └── CLAUDE.md
```

**This is why the MCP server's `namespace` parameter is important** - it maps directly to `groupFolder`:

```typescript
// NanoClaw hook calls MCP with namespace = groupFolder
const namespace = input.groupFolder; // "main", "family", "work"

await mcpClient.callTool('totalreclaw_recall', {
  query: input.userMessage,
  namespace: namespace,
  k: 8
});
```

---

## Agent Instruction Mechanism

### How the Agent Knows to Call Tools

**See**: [TS: TotalReclaw MCP Server](../mcp/TS: TotalReclaw MCP Server.md) for the generic instruction mechanism.

### NanoClaw-Specific Enhancements

In addition to the generic MCP tool descriptions, NanoClaw adds:

1. **Hook-based automatic calls** (agent doesn't need to remember)
2. **Enhanced system prompt** via skill's `SKILL.md`
3. **Context injection** from hook results

```
User sends message
       │
       ▼
┌──────────────────────────────────────┐
│ before-agent-start hook              │
│                                      │
│ const memories = await recall(       │
│   query: userMessage,                │
│   namespace: groupFolder             │
│ );                                   │
│                                      │
│ // Inject into agent context         │
│ context.relevantMemories = memories; │
└──────────────────────────────────────┘
       │
       ▼
Agent processes with memory context
       │
       ▼
┌──────────────────────────────────────┐
│ agent-end hook                       │
│                                      │
│ if (turnCount % 5 === 0) {           │
│   const facts = extract(turns);      │
│   await remember(facts, namespace);  │
│ }                                    │
└──────────────────────────────────────┘
```

---

## Implementation Details

### Phase 1: Install Generic MCP Server

The skill adds the generic `@totalreclaw/mcp-server` as a dependency:

```json
// modify/container/agent-runner/package.json
{
  "dependencies": {
    "@totalreclaw/mcp-server": "^1.0.0"
  }
}
```

### Phase 2: Add NanoClaw Hooks

```
add/
└── src/
    └── totalreclaw/
        └── hooks/
            ├── before-agent-start.ts
            ├── agent-end.ts
            └── pre-compact.ts
```

#### before-agent-start.ts

```typescript
import type { MCPServer } from '@totalreclaw/mcp-server';

export async function beforeAgentStart(
  mcp: MCPServer,
  input: { userMessage: string; groupFolder: string }
): Promise<{ contextString: string }> {
  // Call generic MCP server with NanoClaw's namespace
  const result = await mcp.callTool('totalreclaw_recall', {
    query: input.userMessage,
    namespace: input.groupFolder,  // <-- NanoClaw-specific
    k: 8,
    include_decay: true,
  });

  const memories = result.memories
    .filter(m => m.decay_score > 0.3)
    .map(m => `• ${m.fact_text}`)
    .join('\n');

  return {
    contextString: memories ? `## Relevant Memories\n${memories}` : '',
  };
}
```

### Phase 3: System Prompt Integration

The skill modifies NanoClaw's system prompt to include TotalReclaw instructions:

```typescript
// modify/container/agent-runner/src/index.ts
const TOTALRECLAW_PROMPT = `
## TotalReclaw Integration

You have access to encrypted persistent memory via these MCP tools:
- totalreclaw_remember: Store facts explicitly
- totalreclaw_recall: Search memories
- totalreclaw_forget: Remove memories
- totalreclaw_export: Backup your vault
- totalreclaw_import: Restore from backup (cross-agent portability)

Relevant memories are automatically loaded at conversation start.
You can explicitly store important facts using totalreclaw_remember.
`;

// Inject into system prompt
systemPrompt += '\n' + TOTALRECLAW_PROMPT;
```

---

## Key Management

### Per-Group Credentials

```
/workspace/group/
└── .totalreclaw/
    ├── credentials.enc    # Encrypted master key
    └── config.json        # Server URL, namespace
```

### Credential Derivation

```typescript
// Derive encryption key from recovery phrase
const masterKey = await argon2id(masterPassword, salt);

// Store encrypted in .totalreclaw/credentials.enc
// Never stored in plaintext
```

---

## Configuration

### Environment Variables (Set by Skill)

```bash
TOTALRECLAW_SERVER_URL=http://localhost:8080
TOTALRECLAW_NAMESPACE=${groupFolder}  # Set dynamically per-group
TOTALRECLAW_RECOVERY_PHRASE=${from_credentials_enc}
```

### Skill manifest.yaml

```yaml
name: add-totalreclaw
version: 0.3.0
description: Add end-to-end encrypted memory to NanoClaw

depends_on:
  - name: totalreclaw-mcp-server
    version: ">=1.0.0"

modifies:
  - container/agent-runner/package.json
  - container/agent-runner/src/index.ts

adds:
  - src/totalreclaw/hooks/*.ts
  - src/totalreclaw/extraction/*.ts
```

---

## Testing Strategy

### Unit Tests
- Hook functions (before-agent-start, agent-end)
- Namespace mapping
- Fact extraction

### Integration Tests
- MCP server communication
- End-to-end memory flow
- Cross-group isolation

---

## References

- **Generic MCP Server**: [TS: TotalReclaw MCP Server](../mcp/TS: TotalReclaw MCP Server.md)
- **Client Library**: `/client/`
- **Extraction Prompts**: `/skill/src/extraction/prompts.ts`

| Component | Path | Reusability | Notes |
|-----------|------|-------------|-------|
| XChaCha20-Poly1305 | `/client/src/crypto/cipher.ts` | **Direct** | None |
| Blind Indices | `/client/src/crypto/blind.ts` | **Direct** | None |
| LSH Index | `/client/src/lsh/` | **Direct** | None |
| ONNX Embedding | `/client/src/embedding/` | **Direct** | onnxruntime-node works in containers |
| BM25 + RRF | `/client/src/search/` | **Direct** | None |
| Protobuf API | `/client/src/api/` | **Direct** | Config change needed |

#### Runtime Compatibility

```typescript
// Both use Node.js 18+
// Both compile to ESM
// Dependencies: protobufjs, onnxruntime-node, argon2, tweetnacl
// All work in Docker/Apple Container environments
```

### What Can Be Reused From `/skill/`

| Component | Path | Reusability | Notes |
|-----------|------|-------------|-------|
| Extraction prompts | `/skill/src/extraction/prompts.ts` | **Direct** | None |
| JSON schemas | `/skill/src/extraction/prompts.ts` | **Direct** | None |
| Type definitions | `/skill/src/types.ts` | **Partial** | Need NanoClawContext |
| Hook patterns | `/skill/src/` | **Adapt** | Different hook system |

#### Extraction Prompts (Reusable As-Is)

```typescript
// From /skill/src/extraction/prompts.ts
export const PRE_COMPACTION_PROMPT;      // Comprehensive 20-turn extraction
export const POST_TURN_PROMPT;           // Lightweight 3-turn extraction
export const EXPLICIT_COMMAND_PROMPT;    // "remember that..." handling
export const DEDUP_JUDGE_PROMPT;         // ADD/UPDATE/DELETE/NOOP logic
export const EXTRACTION_RESPONSE_SCHEMA; // Structured output validation
```

#### Types (Need Adaptation)

```typescript
// OpenClaw-specific (needs change)
export interface OpenClawContext {
  userMessage: string;
  history: ConversationTurn[];
  agentId: string;
  sessionId: string;
  tokenCount: number;
  tokenLimit: number;
}

// NanoClaw-specific (create new)
export interface NanoClawContext {
  userMessage: string;
  groupFolder: string;
  chatJid: string;
  isMain: boolean;
  sessionId: string;
  conversationHistory: Array<{ role: 'user' | 'assistant'; content: string }>;
}
```

### Summary: Reusability Matrix

| Component | Source | Reusability | Effort |
|-----------|--------|-------------|--------|
| XChaCha20-Poly1305 encryption | `/client/src/crypto/cipher.ts` | Direct | None |
| Blind indices | `/client/src/crypto/blind.ts` | Direct | None |
| LSH hashing | `/client/src/lsh/` | Direct | None |
| Embedding generation | `/client/src/embedding/` | Direct | None |
| Search reranking | `/client/src/search/` | Direct | None |
| Protobuf client | `/client/src/api/` | Direct | Config change |
| Extraction prompts | `/skill/src/extraction/prompts.ts` | Direct | None |
| JSON schemas | `/skill/src/extraction/prompts.ts` | Direct | None |
| Type definitions | `/skill/src/types.ts` | Partial | Low |
| Hook implementations | `/skill/src/` | Rewrite | Medium |
| Tool definitions | `/skill/src/` | Adapt | Low |

---

## Integration Architecture

### Component Diagram

```
+------------------------------------------------------------------+
|                         HOST MACHINE                              |
|  +------------------------------------------------------------+  |
|  |                     NanoClaw Process                        |  |
|  |                                                            |  |
|  |  src/index.ts ----> src/container-runner.ts                |  |
|  |                            |                               |  |
|  +----------------------------|-------------------------------+  |
|                               | spawn                          |
|                               v                                 |
|  +------------------------------------------------------------+  |
|  |                     CONTAINER                               |  |
|  |                                                            |  |
|  |  +----------------------+    +--------------------------+  |  |
|  |  | agent-runner         |    | MCP Servers              |  |  |
|  |  | (Claude Agent SDK)   |---|                          |  |  |
|  |  |                      |    |  +------------------+    |  |  |
|  |  |  PreCompact hook --->|    |  | nanoclaw         |    |  |  |
|  |  |  PostTurn hook ----->|    |  +------------------+    |  |  |
|  |  |                      |    |  +------------------+    |  |  |
|  |  +----------------------+    |  | totalreclaw       |    |  |  |
|  |                              |  +------------------+    |  |  |
|  |                              +--------------------------+  |  |
|  |                                        |                   |  |
|  |  +------------------------------------|-----------------+ |  |
|  |  |  /workspace/group/                 |                 | |  |
|  |  |    CLAUDE.md  <--- sync --->  [totalreclaw storage] | |  |
|  |  |    .totalreclaw/                    |                 | |  |
|  |  |      credentials.enc               v                 | |  |
|  |  |      config.json          HTTP over TLS              | |  |
|  |  +------------------------------------|-----------------+ |  |
|  |                                      |                   |  |
|  +--------------------------------------|-------------------+  |
|                                         |                      |
+-----------------------------------------|----------------------+
                                          |
                                          v
                              +----------------------+
                              |  TotalReclaw Server   |
                              |  (external service)  |
                              |                      |
                              |  - Encrypted storage |
                              |  - Blind index (GIN) |
                              |  - Server-blind      |
                              +----------------------+
```

### Integration Approach: MCP Server

We recommend the **MCP Server** approach for these reasons:

| Factor | MCP Server | Direct Library |
|--------|------------|----------------|
| Consistency | Matches NanoClaw's existing pattern | Different integration model |
| Modularity | Can be added/removed independently | Tightly coupled |
| Maintainability | Clear separation of concerns | Mixed concerns |
| Tool Discovery | Claude Code auto-discovers tools | Manual registration |
| Security | Runs inside container with same isolation | Same |

### Data Flow

```
1. MESSAGE RECEIVED
   |
   v
2. CONTAINER STARTS
   |
   v
3. BEFORE_AGENT_START hook (via MCP tool call)
   |-- Query TotalReclaw with user message
   |-- Decrypt and rerank results
   |-- Inject memories into context
   |
   v
4. AGENT PROCESSING
    |-- Has access to totalreclaw_remember tool
    |-- Has access to totalreclaw_recall tool
    |-- Has access to totalreclaw_forget tool
    |-- Has access to totalreclaw_export tool
    |-- Has access to totalreclaw_import tool
   |
   v
5. AGENT_END hook (periodic or explicit)
   |-- Extract facts from conversation
   |-- Deduplicate against existing
   |-- Encrypt and store new memories
   |
   v
6. PRE_COMPACT hook (before context truncation)
   |-- Full extraction from all turns
   |-- Sync CLAUDE.md changes
   |-- Flush pending memories
   |
   v
7. CONTAINER EXITS (or goes idle)
```

### Memory Isolation

Each group's memories are isolated via namespacing:

```
+------------------+
|  TotalReclaw      |
|  Server          |
+------------------+
        |
        | userId (derived from recovery phrase)
        v
+------------------+
|  User's vault    |
+------------------+
        |
        | namespace per group
        v
+----------+  +----------+  +----------+
| main     |  | family   |  | work     |
| memories |  | memories |  | memories |
+----------+  +----------+  +----------+
```

```typescript
// Namespace format: {groupFolder}
const namespace = groupFolder; // "main", "family", "work"

// Fact storage includes namespace
await client.remember(factText, { namespace });

// Recall scoped to namespace
const results = await client.recall(query, { namespace, k: 8 });
```

---

## Implementation Details

### Phase 1: Create Skill Package (Week 1)

Create the `/add-totalreclaw` skill package for NanoClaw's skills engine.

#### Directory Structure

```
.claude/skills/add-totalreclaw/
+-- manifest.yaml           # Skill metadata
+-- SKILL.md               # Installation instructions
+-- modify/
|   +-- container/
|   |   +-- agent-runner/
|   |       +-- package.json        # Add @totalreclaw/mcp-server dependency
|   |       +-- src/
|   |           +-- nanoclaw-agent-runner.ts  # Spawns @totalreclaw/mcp-server via npx
|   +-- src/
|       +-- container-runtime.ts    # Add TotalReclaw env vars
+-- add/
|   +-- src/
|       +-- totalreclaw/
|           +-- hooks/
|           +-- +-- before-agent-start.ts
|           +-- +-- agent-end.ts
|           +-- +-- pre-compact.ts
|           +-- extraction/
|               +-- prompts.ts      # Copied from /skill/
|               +-- extractor.ts
+-- intent/
|   +-- mcp-integration.md   # How MCP server integrates
|   +-- hooks.md             # Hook implementation invariants
+-- tests/
    +-- integration.test.ts
```

#### manifest.yaml

```yaml
name: add-totalreclaw
version: 0.2.0
description: Add end-to-end encrypted memory to NanoClaw
author: TotalReclaw Team

dependencies:
  - name: nanoclaw
    minVersion: 0.1.0
    maxVersion: 1.0.0

conflicts: []

modifies:
  - container/agent-runner/package.json
  - container/agent-runner/src/index.ts

adds:
  - src/totalreclaw/hooks/*.ts
  - src/totalreclaw/extraction/*.ts

npmDependencies:
  - "@totalreclaw/mcp-server": "^1.0.0"
```

### Phase 2: MCP Server Integration (Week 1-2)

> **Architecture change (v0.3.0):** NanoClaw no longer maintains a self-contained MCP server.
> Instead, `nanoclaw-agent-runner.ts` spawns the published `@totalreclaw/mcp-server` npm
> package as a stdio child process, ensuring full feature parity with Claude Desktop and
> all other MCP clients.

#### nanoclaw-agent-runner.ts

The agent-runner registers `@totalreclaw/mcp-server` as an MCP server:

```typescript
/**
 * NanoClaw agent-runner that registers TotalReclaw MCP server.
 * Spawns `@totalreclaw/mcp-server` via npx — no custom MCP code needed.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

import { TotalReclaw } from '@totalreclaw/client';
import { rememberTool } from './tools/remember.js';
import { recallTool } from './tools/recall.js';
import { forgetTool } from './tools/forget.js';
import { exportTool } from './tools/export.js';

// Server configuration from environment
const SERVER_URL = process.env.TOTALRECLAW_SERVER_URL || 'http://127.0.0.1:8080';
const NAMESPACE = process.env.TOTALRECLAW_NAMESPACE || 'default';
const MASTER_PASSWORD = process.env.TOTALRECLAW_RECOVERY_PHRASE;

// Initialize TotalReclaw client
let client: TotalReclaw | null = null;

async function getClient(): Promise<TotalReclaw> {
  if (!client) {
    client = new TotalReclaw({ serverUrl: SERVER_URL });
    await client.init();

    // If credentials exist, login; otherwise register
    const credentialsPath = '/workspace/group/.totalreclaw/credentials.enc';
    if (fs.existsSync(credentialsPath)) {
      const credentials = await loadCredentials(credentialsPath, MASTER_PASSWORD);
      await client.login(credentials.userId, MASTER_PASSWORD, credentials.salt);
    } else {
      const userId = await client.register(MASTER_PASSWORD);
      await saveCredentials(credentialsPath, { userId, salt: client.salt });
    }
  }
  return client;
}

// Create MCP server
const server = new Server(
  { name: 'totalreclaw', version: '0.2.0' },
  { capabilities: { tools: {} } }
);

// Register tools list handler
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    rememberTool.definition,
    recallTool.definition,
    forgetTool.definition,
    exportTool.definition,
  ],
}));

// Register tool call handler
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    const om = await getClient();

    switch (name) {
      case 'totalreclaw_remember':
        return await rememberTool.handler(om, args, NAMESPACE);

      case 'totalreclaw_recall':
        return await recallTool.handler(om, args, NAMESPACE);

      case 'totalreclaw_forget':
        return await forgetTool.handler(om, args);

      case 'totalreclaw_export':
        return await exportTool.handler(om, args, NAMESPACE);

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    return {
      content: [
        {
          type: 'text',
          text: `Error: ${error instanceof Error ? error.message : 'Unknown error'}`,
        },
      ],
      isError: true,
    };
  }
});

// Start server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('TotalReclaw MCP server started');
}

main().catch(console.error);
```

### Phase 3: Hook Integration (Week 2)

#### before-agent-start.ts

```typescript
/**
 * BEFORE_AGENT_START Hook
 *
 * Retrieves relevant memories before agent processes user message.
 */

import type { TotalReclaw } from '@totalreclaw/client';

export interface BeforeAgentStartInput {
  userMessage: string;
  groupFolder: string;
  sessionId?: string;
}

export interface BeforeAgentStartOutput {
  contextString?: string;
  memories: Array<{
    text: string;
    score: number;
    type: string;
  }>;
  latencyMs: number;
}

export async function beforeAgentStart(
  client: TotalReclaw,
  input: BeforeAgentStartInput,
  maxMemories: number = 8
): Promise<BeforeAgentStartOutput> {
  const startTime = Date.now();

  try {
    // Query TotalReclaw with namespace isolation
    const results = await client.recall(input.userMessage, {
      namespace: input.groupFolder,
      k: maxMemories,
    });

    // Format memories for context injection
    const memories = results.map((r) => ({
      text: r.fact.text,
      score: r.score,
      type: r.fact.metadata?.type || 'fact',
    }));

    // Build context string
    const contextString = memories.length > 0
      ? formatMemoriesForContext(memories)
      : undefined;

    return {
      contextString,
      memories,
      latencyMs: Date.now() - startTime,
    };
  } catch (error) {
    console.error('beforeAgentStart error:', error);
    return {
      memories: [],
      latencyMs: Date.now() - startTime,
    };
  }
}

function formatMemoriesForContext(memories: Array<{ text: string; score: number; type: string }>): string {
  const lines = ['## Relevant Memories\n'];
  for (const m of memories) {
    lines.push(`- [${m.type}] ${m.text}`);
  }
  return lines.join('\n');
}
```

#### agent-end.ts

```typescript
/**
 * AGENT_END Hook
 *
 * Extracts and stores facts after agent completes turn.
 * Uses POST_TURN_PROMPT for lightweight extraction.
 */

import type { TotalReclaw } from '@totalreclaw/client';
import { POST_TURN_PROMPT, validateExtractionResponse, formatConversationHistory } from '@totalreclaw/client/extraction';

export interface AgentEndInput {
  conversationHistory: Array<{
    role: 'user' | 'assistant';
    content: string;
  }>;
  groupFolder: string;
  turnCount: number;
}

export interface AgentEndOutput {
  factsExtracted: number;
  factsStored: number;
}

export async function agentEnd(
  client: TotalReclaw,
  llmClient: LLMClient,
  input: AgentEndInput,
  extractInterval: number = 5,
  minImportance: number = 6
): Promise<AgentEndOutput> {
  // Only extract periodically
  if (input.turnCount % extractInterval !== 0) {
    return { factsExtracted: 0, factsStored: 0 };
  }

  try {
    // Format recent turns
    const history = formatConversationHistory(
      input.conversationHistory.slice(-3).map((t, i) => ({
        role: t.role,
        content: t.content,
        timestamp: new Date(),
      }))
    );

    // Get existing memories for deduplication
    const existingMemories = await client.recall('*history*', {
      namespace: input.groupFolder,
      k: 20,
    });

    // Format prompt
    const prompt = POST_TURN_PROMPT.format({
      conversationHistory: history,
      existingMemories: formatExistingMemories(existingMemories),
    });

    // Call LLM for extraction
    const response = await llmClient.generate(prompt.system, prompt.user, {
      responseFormat: { type: 'json_object' },
    });

    // Validate response
    const parsed = JSON.parse(response);
    const validation = validateExtractionResponse(parsed);

    if (!validation.valid) {
      console.error('Extraction validation failed:', validation.errors);
      return { factsExtracted: 0, factsStored: 0 };
    }

    // Store new facts (ADD action only for post-turn)
    let factsStored = 0;
    for (const fact of validation.facts!) {
      if (fact.action === 'ADD' && fact.importance >= minImportance) {
        await client.remember(fact.factText, {
          namespace: input.groupFolder,
          importance: fact.importance / 10,
          metadata: {
            type: fact.type,
            entities: fact.entities,
            relations: fact.relations,
          },
        });
        factsStored++;
      }
    }

    return {
      factsExtracted: validation.facts!.length,
      factsStored,
    };
  } catch (error) {
    console.error('agentEnd error:', error);
    return { factsExtracted: 0, factsStored: 0 };
  }
}
```

#### pre-compact.ts

```typescript
/**
 * PRE_COMPACT Hook
 *
 * Full memory extraction before context compaction.
 * Uses PRE_COMPACTION_PROMPT for comprehensive extraction.
 */

import type { TotalReclaw } from '@totalreclaw/client';
import { PRE_COMPACTION_PROMPT, validateExtractionResponse } from '@totalreclaw/client/extraction';

export interface PreCompactInput {
  transcript: string;
  groupFolder: string;
  claudeMdPath: string;
}

export interface PreCompactOutput {
  factsExtracted: number;
  factsStored: number;
  claudeMdUpdated: boolean;
}

export async function preCompact(
  client: TotalReclaw,
  llmClient: LLMClient,
  input: PreCompactInput
): Promise<PreCompactOutput> {
  try {
    // Get existing memories
    const existingMemories = await client.recall('*', {
      namespace: input.groupFolder,
      k: 100,
    });

    // Format prompt with last 20 turns
    const prompt = PRE_COMPACTION_PROMPT.format({
      conversationHistory: input.transcript,
      existingMemories: formatExistingMemories(existingMemories),
    });

    // Call LLM
    const response = await llmClient.generate(prompt.system, prompt.user, {
      responseFormat: { type: 'json_object' },
    });

    // Validate
    const parsed = JSON.parse(response);
    const validation = validateExtractionResponse(parsed);

    if (!validation.valid) {
      console.error('Pre-compact validation failed:', validation.errors);
      return { factsExtracted: 0, factsStored: 0, claudeMdUpdated: false };
    }

    // Process all actions
    let factsStored = 0;
    for (const fact of validation.facts!) {
      switch (fact.action) {
        case 'ADD':
          await client.remember(fact.factText, {
            namespace: input.groupFolder,
            importance: fact.importance / 10,
            metadata: { type: fact.type },
          });
          factsStored++;
          break;

        case 'UPDATE':
          if (fact.existingFactId) {
            await client.forget(fact.existingFactId);
            await client.remember(fact.factText, {
              namespace: input.groupFolder,
              importance: fact.importance / 10,
              metadata: { type: fact.type },
            });
            factsStored++;
          }
          break;

        case 'DELETE':
          if (fact.existingFactId) {
            await client.forget(fact.existingFactId);
          }
          break;

        case 'NOOP':
          // Skip
          break;
      }
    }

    // Optionally sync to CLAUDE.md
    let claudeMdUpdated = false;
    if (input.claudeMdPath) {
      claudeMdUpdated = await syncToClaudeMd(client, input.groupFolder, input.claudeMdPath);
    }

    return {
      factsExtracted: validation.facts!.length,
      factsStored,
      claudeMdUpdated,
    };
  } catch (error) {
    console.error('preCompact error:', error);
    return { factsExtracted: 0, factsStored: 0, claudeMdUpdated: false };
  }
}

async function syncToClaudeMd(
  client: TotalReclaw,
  namespace: string,
  claudeMdPath: string
): Promise<boolean> {
  // Export high-importance memories to CLAUDE.md
  const memories = await client.recall('*', { namespace, k: 50 });

  // Filter to important ones
  const important = memories.filter(m =>
    (m.fact.metadata?.importance || 0.5) >= 0.7
  );

  if (important.length === 0) return false;

  // Read existing CLAUDE.md
  const existing = fs.existsSync(claudeMdPath)
    ? fs.readFileSync(claudeMdPath, 'utf-8')
    : '';

  // Append TotalReclaw section if not exists
  if (!existing.includes('## TotalReclaw Sync')) {
    const section = '\n\n## TotalReclaw Sync\n\n' +
      important.map(m => `- ${m.fact.text}`).join('\n');

    fs.writeFileSync(claudeMdPath, existing + section);
    return true;
  }

  return false;
}
```

### Phase 4: Container Configuration (Week 2-3)

#### container/agent-runner/package.json additions

```json
{
  "dependencies": {
    "@totalreclaw/client": "^0.3.0",
    "@modelcontextprotocol/sdk": "^1.0.0"
  }
}
```

#### Environment Variables (passed to container)

```bash
# Required
TOTALRECLAW_SERVER_URL=https://api.totalreclaw.xyz
TOTALRECLAW_RECOVERY_PHRASE=<from user>

# Optional
TOTALRECLAW_ENABLED=true
TOTALRECLAW_NAMESPACE=<group-folder>
TOTALRECLAW_AUTO_EXTRACT=true
TOTALRECLAW_EXTRACT_INTERVAL=3
TOTALRECLAW_MIN_IMPORTANCE=6
TOTALRECLAW_MAX_CONTEXT=8
```

---

## Key Management

### Credential Storage Location

```
/workspace/group/.totalreclaw/
+-- credentials.enc    # Encrypted credentials (XChaCha20-Poly1305)
+-- config.json        # Non-sensitive config (server URL, etc.)
+-- salt.bin           # Argon2 salt for key derivation
```

### Credential Flow

```
1. User sets recovery phrase (via main channel command)
       |
       v
2. Password derives encryption key via Argon2id
       |
       v
3. Key encrypts TotalReclaw auth credentials
       |
       v
4. Encrypted credentials stored in .totalreclaw/credentials.enc
       |
       v
5. On container start, credentials decrypted with password from env
```

### Security Considerations

| Threat | Mitigation |
|--------|------------|
| Server compromise | End-to-end encryption (server never sees plaintext) |
| Credential theft | Credentials stored encrypted in group folder |
| Cross-group access | Per-group namespace isolation |
| Memory injection | All facts validated before storage |
| Query leakage | Blind indices, server doesn't see query content |

### Container Security

```typescript
// Secrets passed via stdin, not environment
const input: ContainerInput = {
  // ...
  secrets: {
    TOTALRECLAW_RECOVERY_PHRASE: masterPassword,
    // ... other secrets
  }
};

// Secrets deleted after reading
try { fs.unlinkSync('/tmp/input.json'); } catch { /* ignore */ }
```

### Access Control

```typescript
// Main group can access all namespaces
if (isMain) {
  // Can recall from any namespace
  // Can modify global memories
}

// Non-main groups isolated to their namespace
if (!isMain) {
  // Can only recall from own namespace
  // Cannot modify global memories
}
```

---

## API Design

### MCP Tools

#### `totalreclaw_remember`

```typescript
{
  name: "totalreclaw_remember",
  description: "Store a fact in encrypted memory for long-term recall",
  inputSchema: {
    type: "object",
    properties: {
      text: {
        type: "string",
        description: "The fact to remember (atomic, concise)"
      },
      type: {
        type: "string",
        enum: ["fact", "preference", "decision", "episodic", "goal"],
        default: "fact"
      },
      importance: {
        type: "number",
        minimum: 1,
        maximum: 10,
        default: 5
      }
    },
    required: ["text"]
  }
}
```

#### `totalreclaw_recall`

```typescript
{
  name: "totalreclaw_recall",
  description: "Search encrypted memories for relevant information",
  inputSchema: {
    type: "object",
    properties: {
      query: {
        type: "string",
        description: "The search query"
      },
      k: {
        type: "number",
        default: 8,
        description: "Number of results to return"
      }
    },
    required: ["query"]
  }
}
```

#### `totalreclaw_forget`

```typescript
{
  name: "totalreclaw_forget",
  description: "Delete a specific memory by ID",
  inputSchema: {
    type: "object",
    properties: {
      factId: {
        type: "string",
        description: "The ID of the fact to forget"
      }
    },
    required: ["factId"]
  }
}
```

#### `totalreclaw_export`

```typescript
{
  name: "totalreclaw_export",
  description: "Export all memories in plaintext (user portability)",
  inputSchema: {
    type: "object",
    properties: {
      format: {
        type: "string",
        enum: ["json", "markdown"],
        default: "markdown"
      }
    }
  }
}
```

---

## Configuration

### Environment Variables

```bash
# In container environment
TOTALRECLAW_SERVER_URL=https://api.totalreclaw.xyz
TOTALRECLAW_ENABLED=true
TOTALRECLAW_AUTO_EXTRACT=true
TOTALRECLAW_EXTRACT_INTERVAL=3  # turns
TOTALRECLAW_MIN_IMPORTANCE=6    # 1-10
TOTALRECLAW_MAX_CONTEXT=8       # memories in context
```

### Group Configuration

```json
// In SQLite registered_groups.container_config
{
  "totalreclaw": {
    "enabled": true,
    "namespace": "family",
    "syncWithClaudeMd": true
  }
}
```

### Main Channel Commands

```
@Andy configure memory server https://api.totalreclaw.xyz
@Andy set memory password [recovery phrase]
@Andy export all memories
@Andy sync memories with CLAUDE.md
```

---

## Testing Strategy

### Unit Tests

```typescript
describe('TotalReclaw MCP Server', () => {
  it('should store and retrieve memories', async () => {
    const client = createTestClient();
    await client.remember('Test fact', { namespace: 'test' });
    const results = await client.recall('Test', { namespace: 'test' });
    expect(results).toHaveLength(1);
  });

  it('should isolate namespaces', async () => {
    const client = createTestClient();
    await client.remember('Namespace A', { namespace: 'a' });
    await client.remember('Namespace B', { namespace: 'b' });
    const resultsA = await client.recall('*', { namespace: 'a' });
    const resultsB = await client.recall('*', { namespace: 'b' });
    expect(resultsA).toHaveLength(1);
    expect(resultsB).toHaveLength(1);
  });

  it('should validate extraction responses', () => {
    const validResponse = {
      facts: [{
        factText: 'User likes coffee',
        type: 'preference',
        importance: 7,
        confidence: 0.9,
        action: 'ADD',
        entities: [],
        relations: [],
      }],
    };

    const result = validateExtractionResponse(validResponse);
    expect(result.valid).toBe(true);
  });
});
```

### Integration Tests

```typescript
describe('NanoClaw + TotalReclaw Integration', () => {
  it('should inject memories into agent context', async () => {
    // 1. Store a memory
    await totalreclaw.remember('User prefers TypeScript');

    // 2. Send a message
    const response = await nanoclaw.sendMessage('What language should I use?');

    // 3. Verify memory was used
    expect(response).toContain('TypeScript');
  });

  it('should extract facts from conversation', async () => {
    // 1. Have a conversation
    await nanoclaw.sendMessage('I work at Google now');
    await nanoclaw.sendMessage('Remember that');

    // 2. Verify fact was extracted
    const memories = await totalreclaw.recall('work');
    expect(memories.some(m => m.text.includes('Google'))).toBe(true);
  });
});
```

### End-to-End Tests

```bash
# Test full flow with real container
npm run test:e2e -- --group test --message "remember that I like coffee"
npm run test:e2e -- --group test --message "what do I like to drink?"
# Should recall "coffee"
```

---

## Migration Path

### For Existing NanoClaw Users

1. **No breaking changes** - CLAUDE.md continues to work
2. **Opt-in** - TotalReclaw disabled by default
3. **Gradual adoption** - Can use both systems simultaneously

### Migration Steps

```
1. Run /add-totalreclaw skill in NanoClaw
2. Deploy TotalReclaw server (or use hosted version)
3. Configure server URL via main channel
4. Set recovery phrase
5. Optionally: Import existing CLAUDE.md content
```

### CLAUDE.md Sync

```typescript
// Bidirectional sync option
async function syncClaudeMdToTotalReclaw(groupFolder: string) {
  const claudeMd = fs.readFileSync(`/workspace/group/CLAUDE.md`, 'utf-8');
  const facts = await extractFactsFromMarkdown(claudeMd);
  for (const fact of facts) {
    await client.remember(fact, { namespace: groupFolder });
  }
}

async function syncTotalReclawToClaudeMd(groupFolder: string) {
  const memories = await client.recall('*', { namespace: groupFolder, k: 100 });
  const markdown = formatMemoriesAsMarkdown(memories);
  fs.writeFileSync(`/workspace/group/CLAUDE.md`, markdown);
}
```

---

## File Structure Reference

### New Files (in NanoClaw)

```
nanoclaw/
+-- .claude/skills/add-totalreclaw/
|   +-- manifest.yaml
|   +-- SKILL.md
|   +-- modify/
|   |   +-- container/
|   |       +-- agent-runner/
|   |           +-- package.json          # MODIFIED
|   |           +-- src/
|   |               +-- nanoclaw-agent-runner.ts  # Spawns @totalreclaw/mcp-server
|   +-- add/
|       +-- src/
|           +-- totalreclaw/
|               +-- hooks/
|                   +-- before-agent-start.ts
|                   +-- agent-end.ts
|                   +-- pre-compact.ts
+-- groups/*/
    +-- .totalreclaw/
        +-- credentials.enc
        +-- config.json
        +-- salt.bin
```

### TotalReclaw Files (Referenced)

```
totalreclaw/
+-- client/                     # Reused directly
|   +-- src/crypto/
|   +-- src/lsh/
|   +-- src/embedding/
|   +-- src/search/
|   +-- src/api/
+-- skill/                      # Partially reused
|   +-- src/extraction/prompts.ts
|   +-- src/types.ts
+-- server/                     # External service
    +-- src/
    +-- docker-compose.yml
```

---

## References

- [NanoClaw Repository](https://github.com/qwibitai/nanoclaw)
- [NanoClaw Security Model](https://github.com/qwibitai/nanoclaw/blob/main/docs/SECURITY.md)
- [TotalReclaw Client README](/client/README.md)
- [TotalReclaw Skill Package](/skill/)
- [MCP Protocol Specification](https://modelcontextprotocol.io)
- [Claude Agent SDK Documentation](https://docs.anthropic.com)
