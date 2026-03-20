<!--
Product: TotalReclaw
Formerly: tech specs/mcp/TS: TotalReclaw MCP Server.md
Version: 1.0
Last updated: 2026-02-24
-->

# Technical Specification: TotalReclaw MCP Server

> **Generic MCP server for end-to-end encrypted memory**
> **Works with: Claude Desktop, NanoClaw, any MCP-compatible client**

**Version:** 1.0.0
**Date:** 2026-02-23
**Status:** Ready for Implementation

---

## Overview

This specification describes a **generic MCP (Model Context Protocol) server** that exposes TotalReclaw's end-to-end encrypted memory capabilities to any MCP-compatible client.

### Why MCP?

MCP is becoming the standard for AI assistant tooling:
- **Claude Desktop** uses MCP for tool integration
- **NanoClaw** uses MCP servers inside containers
- **Other clients** adopting MCP (IDEs, chat apps, etc.)

By building a generic MCP server, TotalReclaw becomes universally accessible.

---

## Agent Instruction Mechanism

### The Problem

Just exposing tools via MCP isn't enough. The agent needs to know:
1. **What tools exist** → Handled by MCP `ListTools`
2. **When to call them** → Needs instruction
3. **How to call them** → Needs examples

### The Solution: Three-Layer Instruction

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: MCP Tool Schema                                   │
│  - Tool name, description, input schema                      │
│  - Discovered automatically via ListTools                    │
├─────────────────────────────────────────────────────────────┤
│  LAYER 2: Tool Descriptions (Rich)                           │
│  - WHEN to use the tool                                      │
│  - EXAMPLES of usage                                         │
│  - Edge cases and constraints                                │
├─────────────────────────────────────────────────────────────┤
│  LAYER 3: System Prompt Fragment                             │
│  - MCP server provides prompt to inject                      │
│  - Explains memory capabilities                              │
│  - Guides agent on memory lifecycle                          │
└─────────────────────────────────────────────────────────────┘
```

### Layer 1: MCP Tool Schema (Auto-Discovered)

```json
{
  "name": "totalreclaw_remember",
  "description": "Store a fact in encrypted memory",
  "inputSchema": {
    "type": "object",
    "properties": {
      "fact": { "type": "string" },
      "importance": { "type": "integer", "minimum": 1, "maximum": 10 },
      "namespace": { "type": "string" }
    },
    "required": ["fact"]
  }
}
```

### Layer 2: Rich Tool Descriptions

The MCP server provides extended descriptions:

```typescript
const REMEMBER_TOOL = {
  name: "totalreclaw_remember",
  description: `
Store a fact in your encrypted memory vault.

WHEN TO USE:
- User explicitly asks you to remember something ("remember that...")
- User shares a preference ("I prefer...", "I like...", "I hate...")
- User provides personal info (name, location, schedule)
- User corrects previous information about themselves

WHEN NOT TO USE:
- Temporary context (current conversation only)
- Information about others (only store user's own info)
- Sensitive credentials (use secure storage instead)

EXAMPLES:
- User: "Remember that I'm vegetarian"
  → Call: totalreclaw_remember({ fact: "User is vegetarian", importance: 7 })

- User: "My wife's birthday is March 15"
  → Call: totalreclaw_remember({ fact: "User's wife's birthday is March 15", importance: 6 })

- User: "Actually, I'm vegan now, not vegetarian"
  → Call: totalreclaw_remember({ fact: "User is vegan (updated from vegetarian)", importance: 7 })

IMPORTANCE GUIDE:
- 9-10: Critical identity (name, core values, major preferences)
- 7-8: Important preferences (dietary, work style, communication)
- 5-6: Moderate (minor preferences, schedule details)
- 3-4: Low (casual mentions, may forget)
- 1-2: Minimal (ephemeral context)
`,
  inputSchema: { /* ... */ }
};
```

### Layer 3: System Prompt Fragment

The MCP server provides a prompt fragment to inject:

```typescript
const SYSTEM_PROMPT_FRAGMENT = `
## TotalReclaw: Your Encrypted Memory Vault

You have access to an end-to-end encrypted memory system. The server never sees your memories in plaintext - everything is encrypted client-side with AES-256-GCM.

### Available Tools

- **totalreclaw_remember**: Store a new fact
- **totalreclaw_recall**: Search your memories semantically
- **totalreclaw_forget**: Remove a memory
- **totalreclaw_export**: Export your vault as portable Markdown
- **totalreclaw_import**: Import memories from exported backup

### Memory Lifecycle

1. **Storing**: Call totalreclaw_remember when you learn something worth remembering
2. **Retrieving**: Call totalreclaw_recall at conversation start to load context
3. **Updating**: If user corrects info, store the updated fact
4. **Decay**: Old/unimportant memories fade automatically (importance decay)

### Best Practices

- Store facts, not verbatim conversations
- Use importance 5-8 for most user preferences
- Search before storing to avoid duplicates
- Namespace isolates memories per context (work, personal, etc.)

### Privacy

Your memories are end-to-end encrypted. The server only sees encrypted blobs and blind indices (SHA-256 hashes). Even the server operator cannot read your memories.
`;
```

### How This Works in Different Clients

| Client | How Instructions Are Applied |
|--------|------------------------------|
| **Claude Desktop** | MCP `instructions` field + tool descriptions |
| **NanoClaw** | Injected via skill's system prompt modification |
| **Custom clients** | Read from MCP server's `/instructions` endpoint |

---

## Namespace Concept

### Why Namespaces?

Different contexts need isolated memories:
- **Personal projects** vs **Work projects**
- **Family chat** vs **Professional chat**
- **Per-user** in multi-user deployments

### Namespace in MCP

```typescript
// Namespace is optional, defaults to "default"
interface RememberInput {
  fact: string;
  importance?: number;
  namespace?: string;  // "work", "personal", "family", etc.
}

