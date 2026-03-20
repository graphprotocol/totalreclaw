<!--
Product: TotalReclaw
Formerly: tech specs/v0.3 (grok)/TS v0.3.1: Server-side PoC (with Auth).md
Version: 0.3.1b
Last updated: 2026-02-24
-->

# Technical Specification: TotalReclaw Server PoC v0.3.1

**Version:** 0.3.1b (Auth + Content Fingerprint Dedup)
**Date:** February 24, 2026 (updated from February 21, 2026)
**Supersedes:** TS v0.3 Server-side PoC (no Subgraph).md
**Status:** Ready for Implementation
**See also:** TS v0.3.2 (Multi-Agent Conflict Resolution) for advanced conflict resolution layers beyond what this spec covers.

---

## Changelog from v0.3

| Change | Section |
|--------|---------|
| Added authentication system using master password derivation | §5 |
| Added rate limiting design (deferred to post-PoC) | §6 |
| Clarified API error codes | §4 |
| Added conflict resolution strategy | §8 |
| Added security considerations | §10 |
| Added deferred MVP items (LSH re-index, conflict enhancement) | §14 |
| **Added content fingerprint dedup (v0.3.1b)** | **§3, §7, §8** |
| **Added DUPLICATE_CONTENT error code (v0.3.1b)** | **§3** |
| **Added sync endpoint for delta reconciliation (v0.3.1b)** | **§4, §7** |
| **Referenced v0.3.2 for advanced conflict resolution (v0.3.1b)** | **§14** |

---

## 1. Goals & Non-Goals

### Goals
- Simple, single-binary server for PoC testing
- Server-blind: server never sees plaintext or master password
- Authentication derived from user's master password (no separate API keys)
- Protobuf API (future-proof for decentralized migration)
- PostgreSQL backend with event-sourced storage

### Non-Goals (for PoC)
- No subgraphs, no ERC-4337, no paymaster
- No production-scale deployment (single Postgres)
- Rate limiting (deferred)
- Multi-tenant isolation (single user per instance for PoC)

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    CLIENT (OpenClaw Skill)                       │
├─────────────────────────────────────────────────────────────────┤
│  master_password                                                 │
│       │                                                          │
│       ├──► HKDF(pw, salt, "auth") ──► auth_key ──► Auth Header  │
│       │                                                          │
│       └──► HKDF(pw, salt, "enc") ──► encryption_key             │
│                   │                                              │
│                   ▼                                              │
│            AES-256-GCM Encrypt                                   │
│                   │                                              │
│                   ▼                                              │
│            Protobuf Request                                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ HTTP/Protobuf
┌─────────────────────────────────────────────────────────────────┐
│                      SERVER (PoC)                                │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │ Auth Check  │───►│ Rate Limit  │───►│ Request Handler     │  │
│  │ (SHA256)    │    │ (deferred)  │    │                     │  │
│  └─────────────┘    └─────────────┘    └─────────────────────┘  │
│                                                  │               │
│                                                  ▼               │
│                                          ┌─────────────┐         │
│                                          │ PostgreSQL  │         │
│                                          │ • raw_events│         │
│                                          │ • facts     │         │
│                                          │ • users     │         │
│                                          └─────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Protobuf Schema

