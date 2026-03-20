# TotalReclaw End-to-End Flow

> Complete reference for the TotalReclaw end-to-end encrypted memory vault.
> Covers every API endpoint, the cryptographic pipeline, blind search, delta sync,
> content dedup, and authentication -- with request/response examples.

---

## 1. Overview

TotalReclaw is an end-to-end encrypted memory vault for AI agents. It allows
AI agents (OpenClaw, Claude Desktop, any MCP-compatible client) to persist and
retrieve structured "facts" without the server ever seeing plaintext data.

**Core guarantees:**

- **End-to-end encrypted** -- the server stores only encrypted blobs and one-way blind
  indices. It never sees the master password, encryption key, or plaintext facts.
- **Portable** -- one-click plain-text export with cursor-based pagination. No
  vendor lock-in.
- **Universal** -- any MCP-compatible agent can connect via the standard API.

**Key constraints:**

| Metric | Target |
|--------|--------|
| Search latency | <140 ms p95 (1 M memories) |
| Recall | >=93% of true top-250 |
| Storage overhead | <=2.2x vs plaintext |

---

## 2. API Flow -- Happy Path

The typical lifecycle of a user and their memories:

```
Register -> Store -> Search -> Export -> Sync -> Delete Fact -> Delete Account
```

All versioned endpoints live under the `/v1/` prefix. Infrastructure endpoints
(`/health`, `/ready`, `/metrics`) live at the root.

---

## 3. Authentication

### 3.1 Key Derivation (Client-Side)

```
master_password  --->  Argon2id  --->  ikm (input key material)
                                          |
                              HKDF-SHA256(salt, ikm, info="totalreclaw-auth-v1")
                                          |
                           +--------------+--------------+
                           |                             |
                       authKey (32 bytes)         encryptionKey (32 bytes)
                           |                        (separate HKDF expand)
                    SHA256(authKey)
                           |
                    authKeyHash (32 bytes)  -- sent to server at registration
```

- `salt`: 32 cryptographically random bytes, generated once at registration,
  stored on both client (OS keychain) and server.
- The server **never** receives the master password or the encryption key.
- The HKDF info string is `"totalreclaw-auth-v1"` (see `server/src/auth.py`).

### 3.2 Registration

Client sends `SHA256(authKey)` and `salt` to the server. The server stores them
and returns a `user_id`.

### 3.3 Subsequent Requests

Every authenticated request includes:

```
Authorization: Bearer <authKey.hex()>
```

The token is exactly **64 hex characters** (32 bytes). The server:

1. Extracts the hex token from the `Authorization: Bearer` header.
2. Validates length is exactly 64 hex chars (rejects anything else immediately
   to prevent DoS from oversized tokens).
3. Decodes hex to bytes.
4. Computes `SHA256(authKey)`.
5. Looks up the user row where `auth_key_hash` matches AND `is_deleted = false`.
6. Uses `hmac.compare_digest` (constant-time comparison) to prevent timing
   attacks.
7. Updates `last_seen_at` for the authenticated user.
8. Returns the `user_id` to the request handler.

If any step fails, the server returns HTTP 401.

---

## 4. Crypto Flow

### 4.1 Encrypting a Fact (Client-Side)

1. **Extract facts** from the conversation using the LLM.
2. **Encrypt** each fact's plaintext with AES-256-GCM using the `encryptionKey`.
   The resulting ciphertext is the `encrypted_blob` (binary, sent as hex).
3. **Generate blind indices** for searchability:
   - Tokenize the plaintext into words.
   - For each token, compute LSH (Locality-Sensitive Hashing) buckets.
   - For each bucket, compute `SHA256(bucket_value)`.
   - The resulting array of hex digests is the `blind_indices` list.
4. **Compute content fingerprint** (v0.3.1b): `HMAC-SHA256(encryptionKey, fact_plaintext)`.
   This is the `content_fp` field, used for server-side dedup without revealing content.
5. **Assign metadata**: `id` (UUIDv7), `timestamp` (ISO 8601), `decay_score`,
   `source`, `agent_id`.

### 4.2 Decrypting (Client-Side)

The server returns `encrypted_blob` as hex. The client:

1. Decodes hex to bytes.
2. Decrypts with AES-256-GCM using the `encryptionKey`.
3. Gets the original plaintext fact.

The server never performs decryption.

---

## 5. Endpoint Reference

### 5.1 Health Check

```
GET /health
```

No authentication required.

**Response:**

```json
{
  "status": "healthy",
  "version": "0.3.1",
  "database": "connected"
}
```