// Example: NanoClaw passes group folder as namespace
// Example: Claude Desktop could use "personal" or user-defined
```

### Namespace Scope

```
TotalReclaw Server
    │
    └── User Vault (derived from master password)
            │
            ├── namespace: "default"     → General memories
            ├── namespace: "work"         → Work-related
            ├── namespace: "family"       → Family/personal
            └── namespace: "project-x"    → Project-specific
```

---

## Tool Definitions

### totalreclaw_remember

```typescript
interface RememberInput {
  fact: string;           // The fact to store
  importance?: number;    // 1-10, default 5
  namespace?: string;     // Optional namespace
  metadata?: {            // Optional metadata
    type?: string;        // "preference" | "event" | "relationship" | "fact"
    expires_at?: string;  // ISO timestamp for time-limited facts
  };
}

interface RememberOutput {
  success: boolean;
  fact_id: string;        // UUID of stored fact
  was_duplicate: boolean; // True if similar fact already existed
  action: "created" | "updated" | "skipped";
}
```

### totalreclaw_recall

```typescript
interface RecallInput {
  query: string;          // Natural language query
  k?: number;             // Number of results (default 8)
  min_importance?: number;// Minimum importance filter (default 5)
  namespace?: string;     // Optional namespace
  include_decay?: boolean;// Apply decay scoring (default true)
}

interface RecallOutput {
  memories: Array<{
    fact_id: string;
    fact_text: string;
    score: number;        // 0.0 - 1.0 relevance
    importance: number;
    age_days: number;
    decay_score: number;
  }>;
  latency_ms: number;
}
```

### totalreclaw_forget

```typescript
interface ForgetIntput {
  fact_id?: string;       // Specific fact to forget
  query?: string;         // Or forget by semantic query
  namespace?: string;     // Optional namespace scope
}

interface ForgetOutput {
  deleted_count: number;
  fact_ids: string[];
}
```

### totalreclaw_export

```typescript
interface ExportInput {
  format?: "markdown" | "json";
  namespace?: string;     // Optional namespace scope
  include_metadata?: boolean;
}

interface ExportOutput {
  content: string;        // Exported content
  format: string;
  fact_count: number;
  exported_at: string;
}
```

### totalreclaw_import

```typescript
interface ImportInput {
  content: string;              // Exported content (JSON or Markdown)
  format?: "markdown" | "json"; // Auto-detected if not specified
  namespace?: string;           // Target namespace (defaults to source namespace)
  namespace_mapping?: Record<string, string>;  // Remap namespaces: {"work": "work-v2"}
  merge_strategy?: "skip_existing" | "overwrite" | "merge";  // Default: "skip_existing"
  reencrypt?: boolean;          // Re-encrypt with current master password (default: true)
  validate_only?: boolean;      // Parse and validate without importing (dry-run)
}