```proto
syntax = "proto3";
package totalreclaw;

message TotalReclawFact {
  string id = 1;                    // UUIDv7 (time-sortable)
  string timestamp = 2;             // ISO 8601
  string owner = 3;                 // user_id from auth
  string encrypted_blob = 4;        // Base64 of AES-256-GCM (doc + embedding + metadata)
  repeated string blind_indices = 5; // SHA-256(token) + SHA-256(LSH bucket)
  float decay_score = 6;
  bool is_active = 7;
  int32 version = 8;
  string source = 9;                // conversation | pre_compaction | explicit | etc.
  // --- Added in v0.3.1b ---
  string content_fp = 10;           // HMAC-SHA256 content fingerprint (for dedup, see §8.2)
  string agent_id = 11;             // identifier of the agent that created this fact
}

message StoreRequest {
  repeated TotalReclawFact facts = 1;
}

message StoreResponse {
  bool success = 1;
  repeated string ids = 2;
  ErrorCode error_code = 3;
  string error_message = 4;
  // --- Added in v0.3.1b ---
  repeated string duplicate_ids = 5;  // fact IDs that were rejected as duplicates
}

message SearchRequest {
  repeated string blind_trapdoors = 1;   // LSH + keyword trapdoors from client
  int32 limit = 2;                       // default 500
  float min_decay_score = 3;             // default 0.3
}

message SearchResponse {
  repeated TotalReclawFact facts = 1;     // encrypted, client decrypts & reranks
  int32 total_available = 2;
  ErrorCode error_code = 3;
  string error_message = 4;
}

// --- Added in v0.3.1b: Delta sync for agent reconnection ---
message SyncRequest {
  int64 since_sequence = 1;              // agent's last known sequence_id
  int32 limit = 2;                       // max facts to return (default 1000)
}

message SyncResponse {
  repeated TotalReclawFact facts = 1;
  int64 latest_sequence = 2;             // current highest sequence_id for this user
  bool has_more = 3;                     // true if more facts beyond limit
}

enum ErrorCode {
  OK = 0;
  INVALID_REQUEST = 1;
  UNAUTHORIZED = 2;
  RATE_LIMITED = 3;
  NOT_FOUND = 4;
  INTERNAL_ERROR = 5;
  VERSION_CONFLICT = 6;
  DUPLICATE_CONTENT = 7;                 // Added in v0.3.1b: content fingerprint match
}
```

---

## 4. API Endpoints

### Base URL
```
http://localhost:8080  (PoC only, never expose to internet)
```

### Endpoints

| Method | Path | Request | Response | Description |
|--------|------|---------|----------|-------------|
| POST | /register | RegisterRequest | RegisterResponse | One-time user registration |
| POST | /store | StoreRequest | StoreResponse | Store new facts (with dedup) |
| POST | /search | SearchRequest | SearchResponse | Blind-index search |
| POST | /update | StoreRequest | StoreResponse | Update/decay facts |
| DELETE | /facts/{id} | - | { success: bool } | Soft delete (tombstone) |
| GET | /health | - | { status: "ok" } | Health check |
| GET | /export | - | ExportResponse | Export all user data |
| GET | /sync | SyncRequest | SyncResponse | Delta sync since sequence (v0.3.1b) |

### New: Registration

```proto
message RegisterRequest {
  string auth_key_hash = 1;    // SHA256(HKDF(master_password, salt, "auth"))
  bytes salt = 2;              // 32 random bytes
}

message RegisterResponse {
  bool success = 1;
  string user_id = 2;          // Server-generated UUID
  ErrorCode error_code = 3;
  string error_message = 4;
}
```

### Authentication Header

All requests except `/register` and `/health` require:
```
Authorization: Bearer <auth_key>
```

Where `auth_key = HKDF(master_password, salt, "auth")` (derived client-side).

Server validates: `SHA256(auth_key) == stored_auth_key_hash`

---

## 5. Authentication System

### Design Principles
1. **No separate API key** - master password IS the auth credential
2. **Server-blind** - server never sees master password
3. **Cryptographic separation** - auth key ≠ encryption key
4. **Stateless requests** - no sessions, no tokens to refresh
5. **Portable** - same master password works on any device

### Registration Flow

