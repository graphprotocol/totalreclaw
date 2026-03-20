# NanoClaw Memory System

> **Comprehensive documentation of how NanoClaw handles memory, storage, and session continuity.**

---

## Table of Contents

1. [Overview](#overview)
2. [Memory Architecture](#memory-architecture)
3. [Storage Systems](#storage-systems)
4. [Session Continuity](#session-continuity)
5. [Memory Hierarchy](#memory-hierarchy)
6. [Comparison with OpenClaw](#comparison-with-openclaw)
7. [Integration Points for TotalReclaw](#integration-points-for-totalreclaw)

---

## Overview

NanoClaw is a lightweight personal Claude assistant that runs agents in isolated Linux containers. Unlike OpenClaw's complex multi-process architecture, NanoClaw uses a simple file-based memory system combined with SQLite for structured data.

### Key Characteristics

- **Single Node.js process** - No microservices, no message queues
- **Container isolation** - Agents run in Docker/Apple Container with explicit mounts
- **File-based memory** - CLAUDE.md files for persistent context
- **SQLite for state** - Messages, sessions, tasks stored in database
- **Per-group isolation** - Each WhatsApp group has its own memory namespace

---

## Memory Architecture

```
+-------------------+
|   Host Machine    |
+-------------------+
        |
        |  mount
        v
+-------------------+     +-------------------+
|  Container (per   |     |  Global Memory    |
|  group invocation)|     |  (read-only for  |
|                   |     |   non-main)       |
|  /workspace/group |     |  /workspace/global|
|  CLAUDE.md        |<----|  CLAUDE.md        |
|  conversations/   |     +-------------------+
|  logs/            |
+-------------------+
        |
        |  Claude Agent SDK
        v
+-------------------+
|  Session Storage  |
|  ~/.claude/       |
|  (per-group,      |
|   mounted from    |
|   host)           |
+-------------------+
```

### Memory Layers

1. **CLAUDE.md (Project Memory)** - Markdown files loaded by Claude Code
2. **Session Storage** - Conversation transcripts managed by Claude Agent SDK
3. **SQLite Database** - Messages, groups, tasks, scheduler state
4. **File System** - Arbitrary files created/modified by agents

---

## Storage Systems

### 1. CLAUDE.md Files (Primary Memory)

CLAUDE.md files are the primary mechanism for persistent memory in NanoClaw. They are automatically loaded by Claude Code when the agent starts.

#### Location Hierarchy

```
nanoclaw/
+-- CLAUDE.md              # Root memory (instructions for AI)
+-- groups/
|   +-- main/
|   |   +-- CLAUDE.md      # Main channel memory (admin)
|   |   +-- conversations/ # Archived conversation transcripts
|   |   +-- logs/          # Container execution logs
|   +-- family/
|   |   +-- CLAUDE.md      # Family group memory
|   +-- work/
|       +-- CLAUDE.md      # Work group memory
+-- groups/global/         # Global memory (shared, read-only)
    +-- CLAUDE.md          # Shared context for all groups
```

#### Memory Inheritance

When an agent runs for a group:

1. **Main group** sees:
   - `/workspace/project/CLAUDE.md` (root, read-only)
   - `/workspace/group/CLAUDE.md` (group-specific, read-write)
   - Can modify global memory via project root access

2. **Non-main groups** see:
   - `/workspace/group/CLAUDE.md` (group-specific, read-write)
   - `/workspace/global/CLAUDE.md` (shared, read-only)
   - Cannot modify global memory

#### CLAUDE.md Content Example

```markdown
# Family Group Memory

## Members
- Alice (mom) - loves gardening
- Bob (dad) - software engineer, uses Emacs
- Charlie (son) - college student, studying CS

## Preferences
- Schedule family dinners on Sundays
- Birthday reminders are important
- Don't discuss politics

## Recent Events
- 2026-02-20: Alice's garden is blooming
- 2026-02-18: Charlie got an A on his algorithms exam
```

### 2. SQLite Database (Structured State)

Location: `store/messages.db`

#### Schema

```sql
-- Chat metadata (all known chats)
CREATE TABLE chats (
  jid TEXT PRIMARY KEY,
  name TEXT,
  last_message_time TEXT,
  channel TEXT,
  is_group INTEGER DEFAULT 0
);

-- Message history (registered groups only)
CREATE TABLE messages (
  id TEXT,
  chat_jid TEXT,
  sender TEXT,
  sender_name TEXT,
  content TEXT,
  timestamp TEXT,
  is_from_me INTEGER,
  is_bot_message INTEGER DEFAULT 0,
  PRIMARY KEY (id, chat_jid)
);

-- Registered groups (activated groups)
CREATE TABLE registered_groups (
  jid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  folder TEXT NOT NULL UNIQUE,
  trigger_pattern TEXT NOT NULL,
  added_at TEXT NOT NULL,
  container_config TEXT,
  requires_trigger INTEGER DEFAULT 1
);

-- Session tracking (per-group session IDs)
CREATE TABLE sessions (
  group_folder TEXT PRIMARY KEY,
  session_id TEXT NOT NULL
);

-- Scheduled tasks
CREATE TABLE scheduled_tasks (
  id TEXT PRIMARY KEY,
  group_folder TEXT NOT NULL,
  chat_jid TEXT NOT NULL,
  prompt TEXT NOT NULL,
  schedule_type TEXT NOT NULL,
  schedule_value TEXT NOT NULL,
  context_mode TEXT DEFAULT 'isolated',
  next_run TEXT,
  last_run TEXT,
  last_result TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT NOT NULL
);

-- Router state (cursors, timestamps)
CREATE TABLE router_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

#### Key Operations

```typescript
// Store incoming message
storeMessage(msg: NewMessage): void

// Get messages since timestamp (for context)
getMessagesSince(chatJid: string, sinceTimestamp: string): NewMessage[]

// Get new messages for all registered groups
getNewMessages(jids: string[], lastTimestamp: string): NewMessage[]

// Session management
getSession(groupFolder: string): string | undefined
setSession(groupFolder: string, sessionId: string): void

// Group registration
getRegisteredGroup(jid: string): RegisteredGroup | undefined
setRegisteredGroup(jid: string, group: RegisteredGroup): void
```

### 3. Session Storage (Claude Agent SDK)

Location: `data/sessions/{group-folder}/.claude/`

Each group has isolated session storage:

```
data/sessions/
+-- main/
|   +-- .claude/
|       +-- settings.json      # Claude Code settings
|       +-- skills/            # Copied from container/skills/
|       +-- transcripts/       # Session transcripts
|       +-- sessions-index.json
+-- family/
|   +-- .claude/
|       +-- ...
+-- work/
    +-- .claude/
        +-- ...
```

#### settings.json Example

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"
  }
}
```

This is mounted into the container at `/home/node/.claude/`.

### 4. Conversation Archives

Location: `groups/{group-folder}/conversations/`

Before context compaction, conversations are archived as Markdown:

```markdown
# Family Dinner Planning

Archived: Feb 22, 2026 at 3:45 PM

---

**User**: @Andy can you help me plan the family dinner for Sunday?

**Andy**: Of course! I'll help you plan the family dinner. Based on what I know:
- Alice prefers vegetarian options
- Bob is flexible but enjoys grilled dishes
- Charlie has been craving Italian food lately

What type of cuisine are you considering?

**User**: Let's do Italian. Can you suggest a menu?

**Andy**: Here's a suggested Italian menu for Sunday's family dinner:
...
```

---

## Session Continuity

### Session Lifecycle

```
1. New Message Arrives
       |
       v
2. Check for Active Container
       |
       +--[Active]--> Pipe message via IPC
       |                    |
       |                    v
       |              Container processes
       |                    |
       +--[No Container]    v
       |              Container starts
       |                    |
       v                    v
3. Load Session ID from SQLite
       |
       v
4. Spawn Container with:
   - sessionId (resume conversation)
   - groupFolder (mount correct memory)
   - prompt (user message)
       |
       v
5. Container runs Claude Agent SDK
   - Loads CLAUDE.md from /workspace/group
   - Loads global CLAUDE.md (if non-main)
   - Resumes session if sessionId provided
       |
       v
6. Stream output back to host
   - Session ID returned for persistence
   - Results streamed as they arrive
       |
       v
7. Update SQLite with new session ID
```

### Session Management Code

```typescript
// In container-runner.ts
export async function runContainerAgent(
  group: RegisteredGroup,
  input: ContainerInput,
  onProcess: (proc: ChildProcess, containerName: string) => void,
  onOutput?: (output: ContainerOutput) => Promise<void>,
): Promise<ContainerOutput>

// ContainerInput includes sessionId for continuity
interface ContainerInput {
  prompt: string;
  sessionId?: string;        // Resume existing session
  groupFolder: string;
  chatJid: string;
  isMain: boolean;
  isScheduledTask?: boolean;
  secrets?: Record<string, string>;
}
```

### Idle Timeout

Containers stay alive for `IDLE_TIMEOUT` (default: 30 minutes) after the last result. This allows:

1. **Fast follow-up responses** - No cold start
2. **Context preservation** - Session remains active
3. **IPC piping** - New messages piped to running container

```typescript
// In src/index.ts
idleTimer = setTimeout(() => {
  logger.debug({ group: group.name }, 'Idle timeout, closing container stdin');
  queue.closeStdin(chatJid);
}, IDLE_TIMEOUT);
```

---

## Memory Hierarchy

### What Gets Remembered Where

| Data Type | Storage | Scope | Persistence |
|-----------|---------|-------|-------------|
| User preferences | CLAUDE.md | Per-group | Manual/LLM edit |
| Global preferences | groups/global/CLAUDE.md | All groups | Manual (main only) |
| Conversation history | Session transcripts | Per-group | Auto (until compact) |
| Archived conversations | conversations/*.md | Per-group | Auto (on compact) |
| Message history | SQLite | Per-group | Auto |
| Group metadata | SQLite | Global | Auto |
| Scheduled tasks | SQLite | Per-group | Auto |
| Agent settings | data/sessions/.claude/ | Per-group | Manual |

### Memory Update Flow

```
User Message --> Agent Processing --> Response
                      |
                      v
              [Periodic Extraction]
                      |
                      +-- High importance --> Update CLAUDE.md
                      |
                      +-- Pre-compaction --> Archive to conversations/
                      |
                      +-- Low importance --> Stored in session only
```

---

## Comparison with OpenClaw

### Architecture Comparison

| Aspect | NanoClaw | OpenClaw |
|--------|----------|----------|
| Processes | 1 Node.js | 4-5 different processes |
| Configuration | Minimal (env vars) | 8+ config files |
| Dependencies | ~10 | 45+ |
| Channels | 1-2 (pluggable) | 15+ abstractions |
| Security | Container isolation | Application-level ACLs |
| Memory | CLAUDE.md + SQLite | QMD (proprietary) |
| Code size | ~2000 lines | 52+ modules |

### Memory Comparison

| Feature | NanoClaw | OpenClaw |
|---------|----------|----------|
| Primary storage | CLAUDE.md files | QMD (queryable memory DB) |
| Semantic search | No | Yes (vector search) |
| Fact extraction | Manual/LLM-driven | Automatic (Mem0-style) |
| Cross-group sharing | Read-only global | Configurable |
| Export | Plain text (MD) | Proprietary format |
| Memory decay | Manual | Automatic scoring |

### Security Comparison

| Aspect | NanoClaw | OpenClaw |
|--------|----------|----------|
| Isolation | OS-level (containers) | Application-level |
| Attack surface | Explicit mounts | Shared memory space |
| Credential exposure | Filtered env vars | Potentially accessible |
| Cross-group access | Prevented by mount | Prevented by ACLs |

---

## Integration Points for TotalReclaw

### Where TotalReclaw Can Add Value

1. **Semantic Search** - NanoClaw lacks vector search; TotalReclaw can provide encrypted semantic retrieval
2. **Fact Extraction** - Automated extraction and deduplication from conversations
3. **Memory Decay** - Importance-based forgetting instead of manual curation
4. **Cross-Device Sync** - Encrypted memory that syncs across instances
5. **End-to-End Encrypted** - Server never sees plaintext memories

### Integration Opportunities

```
+-------------------+
|   NanoClaw Host   |
+-------------------+
        |
        |  MCP / Skill
        v
+-------------------+
|  TotalReclaw       |
|  (in container    |
|   or as MCP       |
|   server)         |
+-------------------+
        |
        |  Protobuf/HTTP
        v
+-------------------+
| TotalReclaw Server |
| (encrypted store) |
+-------------------+
```

### Key Files to Modify/Extend

| File | Purpose | Integration Point |
|------|---------|-------------------|
| `container/agent-runner/src/index.ts` | Agent execution | Add TotalReclaw MCP server |
| `src/db.ts` | SQLite operations | Sync with TotalReclaw |
| `src/container-runner.ts` | Container config | Mount TotalReclaw config |
| `groups/*/CLAUDE.md` | Memory files | Sync with TotalReclaw |

### Data Flow for TotalReclaw Integration

```
1. Message received
       |
       v
2. TotalReclaw recall (pre-agent hook)
   - Query encrypted memories
   - Inject into context
       |
       v
3. Agent processes with memory context
       |
       v
4. TotalReclaw remember (post-agent hook)
   - Extract facts from conversation
   - Encrypt and store
       |
       v
5. Pre-compaction: Full memory flush
   - Extract all remaining facts
   - Sync to server
```

---

## Appendix: File Paths Reference

### Host Machine

```
nanoclaw/
+-- store/
|   +-- messages.db          # SQLite database
|   +-- auth/                # WhatsApp auth state
+-- data/
|   +-- sessions/            # Per-group Claude sessions
|   |   +-- {group}/
|   |       +-- .claude/
|   +-- ipc/                 # Per-group IPC directories
|       +-- {group}/
|           +-- messages/
|           +-- tasks/
|           +-- input/
+-- groups/
|   +-- main/
|   |   +-- CLAUDE.md
|   |   +-- conversations/
|   |   +-- logs/
|   +-- {other-groups}/
|   +-- global/
|       +-- CLAUDE.md
+-- container/
    +-- agent-runner/        # Container code
    +-- skills/              # Skills copied to sessions
```

### Inside Container

```
/workspace/
+-- group/                   # Group-specific files (rw)
|   +-- CLAUDE.md
|   +-- conversations/
+-- global/                  # Global memory (ro for non-main)
|   +-- CLAUDE.md
+-- project/                 # Project root (ro, main only)
+-- ipc/
|   +-- messages/
|   +-- tasks/
|   +-- input/               # Follow-up messages
+-- extra/                   # Additional mounts
/home/node/.claude/          # Session storage (mounted)
```

---

## References

- [NanoClaw README](https://github.com/qwibitai/nanoclaw)
- [NanoClaw Security Model](https://github.com/qwibitai/nanoclaw/blob/main/docs/SECURITY.md)
- [Claude Agent SDK Documentation](https://docs.anthropic.com)
- [Claude Code Memory](https://code.claude.com/docs/en/memory)