interface ImportOutput {
  success: boolean;
  facts_imported: number;
  facts_skipped: number;
  facts_merged: number;
  errors: Array<{
    line?: number;
    fact_id?: string;
    error: string;
  }>;
  warnings: string[];
  import_id: string;            // UUID for this import batch (for rollback)
}
```

#### Import Behavior

1. **Format Detection**: Auto-detect JSON vs Markdown based on content structure
2. **Validation**: 
   - Verify JSON schema or Markdown structure
   - Check required fields (fact_text, type, etc.)
   - Validate importance range (1-10)
3. **Deduplication** (merge_strategy):
   - `skip_existing`: If similar fact exists (semantic similarity > 0.85), skip
   - `overwrite`: Delete existing, import new
   - `merge`: Use conflict resolution (see v0.3.1 §340-357)
4. **Namespace Remapping**: Apply `namespace_mapping` before storage
5. **Re-encryption**: All facts encrypted with current master password
6. **Rollback**: `import_id` allows `totalreclaw_forget({ import_id })` within 24h

#### Conflict Resolution

Uses optimistic locking + LLM-assisted merge as defined in v0.3.1 §340-357:
- Version field on facts enables optimistic locking
- Conflicting updates trigger LLM-assisted merge
- Merge preserves higher importance and more recent timestamp
- User can override via explicit `overwrite` strategy

---

## MCP Server Implementation

### Server Structure

```typescript
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

const server = new Server(
  { name: 'totalreclaw', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

// Register tools
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [REMEMBER_TOOL, RECALL_TOOL, FORGET_TOOL, EXPORT_TOOL, IMPORT_TOOL],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  switch (name) {
    case 'totalreclaw_remember':
      return await handleRemember(args);
    case 'totalreclaw_recall':
      return await handleRecall(args);
    case 'totalreclaw_forget':
      return await handleForget(args);
    case 'totalreclaw_export':
      return await handleExport(args);
    case 'totalreclaw_import':
      return await handleImport(args);
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
});

// Provide system prompt fragment
server.setRequestHandler(GetPromptRequestSchema, async (request) => {
  if (request.params.name === 'totalreclaw_instructions') {
    return {
      messages: [{
        role: 'assistant',
        content: { type: 'text', text: SYSTEM_PROMPT_FRAGMENT }
      }]
    };
  }
});

// Start server
const transport = new StdioServerTransport();
await server.connect(transport);
```

### Environment Variables

```bash
# Required
TOTALRECLAW_SERVER_URL=http://localhost:8080
TOTALRECLAW_MASTER_PASSWORD=<user's master password>

# Optional
TOTALRECLAW_NAMESPACE=default
TOTALRECLAW_DEFAULT_IMPORTANCE=5
```

---

## Integration Examples

### Claude Desktop

```json
// claude_desktop_config.json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_SERVER_URL": "http://localhost:8080",
        "TOTALRECLAW_MASTER_PASSWORD": "${TOTALRECLAW_PASSWORD}"
      }
    }
  }
}
```

### NanoClaw

See: `TS: TotalReclaw Skill for NanoClaw.md` for NanoClaw-specific integration using this MCP server.

---

## Security Considerations

1. **Master Password**: Never logged, passed via environment or secure prompt
2. **Memory Isolation**: Each namespace is cryptographically isolated
3. **No Server Knowledge**: Server never sees plaintext facts
4. **Transport**: Use TLS for server communication

---

## Deliverables

```
@mcp/
├── totalreclaw-mcp-server/
│   ├── package.json
│   ├── src/
│   │   ├── index.ts           # MCP server entry
│   │   ├── tools/
│   │   │   ├── remember.ts
│   │   │   ├── recall.ts
│   │   │   ├── forget.ts
│   │   │   ├── export.ts
│   │   │   └── import.ts
│   │   ├── prompts/
│   │   │   └── instructions.ts # System prompt fragment
│   │   └── client/
│   │       └── index.ts        # TotalReclaw client wrapper
│   └── README.md
└── README.md                   # Overview
```

---

## References

- [MCP Specification](https://modelcontextprotocol.io/)
- [TotalReclaw Client Library](/client/)
- [NanoClaw Integration Spec](../nanoclaw/TS: TotalReclaw Skill for NanoClaw.md)