```
┌──────────────────────────────────────────────────────────────┐
│ CLIENT                                                        │
├──────────────────────────────────────────────────────────────┤
│ 1. User enters master_password                               │
│ 2. Generate: salt = random(32 bytes)                         │
│ 3. Derive: auth_key = HKDF-SHA256(                           │
│                master_password,                              │
│                salt,                                         │
│                "totalreclaw-auth-v1",                         │
│                length=32                                     │
│            )                                                 │
│ 4. Compute: auth_key_hash = SHA256(auth_key)                 │
│ 5. Send: POST /register { auth_key_hash, salt }              │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ SERVER                                                        │
├──────────────────────────────────────────────────────────────┤
│ 1. Generate: user_id = UUIDv7()                              │
│ 2. Store: INSERT INTO users (user_id, auth_key_hash, salt)   │
│ 3. Return: { success: true, user_id }                        │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ CLIENT (Post-Registration)                                    │
├──────────────────────────────────────────────────────────────┤
│ Store locally in OS keychain:                                 │
│   • user_id                                                   │
│   • salt (for future auth_key derivation)                     │
│                                                               │
│ User remembers: master_password                               │
└──────────────────────────────────────────────────────────────┘
```

### Request Authentication

```
┌──────────────────────────────────────────────────────────────┐
│ CLIENT (Every Request)                                        │
├──────────────────────────────────────────────────────────────┤
│ 1. User enters master_password (or from OS keychain)         │
│ 2. Retrieve: salt from local storage                         │
│ 3. Derive: auth_key = HKDF-SHA256(master_password, salt, ...)│
│ 4. Send request with header:                                 │
│      Authorization: Bearer <auth_key>                        │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│ SERVER                                                        │
├──────────────────────────────────────────────────────────────┤
│ 1. Extract: auth_key from Authorization header               │
│ 2. Compute: auth_key_hash = SHA256(auth_key)                 │
│ 3. Lookup: SELECT * FROM users WHERE auth_key_hash = ?       │
│ 4. If found: use user_id for all operations                  │
│ 5. If not found: return 401 Unauthorized                     │
└──────────────────────────────────────────────────────────────┘
```

### Key Separation

```
master_password
       │
       ├──► HKDF(pw, salt, "totalreclaw-auth-v1")  ──► auth_key (for server auth)
       │
       └──► HKDF(pw, salt, "totalreclaw-enc-v1")   ──► encryption_key (for AES-GCM)
```

**Critical**: Server only ever sees `auth_key` (and stores `SHA256(auth_key)`). Server never has enough information to derive `encryption_key`.

---

## 6. Rate Limiting (Deferred)

Design for future implementation:

| Endpoint | Limit | Window |
|----------|-------|--------|
| /register | 5 | per hour per IP |
| /store | 100 | per minute per user |
| /search | 200 | per minute per user |
| /export | 5 | per hour per user |

Implementation: Use Redis with sliding window or Postgres-based rate limiter.

---

## 7. Database Schema

```sql
-- Users table (authentication)
CREATE TABLE users (
  user_id TEXT PRIMARY KEY,           -- UUIDv7
  auth_key_hash BYTEA NOT NULL,       -- SHA256(auth_key)
  salt BYTEA NOT NULL,                -- 32 bytes
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ
);

CREATE INDEX idx_users_auth_hash ON users(auth_key_hash);

-- Raw events (immutable log)
CREATE TABLE raw_events (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  event_bytes BYTEA NOT NULL,         -- raw Protobuf of StoreRequest
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_events_user ON raw_events(user_id, created_at DESC);

-- Facts (mutable view)
CREATE TABLE facts (
  id TEXT PRIMARY KEY,                -- fact UUIDv7
  user_id TEXT NOT NULL REFERENCES users(user_id),
  encrypted_blob BYTEA NOT NULL,
  blind_indices TEXT[] NOT NULL,
  decay_score FLOAT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  version INT NOT NULL DEFAULT 1,
  source TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  -- Added in v0.3.1b: content fingerprint dedup + sync
  sequence_id BIGSERIAL,              -- monotonic per-user, for delta sync
  content_fp TEXT,                    -- HMAC-SHA256 fingerprint for exact dedup
  agent_id TEXT                       -- which agent created this fact
);

CREATE INDEX idx_facts_user ON facts(user_id);
CREATE INDEX idx_facts_blind_gin ON facts USING GIN(blind_indices);
CREATE INDEX idx_facts_active_decay ON facts(user_id, is_active, decay_score DESC);
-- Added in v0.3.1b:
CREATE UNIQUE INDEX idx_facts_user_fp ON facts(user_id, content_fp) WHERE is_active = true;
CREATE INDEX idx_facts_user_seq ON facts(user_id, sequence_id);

-- Tombstones (for soft delete, 30-day retention)
CREATE TABLE tombstones (
  fact_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  deleted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tombstones_expiry ON tombstones(deleted_at);
```