Status is `"healthy"` if the database is connected, `"degraded"` otherwise.

---

### 5.2 Readiness Check

```
GET /ready
```

No authentication required. Returns 200 only if the database is connected.

**Response:**

```json
{
  "ready": true
}
```

---

### 5.3 Register

```
POST /v1/register
Content-Type: application/json
```

No authentication required.

**Request:**

```json
{
  "auth_key_hash": "<64-char hex string: SHA256(authKey)>",
  "salt": "<64-char hex string: 32 random bytes>"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `auth_key_hash` | string | Hex-encoded SHA-256 hash of the HKDF-derived auth key. Must be exactly 32 bytes (64 hex chars). |
| `salt` | string | Hex-encoded 32-byte random salt used for HKDF derivation. Must be exactly 32 bytes (64 hex chars). |

**Response (success):**

```json
{
  "success": true,
  "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**Response (user exists):**

```json
{
  "success": false,
  "error_code": "USER_EXISTS",
  "error_message": "User with this auth key already exists"
}
```

**Error codes:** `USER_EXISTS`, `INVALID_REQUEST`, `INTERNAL_ERROR`.

---

### 5.4 Store

```
POST /v1/store
Content-Type: application/json
Authorization: Bearer <authKey hex, 64 chars>
```

**Request:**

```json
{
  "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "facts": [
    {
      "id": "f1a2b3c4-d5e6-7890-abcd-ef1234567890",
      "timestamp": "2026-02-24T12:00:00Z",
      "encrypted_blob": "<hex-encoded AES-256-GCM ciphertext, max 2MB hex>",
      "blind_indices": [
        "a1b2c3...64-char-hex-SHA256...",
        "d4e5f6...64-char-hex-SHA256..."
      ],
      "decay_score": 0.9,
      "is_active": true,
      "version": 1,
      "source": "conversation",
      "content_fp": "<64-char hex HMAC-SHA256 fingerprint, optional>",
      "agent_id": "my-agent-v1"
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | Must match the authenticated user's ID. |
| `facts` | array | Yes | Up to 500 facts per request. |
| `facts[].id` | string | Yes | UUIDv7 fact identifier (client-generated). |
| `facts[].timestamp` | string | Yes | ISO 8601 timestamp. |
| `facts[].encrypted_blob` | string | Yes | Hex-encoded AES-256-GCM ciphertext. Max 2 MB hex (1 MB binary). |
| `facts[].blind_indices` | array of strings | Yes | SHA-256 hex hashes for blind search. Max 1000 entries. |
| `facts[].decay_score` | float | No | Importance score, 0.0 to 10.0. Default: 1.0. |
| `facts[].is_active` | bool | No | Whether the fact is active. Default: true. |
| `facts[].version` | int | No | Version for optimistic locking. Default: 1. |
| `facts[].source` | string | Yes | Origin: `"conversation"`, `"pre_compaction"`, or `"explicit"`. Max 100 chars. |
| `facts[].content_fp` | string | No | HMAC-SHA256 content fingerprint for dedup (v0.3.1b). |
| `facts[].agent_id` | string | No | Identifier of the creating agent (v0.3.1b). |

**Response (success):**

```json
{
  "success": true,
  "ids": ["f1a2b3c4-d5e6-7890-abcd-ef1234567890"],
  "version": 1,
  "duplicate_ids": null
}
```

**Response (with duplicates detected):**

```json
{
  "success": true,
  "ids": [],
  "version": 1,
  "duplicate_ids": ["original-fact-id-that-already-exists"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Whether the operation succeeded. |
| `ids` | array of strings | IDs of newly stored facts (excludes duplicates). |
| `version` | int | Highest version among stored facts. |
| `duplicate_ids` | array of strings or null | IDs of existing facts that matched by `content_fp`. Only present if duplicates were detected. |

**Error codes:** `UNAUTHORIZED`, `INVALID_REQUEST`, `STORAGE_ERROR`, `AUTH_FAILED`.

**Server behavior:**

1. Validates `user_id` matches the authenticated user.
2. For each fact:
   - Decodes hex `encrypted_blob` to bytes.
   - If `content_fp` is provided, checks for an existing active fact with the
     same fingerprint. If found, skips the fact and adds the existing ID to
     `duplicate_ids`.
   - Stores the fact with auto-incremented `sequence_id`.
3. Writes an audit log entry to `raw_events`.

---

### 5.5 Search

```
POST /v1/search
Content-Type: application/json
Authorization: Bearer <authKey hex, 64 chars>
```

**Request:**

```json
{
  "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "trapdoors": [
    "a1b2c3...64-char-hex-SHA256...",
    "d4e5f6...64-char-hex-SHA256..."
  ],
  "max_candidates": 3000,
  "min_decay_score": 0.0
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | Must match authenticated user. |
| `trapdoors` | array of strings | Yes | Blind trapdoors (SHA-256 hex hashes). Max 1000 entries. |
| `max_candidates` | int | No | Maximum candidates to return. Default: 3000. Range: 1-10000. |
| `min_decay_score` | float | No | Filter by minimum decay score. Default: 0.0. |

**Response:**

```json
{
  "success": true,
  "results": [
    {
      "fact_id": "f1a2b3c4-d5e6-7890-abcd-ef1234567890",
      "encrypted_blob": "<hex-encoded ciphertext>",
      "decay_score": 0.9,
      "timestamp": 1708776000000,
      "version": 1
    }
  ],
  "total_candidates": 1
}
```

| Field | Type | Description |
|-------|------|-------------|
| `results[].fact_id` | string | Fact identifier. |
| `results[].encrypted_blob` | string | Hex-encoded encrypted ciphertext. |
| `results[].decay_score` | float | Current decay score. |
| `results[].timestamp` | int | Created-at timestamp in Unix milliseconds. |
| `results[].version` | int | Fact version. |
| `total_candidates` | int | Number of results returned. |

**Error codes:** `UNAUTHORIZED`, `INVALID_REQUEST`, `AUTH_FAILED`, `INTERNAL_ERROR`.

---

### 5.6 Export

```
GET /v1/export?limit=1000&cursor=<last_fact_id>
Authorization: Bearer <authKey hex, 64 chars>
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `limit` | int | No | Facts per page. Default: 1000. Max: 5000. |
| `cursor` | string | No | Last `fact_id` from the previous page. Omit for first page. |

**Response:**

```json
{
  "success": true,
  "facts": [
    {
      "id": "f1a2b3c4-d5e6-7890-abcd-ef1234567890",
      "encrypted_blob": "<hex>",
      "blind_indices": ["..."],
      "decay_score": 0.9,
      "version": 1,
      "source": "conversation",
      "created_at": "2026-02-24T12:00:00+00:00",
      "updated_at": "2026-02-24T12:00:00+00:00"
    }
  ],
  "cursor": "next-page-fact-id-or-null",
  "has_more": true,
  "total_count": 42
}
```

Pagination uses a stable cursor based on `(created_at DESC, id DESC)` ordering.
To export all facts, keep requesting with the returned `cursor` until
`has_more` is `false`.

---

### 5.7 Delta Sync (v0.3.1b)

```
GET /v1/sync?since_sequence=0&limit=1000
Authorization: Bearer <authKey hex, 64 chars>
```

| Query Param | Type | Required | Description |
|-------------|------|----------|-------------|
| `since_sequence` | int | No | Return facts with `sequence_id > N`. Default: 0 (all facts). |
| `limit` | int | No | Max facts to return. Default: 1000. Max: 10000. |

**Response:**

```json
{
  "success": true,
  "facts": [
    {
      "id": "f1a2b3c4-...",
      "sequence_id": 42,
      "encrypted_blob": "<hex>",
      "blind_indices": ["..."],
      "decay_score": 0.9,
      "is_active": true,
      "version": 1,
      "source": "conversation",
      "content_fp": "abc123...",
      "agent_id": "agent-1",
      "created_at": "2026-02-24T12:00:00+00:00",
      "updated_at": "2026-02-24T12:00:00+00:00"
    }
  ],
  "latest_sequence": 42,
  "has_more": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `facts` | array | Facts changed since `since_sequence`. Ordered by `sequence_id ASC`. |
| `latest_sequence` | int | Highest `sequence_id` for this user (across all facts, not just returned ones). |
| `has_more` | bool | If true, more facts exist beyond `limit`. Client should paginate. |

**How delta sync works:**

- Each fact receives an auto-incremented `sequence_id` (PostgreSQL sequence,
  per-user monotonic via `nextval('facts_sequence_id_seq')`).
- An agent coming online calls `GET /v1/sync?since_sequence=<last_known>`.
- The server returns all facts (including inactive/deleted tombstoned facts)
  with `sequence_id > since_sequence`.
- The agent stores the `latest_sequence` locally and uses it in the next sync
  call.

---

### 5.8 Delete Fact

```
DELETE /v1/facts/{fact_id}
Authorization: Bearer <authKey hex, 64 chars>
```

**Response (success):**

```json
{
  "success": true
}
```

**Response (not found):**

```json
{
  "success": false,
  "error_code": "NOT_FOUND",
  "error_message": "Fact not found"
}
```

**Server behavior:**

1. Creates a tombstone record in the `tombstones` table (retained 30 days for
   undo capability).
2. Sets `is_active = false` on the fact.
3. The fact no longer appears in search results (search filters on
   `is_active = true`).

---

### 5.9 Delete Account (GDPR)

```
DELETE /v1/account
Authorization: Bearer <authKey hex, 64 chars>
```

**Response:**

```json
{
  "success": true,
  "message": "Account scheduled for deletion. All data will be permanently purged after 30 days.",
  "purge_scheduled_at": "2026-03-26T12:00:00+00:00"
}
```

**Server behavior:**

1. Marks the user as `is_deleted = true` with a `deleted_at` timestamp.
2. Deactivates all user facts (`is_active = false`).
3. Logs a deletion audit event in `raw_events`.
4. After deletion, subsequent authenticated requests with the same auth key
   return HTTP 401 (the user lookup excludes `is_deleted = true`).
5. Data is permanently purged after 30 days via a cleanup job.

---

### 5.10 Relay UserOperation (Phase 3)

```
POST /v1/relay
Content-Type: application/json
Authorization: Bearer <authKey hex, 64 chars>
```

Relays an ERC-4337 UserOperation to the Pimlico bundler for on-chain anchoring
on Base L2. This is a future feature for the Seed-to-Subgraph decentralized
storage layer.

**Request:**

```json
{
  "userOperation": {
    "sender": "0x...",
    "nonce": "0x...",
    "initCode": "0x",
    "callData": "0x...",
    "callGasLimit": "0x50000",
    "verificationGasLimit": "0x60000",
    "preVerificationGas": "0x10000",
    "maxFeePerGas": "0x0",
    "maxPriorityFeePerGas": "0x0",
    "paymasterAndData": "0x",
    "signature": "0x..."
  },
  "target": "0x<EventfulDataEdge contract address>"
}
```

**Response:**

```json
{
  "success": true,
  "userOpHash": "0x...",
  "transactionHash": null
}
```

Rate-limited per sender address (in-memory sliding window).

---

### 5.11 Prometheus Metrics

```
GET /metrics
```

No authentication required. Returns Prometheus-formatted metrics including:

- `http_requests_total` (counter, labels: method, endpoint, status_code)
- `http_request_duration_seconds` (histogram, labels: method, endpoint)
- `http_errors_total` (counter, labels: method, endpoint, status_code)
- `rate_limit_hits_total` (counter, labels: path, limit_type)
- `db_pool_size`, `db_pool_checked_in`, `db_pool_checked_out`, `db_pool_overflow` (gauges)

---

## 6. Search Flow (Blind Index Search)

This is the core of the server-blind design. The server never learns what the
user is searching for.

### 6.1 Client-Side: Generating Trapdoors

```
query_text = "What coffee does the user prefer?"
                |
          tokenize + normalize
                |
     tokens = ["coffee", "prefer"]
                |
     for each token:
         compute LSH buckets
         for each bucket:
             trapdoor = SHA256(bucket_value)
                |
     trapdoors = ["a1b2c3...", "d4e5f6...", ...]
```

The trapdoors are the same SHA-256 hashes that were stored as `blind_indices`
when the fact was originally stored. If a query token produces the same LSH
bucket as a stored fact's token, the trapdoors will match.

### 6.2 Server-Side: GIN Index Lookup

```sql
SELECT id, encrypted_blob, decay_score, created_at, version
FROM facts
WHERE user_id = :user_id
  AND is_active = true
  AND decay_score >= :min_decay
  AND blind_indices && CAST(:trapdoors AS text[])
ORDER BY decay_score DESC
LIMIT :max_candidates
```

- The `&&` operator is PostgreSQL's array overlap operator. It returns rows
  where `blind_indices` has **any** element in common with the `trapdoors`
  array.
- The `idx_facts_blind_gin` GIN index makes this operation efficient even
  at scale.
- Trapdoors are validated server-side: each must be exactly 64 hex characters
  (a valid SHA-256 output). Invalid trapdoors are silently dropped.

### 6.3 Client-Side: Decrypt and Re-rank

The server returns 400-3000 encrypted candidates. The client:

1. **Decrypts** each `encrypted_blob` using AES-256-GCM with the
   `encryptionKey`.
2. **Re-ranks** using a fusion of:
   - **BM25** -- term-frequency relevance scoring on the decrypted plaintext.
   - **Cosine similarity** -- vector similarity between query embedding and
     fact embedding (both computed client-side).
   - **RRF (Reciprocal Rank Fusion)** -- combines BM25 and cosine ranks into
     a single fused ranking.
3. Returns the **top 8** results to the AI agent.

---

## 7. Content Fingerprint Dedup (v0.3.1b)

Prevents storing the same fact twice across different conversations or agents.

### 7.1 How It Works

1. **Client** computes `content_fp = HMAC-SHA256(encryptionKey, fact_plaintext)`.
   This is a deterministic fingerprint that is the same for identical content
   but reveals nothing about the content without the encryption key.
2. **Client** includes `content_fp` in the store request.
3. **Server** checks if an active fact with the same `(user_id, content_fp)`
   exists (using the `idx_facts_user_fp` unique partial index on active facts).
4. If a match is found, the server **skips** the duplicate and returns the
   existing fact's ID in `duplicate_ids`.
5. If no match, the fact is stored normally.

### 7.2 Database Index

```sql
CREATE UNIQUE INDEX idx_facts_user_fp
ON facts (user_id, content_fp)
WHERE is_active = true;
```

This ensures uniqueness only among active facts. Deleted facts (soft-deleted
with `is_active = false`) do not block re-insertion of the same content.

---

## 8. Server Endpoints Summary

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Health check (status + database connectivity) |
| `/ready` | GET | None | Readiness probe for container orchestration |
| `/v1/register` | POST | None | Register new user with auth_key_hash + salt |
| `/v1/store` | POST | Bearer | Store encrypted facts with blind indices |
| `/v1/search` | POST | Bearer | Blind index search, returns encrypted candidates |
| `/v1/export` | GET | Bearer | Export all facts (cursor-based pagination) |
| `/v1/sync` | GET | Bearer | Delta sync by sequence_id (v0.3.1b) |
| `/v1/facts/{id}` | DELETE | Bearer | Soft delete a fact (creates tombstone) |
| `/v1/account` | DELETE | Bearer | Delete account -- GDPR (30-day purge) |
| `/v1/relay` | POST | Bearer | Relay UserOperation to bundler (Phase 3) |
| `/metrics` | GET | None | Prometheus metrics |

---

## 9. Database Schema

Four tables in PostgreSQL:

### users

| Column | Type | Description |
|--------|------|-------------|
| `user_id` | TEXT PK | UUIDv7 identifier |
| `auth_key_hash` | BYTEA(32) | SHA-256 of the HKDF-derived auth key |
| `salt` | BYTEA(32) | Random salt for HKDF derivation |
| `created_at` | TIMESTAMPTZ | Registration timestamp |
| `last_seen_at` | TIMESTAMPTZ | Last authenticated request |
| `is_deleted` | BOOLEAN | Soft delete flag |
| `deleted_at` | TIMESTAMPTZ | When account was deleted |

### facts

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | UUIDv7 fact identifier |
| `user_id` | TEXT FK | Owner (references users) |
| `encrypted_blob` | BYTEA | AES-256-GCM ciphertext |
| `blind_indices` | TEXT[] | Array of SHA-256 hex hashes |
| `decay_score` | FLOAT | Importance score (0.0-10.0) |
| `is_active` | BOOLEAN | Active flag (false = soft deleted) |
| `version` | INTEGER | Optimistic locking version |
| `source` | TEXT | Origin (conversation, pre_compaction, explicit) |
| `created_at` | TIMESTAMPTZ | When the fact was created |
| `updated_at` | TIMESTAMPTZ | Last modification |
| `sequence_id` | BIGINT | Auto-incremented per-user monotonic ID (v0.3.1b) |
| `content_fp` | TEXT | HMAC-SHA256 content fingerprint (v0.3.1b) |
| `agent_id` | TEXT | Creating agent identifier (v0.3.1b) |

**Indexes:**

| Index | Type | Purpose |
|-------|------|---------|
| `idx_facts_user` | B-tree on `user_id` | Fast user lookup |
| `idx_facts_active_decay` | B-tree on `(user_id, is_active, decay_score DESC)` | Sorted retrieval |
| `idx_facts_blind_gin` | GIN on `blind_indices` | Blind index search (array overlap) |
| `idx_facts_search` | Partial B-tree on `(user_id, is_active) WHERE is_active = true` | Active-only queries |
| `idx_facts_user_fp` | Unique partial on `(user_id, content_fp) WHERE is_active = true` | Dedup (v0.3.1b) |
| `idx_facts_user_seq` | B-tree on `(user_id, sequence_id)` | Delta sync (v0.3.1b) |

### raw_events

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PK | Auto-incremented ID |
| `user_id` | TEXT FK | Owner (references users) |
| `event_bytes` | BYTEA | Raw event payload (JSON audit log) |
| `created_at` | TIMESTAMPTZ | Event timestamp |

### tombstones

| Column | Type | Description |
|--------|------|-------------|
| `fact_id` | TEXT PK FK | Deleted fact (references facts) |
| `user_id` | TEXT | Owner |
| `deleted_at` | TIMESTAMPTZ | Deletion timestamp |

Tombstones are retained for 30 days for undo capability.

---

## 10. Middleware and Security

The server applies the following middleware stack (in order):

1. **Request size limit** -- rejects bodies > 50 MB.
2. **Security headers** -- adds `X-Frame-Options: DENY`,
   `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`,
   `Referrer-Policy: strict-origin-when-cross-origin`.
3. **Prometheus metrics** -- records request count, latency, and error rate.
4. **Request logging** -- assigns a `X-Correlation-ID` (UUID) to every request
   for tracing. Does not log headers or bodies (to avoid leaking auth keys).
5. **Sensitive data filter** -- redacts log entries containing auth keys, passwords,
   encrypted blobs, or salts.
6. **Rate limiting** -- per-user rate limiting middleware.
7. **CORS** -- configured per environment. Development allows localhost origins.

---

## 11. E2E Test Script

The file `tests/test_e2e_smoke.py` exercises the full API flow against a running
server instance. It covers:

1. Health check
2. User registration
3. Storing 2 facts (with blind indices and content fingerprint)
4. Searching by single trapdoor (verifies fact 1 found)
5. Searching by multiple trapdoors (verifies fact 2 found)
6. Content fingerprint dedup (re-stores fact 1's fingerprint, verifies duplicate detected)
7. Export with cursor-based pagination
8. Delta sync (verifies sequence_ids)
9. Fact deletion + search verification (deleted fact no longer appears)
10. Account deletion + auth verification (subsequent requests return 401)

### Running the E2E tests

Against a running server (Docker or local):

```bash
SERVER_URL=http://localhost:8080 python tests/test_e2e_smoke.py
```

With pytest:

```bash
SERVER_URL=http://localhost:8080 pytest tests/test_e2e_smoke.py -v
```

The test creates a fresh user with random credentials for each run, so it is
safe to run repeatedly without cleanup.

---

## 12. Sequence Diagram

```
Client                                          Server (PostgreSQL)
  |                                                  |
  |  (1) Generate salt, derive authKey via HKDF      |
  |  (2) Compute authKeyHash = SHA256(authKey)       |
  |                                                  |
  |------- POST /v1/register ----------------------->|
  |  { auth_key_hash, salt }                         |
  |                                                  |  Store auth_key_hash + salt
  |<---------- { user_id } -------------------------|  Return user_id
  |                                                  |
  |  (3) Extract facts from conversation (LLM)      |
  |  (4) Encrypt facts with AES-256-GCM             |
  |  (5) Generate blind_indices (SHA256 of LSH)      |
  |  (6) Compute content_fp = HMAC-SHA256            |
  |                                                  |
  |------- POST /v1/store -------------------------->|
  |  Authorization: Bearer <authKey.hex()>           |
  |  { user_id, facts: [...] }                       |
  |                                                  |  SHA256(authKey) -> lookup user
  |                                                  |  Check content_fp dedup
  |                                                  |  INSERT INTO facts
  |<---------- { ids, duplicate_ids } ---------------|
  |                                                  |
  |  (7) User queries: "What coffee do I like?"      |
  |  (8) Tokenize + LSH -> trapdoors                 |
  |                                                  |
  |------- POST /v1/search ------------------------->|
  |  { user_id, trapdoors: [...] }                   |
  |                                                  |  blind_indices && trapdoors (GIN)
  |                                                  |  Return encrypted candidates
  |<---------- { results: [...] } ------------------|
  |                                                  |
  |  (9) Decrypt candidates with encryptionKey       |
  |  (10) BM25 + Cosine + RRF re-ranking             |
  |  (11) Return top 8 to AI agent                   |
  |                                                  |
```