---

## 8. Conflict Resolution

### 8.1 Version-Based Optimistic Locking (Unchanged)

For updates to **existing facts** (same fact ID):

```sql
-- On update, include version check
UPDATE facts
SET encrypted_blob = ?, version = version + 1, updated_at = NOW()
WHERE id = ? AND user_id = ? AND version = ?;

-- If affected_rows == 0, return VERSION_CONFLICT error
```

**Client strategy on VERSION_CONFLICT:**
1. Re-retrieve latest version
2. Merge changes (LLM-assisted merge for text)
3. Retry update with new version

### 8.2 Content Fingerprint Dedup (Added v0.3.1b)

For **new facts** that may duplicate existing content (e.g., after agent restart, or when
multiple agents extract from the same source material):

#### Content Fingerprint Computation (Client-Side)

```
dedup_key   = HKDF-SHA256(master_password, salt, "totalreclaw-dedup-v1")
content_fp  = HMAC-SHA256(dedup_key, normalize(plaintext))
```

**`normalize(text)` function:**
1. Unicode NFC normalization
2. Lowercase
3. Collapse whitespace (multiple spaces/tabs/newlines to single space)
4. Trim leading/trailing whitespace
5. UTF-8 encode

**Key derivation note:** The dedup key is derived via a separate HKDF context string
(`"totalreclaw-dedup-v1"`) so that it survives encryption key rotation independently.
It follows the same pattern as auth/encryption key separation (§5).

```
master_password
       │
       ├──► HKDF(pw, salt, "totalreclaw-auth-v1")   ──► auth_key
       ├──► HKDF(pw, salt, "totalreclaw-enc-v1")    ──► encryption_key
       └──► HKDF(pw, salt, "totalreclaw-dedup-v1")  ──► dedup_key  (NEW)
```

**Extraction temperature requirement:** For content fingerprint dedup to be effective, the
LLM fact extraction call MUST use `temperature=0` (or the lowest available setting). This
maximizes output determinism: the same input text produces the same extracted fact text
across agents and sessions. Without this, minor wording variations in extraction output
defeat the fingerprint. Note that `temperature=0` is not guaranteed to be fully deterministic
by all providers, but achieves >95% consistency on short structured extractions — sufficient
for dedup where occasional misses are a minor storage cost, not a correctness failure.

**Platform-specific implementation:**

- **OpenClaw:** Use the built-in `llm-task` plugin tool. The skill instructs the agent to
  invoke `llm-task` with `temperature: 0`, the fact extraction prompt, and a JSON schema
  for structured output. This runs a separate LLM call that does NOT affect the main
  conversation's temperature. The `llm-task` tool is JSON-only, no tools exposed — ideal
  for structured extraction. Must be enabled in OpenClaw config:
  ```json
  { "plugins": { "entries": { "llm-task": { "enabled": true } } } }
  ```

- **NanoClaw:** The Claude Agent SDK does not currently expose `temperature` as a parameter
  (open issue: anthropics/claude-agent-sdk-python#273, 20+ upvotes). Do NOT implement a
  workaround that bypasses the Agent SDK. Revisit when the Agent SDK adds native temperature
  support — expected to land given community demand. Until then, NanoClaw extraction uses
  the SDK default temperature, which reduces fingerprint dedup effectiveness but does not
  break it (duplicates that slip through are a storage cost, not a correctness issue).

**Privacy:** The server sees the fingerprint but cannot reverse it without the dedup key.
The fingerprint reveals only one bit per fact pair: "same content or not." This is strictly
less information than blind indices already reveal (approximate similarity). See TS v0.3.2
§9 for full privacy analysis.

#### Server-Side Dedup Check (in `/store` handler)

```
POST /store receives StoreRequest with facts[]:

FOR EACH fact in request:
  1. Check: SELECT id FROM facts
            WHERE user_id = $uid AND content_fp = $fp AND is_active = true;

  2. IF found:
       Skip this fact. Add existing fact ID to response.duplicate_ids[].
       Do NOT return an error — continue processing remaining facts.

  3. IF not found:
       Insert fact normally. Assign sequence_id. Add to response.ids[].

Return StoreResponse with:
  success = true (partial success is still success)
  ids = [newly stored fact IDs]
  duplicate_ids = [skipped fact IDs that already existed]
```

**Behavior:** Duplicates are silently skipped, not hard-rejected. The client knows which
facts were skipped via `duplicate_ids` and can proceed without retry logic. This makes the
store operation **idempotent** — pushing the same facts twice is safe.

#### Sync Endpoint (for Agent Reconnection)

```
GET /sync?since_sequence={seq}&limit=1000

Returns all facts for the authenticated user with sequence_id > since_sequence.
Used by agents after coming online to pull changes made by other agents.

Response: SyncResponse {
  facts: [...],           // facts since the given sequence
  latest_sequence: 4721,  // current highest sequence_id
  has_more: false         // true if more facts beyond limit (paginate)
}
```

The agent stores `latest_sequence` locally and uses it on the next reconnection.

#### Client Reconnection Protocol

```
1. Agent comes online.
2. GET /sync?since_sequence={last_known_sequence}
3. Build set of server fingerprints: { content_fp → fact_id }
4. For each local pending fact:
   a. IF content_fp already in server set → skip (already stored)
   b. ELSE → POST /store (server will also check, but pre-filtering avoids round trips)
5. Update local last_known_sequence.
```

> **For advanced conflict resolution** (semantic near-duplicates, contradictions, LLM-assisted
> merge), see TS v0.3.2: Multi-Agent Conflict Resolution.

---

## 9. Search Implementation

### Query Flow
```sql
-- Blind index lookup using GIN array contains operator
SELECT id, encrypted_blob, decay_score
FROM facts
WHERE user_id = ?
  AND is_active = true
  AND decay_score >= ?
  AND blind_indices && ARRAY[?]::text[]  -- GIN index
ORDER BY decay_score DESC
LIMIT ?;
```

### Performance Target
- <50ms for 100K facts with GIN index
- Client-side reranking handles the rest

---

## 10. Security Considerations

### Server-Side Protections

| Threat | Mitigation |
|--------|------------|
| Brute force auth | Rate limiting (post-PoC) |
| DB leak | auth_key_hash + salt only, no passwords |
| Replay attacks | Include timestamp in future versions |
| MITM | HTTPS in production (localhost for PoC) |

### Client Responsibilities

| Threat | Mitigation |
|--------|------------|
| Weak password | Enforce minimum entropy check |
| Password reuse | Warn user (can't detect server-side) |
| Key exfiltration | OS keychain storage |

### E2EE Guarantee
- Server stores: `auth_key_hash`, `salt`, `encrypted_blob`, `blind_indices`
- Server NEVER sees: `master_password`, `encryption_key`, `plaintext`

---

## 11. Implementation Order

| Day | Tasks |
|-----|-------|
| 1 | Protobuf schema + codegen, DB schema + migrations |
| 2 | /register, /health endpoints, auth middleware |
| 3 | /store, /search endpoints with GIN queries |
| 4 | /update, /delete, version conflict handling |
| 5 | Integration tests, Docker Compose |

---

## 12. Deliverables

```
server/
├── proto/
│   └── totalreclaw.proto
├── src/
│   ├── main.py (or index.ts)
│   ├── auth.py
│   ├── handlers/
│   │   ├── register.py
│   │   ├── store.py
│   │   └── search.py
│   └── db/
│       ├── schema.sql
│       └── queries.py
├── tests/
│   ├── test_auth.py
│   ├── test_store.py
│   └── test_search.py
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 13. Docker Compose (PoC)

```yaml
version: '3.9'
services:
  totalreclaw-server:
    build: .
    container_name: totalreclaw-poc
    environment:
      DATABASE_URL: postgresql://totalreclaw:dev@postgres:5432/totalreclaw
    ports:
      - "127.0.0.1:8080:8080"  # localhost ONLY
    depends_on:
      - postgres
    restart: unless-stopped

  postgres:
    image: postgres:16
    container_name: totalreclaw-db
    environment:
      POSTGRES_USER: totalreclaw
      POSTGRES_PASSWORD: dev  # Change in production
      POSTGRES_DB: totalreclaw
    volumes:
      - totalreclaw-data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"  # localhost ONLY
    restart: unless-stopped

volumes:
  totalreclaw-data:
```

**Security Notes:**
- All ports bound to 127.0.0.1 (localhost only)
- Default password MUST be changed for any non-PoC use
- No external network access required

---

## 14. Deferred to MVP Phase

These items are **out of scope for PoC** but **required before MVP launch**.

### 14.1 LSH Runtime Re-indexing

**Reference:** `TS v0.3: E2EE with LSH + Blind Buckets.md` §530-587

When LSH parameters (`n_bits`, `n_tables`) need to change (rare, only at 500K+ corpus size):

| Aspect | Detail |
|--------|--------|
| **Trigger** | Admin `POST /admin/lsh-reindex` with new params |
| **Downtime** | Per-user, not global |
| **Client requirement** | Must have master password (cannot re-index server-side) |
| **Process** | Client-side: decrypt → recompute LSH → re-encrypt → re-upload |
| **APIs needed** | `GET /lsh-config`, `POST /admin/lsh-reindex`, `GET /lsh-reindex/status` |

**Note:** `candidate_pool` is dynamic and auto-adjusts based on corpus size (no re-index needed).

### 14.2 Conflict Resolution Enhancement

**Current state (§8):** Optimistic locking + content fingerprint dedup (v0.3.1b)

**What v0.3.1b covers:**
- Exact duplicate prevention via content fingerprint (HMAC-SHA256)
- Delta sync via sequence_id watermark
- Idempotent store operations (safe to retry)
- Agent crash recovery (re-push is safe)

**What remains for post-MVP (see TS v0.3.2 for full specification):**

| Gap | Description | Priority | TS v0.3.2 Layer |
|-----|-------------|----------|-----------------|
| Semantic near-duplicate detection | "prefers Python" vs "likes Python over JS" | MEDIUM | Layer 3 (blind index overlap) |
| Stale contradiction resolution | "lives in Lisbon" vs "moved to Berlin" | MEDIUM | Layer 4 (client reconciliation) |
| LLM-assisted merge | Prompt, model, fallback for merging conflicting facts | MEDIUM | Layer 4 |
| Import conflicts | How to handle duplicates during bulk import | LOW | Layer 1 (fingerprint covers exact) |
| Namespace collisions | Same namespace on different servers | LOW | Out of scope |

**Recommended phasing:**
1. **MVP (this spec):** Content fingerprint dedup + sync endpoint — catches ~70% of duplicates
2. **Post-MVP:** Blind index overlap detection (TS v0.3.2 Layer 3) — catches semantic near-dupes
3. **Post-MVP:** Client-side LLM merge (TS v0.3.2 Layer 4) — handles contradictions

### 14.3 MVP Checklist

Before launching MVP:

- [ ] Implement `POST /admin/lsh-reindex` endpoint
- [ ] Implement `GET /lsh-reindex/status` endpoint
- [ ] Add per-user `lsh_config` table
- [x] Content fingerprint dedup in `/store` handler (v0.3.1b §8.2)
- [x] `GET /sync` endpoint for delta reconciliation (v0.3.1b §8.2)
- [x] `DUPLICATE_CONTENT` error code (v0.3.1b §3)
- [ ] Client-side sync protocol implementation
- [ ] Test multi-agent dedup scenarios (crash recovery, shared context)
