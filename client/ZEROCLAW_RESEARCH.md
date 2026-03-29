# ZeroClaw Research — Raw Documentation for TotalReclaw Memory Backend

Collected 2026-03-29 from github.com/zeroclaw-labs/zeroclaw (via public fork az9713/zeroclaw).

---

## 1. Repository Structure

```
zeroclaw/
├── .cargo/
├── .gemini/
├── .githooks/
├── .github/
├── benches/
├── dev/
├── docs/
│   ├── datasheets/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPER_GUIDE.md
│   ├── TECHNOLOGY_STACK.md
│   ├── USER_GUIDE.md
│   ├── actions-source-policy.md
│   ├── adding-boards-and-tools.md
│   ├── agnostic-security.md
│   ├── ci-map.md
│   ├── frictionless-security.md
│   ├── hardware-peripherals-design.md
│   ├── langgraph-integration.md
│   ├── mattermost-setup.md
│   ├── network-deployment.md
│   ├── pr-workflow.md
│   ├── resource-limits.md
│   ├── reviewer-playbook.md
│   └── sandboxing.md
├── examples/
├── firmware/
├── fuzz/
├── python/
├── scripts/
├── src/
│   ├── agent/
│   ├── channels/
│   ├── config/
│   ├── gateway/
│   ├── memory/
│   │   ├── backend.rs
│   │   ├── chunker.rs
│   │   ├── embeddings.rs
│   │   ├── hygiene.rs
│   │   ├── lucid.rs
│   │   ├── markdown.rs
│   │   ├── mod.rs
│   │   ├── none.rs
│   │   ├── response_cache.rs
│   │   ├── snapshot.rs
│   │   ├── sqlite.rs
│   │   ├── traits.rs
│   │   └── vector.rs
│   ├── peripherals/
│   ├── providers/
│   ├── runtime/
│   ├── security/
│   ├── tools/
│   ├── lib.rs
│   └── main.rs
├── test_helpers/
├── tests/
├── AGENTS.md
├── CHANGELOG.md
├── CLAUDE.md
├── CONTRIBUTING.md
├── Cargo.lock
├── Cargo.toml
├── Dockerfile
├── LICENSE
├── README.md
├── SECURITY.md
└── rust-toolchain.toml
```

---

## 2. CLAUDE.md (Full)

```markdown
# CLAUDE.md — ZeroClaw Agent Engineering Protocol

This file defines the default working protocol for claude code in this repository.
Scope: entire repository.

## 1) Project Snapshot (Read First)

ZeroClaw is a Rust-first autonomous agent runtime optimized for:

- high performance
- high efficiency
- high stability
- high extensibility
- high sustainability
- high security

Core architecture is trait-driven and modular. Most extension work should be done by implementing traits and registering in factory modules.

Key extension points:

- `src/providers/traits.rs` (`Provider`)
- `src/channels/traits.rs` (`Channel`)
- `src/tools/traits.rs` (`Tool`)
- `src/memory/traits.rs` (`Memory`)
- `src/observability/traits.rs` (`Observer`)
- `src/runtime/traits.rs` (`RuntimeAdapter`)
- `src/peripherals/traits.rs` (`Peripheral`) — hardware boards (STM32, RPi GPIO)

## 2) Deep Architecture Observations

1. **Trait + factory architecture is the stability backbone**
   - Extension points are intentionally explicit and swappable.
   - Most features should be added via trait implementation + factory registration, not cross-cutting rewrites.
2. **Security-critical surfaces are first-class and internet-adjacent**
   - `src/gateway/`, `src/security/`, `src/tools/`, `src/runtime/` carry high blast radius.
   - Defaults already lean secure-by-default.
3. **Performance and binary size are product goals, not nice-to-have**
   - `Cargo.toml` release profile and dependency choices optimize for size and determinism.
4. **Config and runtime contracts are user-facing API**
   - `src/config/schema.rs` and CLI commands are effectively public interfaces.
   - Backward compatibility and explicit migration matter.
5. **The project now runs in high-concurrency collaboration mode**
   - CI + docs governance + label routing are part of the product delivery system.

## 3) Engineering Principles (Normative)

### 3.1 KISS
- Prefer straightforward control flow over clever meta-programming.
- Prefer explicit match branches and typed structs over hidden dynamic behavior.

### 3.2 YAGNI
- Do not add new config keys, trait methods, feature flags, or workflow branches without a concrete accepted use case.

### 3.3 DRY + Rule of Three
- Duplicate small, local logic when it preserves clarity.
- Extract shared utilities only after repeated, stable patterns (rule-of-three).

### 3.4 SRP + ISP
- Keep each module focused on one concern.
- Extend behavior by implementing existing narrow traits whenever possible.

### 3.5 Fail Fast + Explicit Errors
- Prefer explicit `bail!`/errors for unsupported or unsafe states.
- Never silently broaden permissions/capabilities.

### 3.6 Secure by Default + Least Privilege
- Deny-by-default for access and exposure boundaries.
- Never log secrets, raw tokens, or sensitive payloads.

### 3.7 Determinism + Reproducibility
- Prefer reproducible commands and locked dependency behavior.
- Keep tests deterministic.

### 3.8 Reversibility + Rollback-First Thinking
- Keep changes easy to revert (small scope, clear blast radius).

## 4) Repository Map

- `src/main.rs` — CLI entrypoint and command routing
- `src/lib.rs` — module exports and shared command enums
- `src/config/` — schema + config loading/merging
- `src/agent/` — orchestration loop
- `src/gateway/` — webhook/gateway server
- `src/security/` — policy, pairing, secret store
- `src/memory/` — markdown/sqlite memory backends + embeddings/vector merge
- `src/providers/` — model providers and resilient wrapper
- `src/channels/` — Telegram/Discord/Slack/etc channels
- `src/tools/` — tool execution surface (shell, file, memory, browser)
- `src/peripherals/` — hardware peripherals

## 5) Risk Tiers by Path

- **Low risk**: docs/chore/tests-only changes
- **Medium risk**: most `src/**` behavior changes without boundary/security impact
- **High risk**: `src/security/**`, `src/runtime/**`, `src/gateway/**`, `src/tools/**`, `.github/workflows/**`

## 6) Agent Workflow

1. Read before write
2. Define scope boundary
3. Implement minimal patch
4. Validate by risk tier
5. Document impact
6. Respect queue hygiene

### Branch / Commit / PR Flow
- Create and work from a non-`main` branch.
- Commit changes with clear, scoped commit messages.
- Open a PR to `main`; do not push directly to `main`.
- Merge via PR controls (squash/rebase/merge).

### Code Naming Contract
- Modules/files: `snake_case`
- Types/traits/enums: `PascalCase`
- Functions/variables: `snake_case`
- Constants/statics: `SCREAMING_SNAKE_CASE`
- Trait implementers: `<ProviderName>Provider`, `<ChannelName>Channel`, `<ToolName>Tool`, `<BackendName>Memory`
- Factory registration keys: stable, lowercase, user-facing (e.g. `"openai"`, `"discord"`, `"shell"`)

### Architecture Boundary Contract
- Extend via trait implementations + factory wiring first.
- Dependency direction inward to contracts.
- Avoid cross-subsystem coupling.
- New shared abstractions only after rule-of-three.
- Config keys are public contract: document defaults, compatibility, migration.

## 7) Change Playbooks

### Adding a Provider
- Implement `Provider` in `src/providers/`.
- Register in `src/providers/mod.rs` factory.

### Adding a Channel
- Implement `Channel` in `src/channels/`.

### Adding a Tool
- Implement `Tool` in `src/tools/` with strict parameter schema.

### Adding a Peripheral
- Implement `Peripheral` in `src/peripherals/`.

### Security / Runtime / Gateway Changes
- Include threat/risk notes and rollback strategy.

## 8) Validation Matrix

```bash
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test
```

Preferred:
```bash
./dev/ci.sh all
```

## 10) Anti-Patterns (Do Not)
- Do not add heavy dependencies for minor convenience.
- Do not silently weaken security policy or access constraints.
- Do not add speculative config/feature flags "just in case".
- Do not mix massive formatting-only changes with functional changes.
- Do not modify unrelated modules "while here".
```

---

## 3. Memory Trait (src/memory/traits.rs) — COMPLETE

```rust
use async_trait::async_trait;
use serde::{Deserialize, Serialize};

/// A single memory entry
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub id: String,
    pub key: String,
    pub content: String,
    pub category: MemoryCategory,
    pub timestamp: String,
    pub session_id: Option<String>,
    pub score: Option<f64>,
}

/// Memory categories for organization
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MemoryCategory {
    /// Long-term facts, preferences, decisions
    Core,
    /// Daily session logs
    Daily,
    /// Conversation context
    Conversation,
    /// User-defined custom category
    Custom(String),
}

impl std::fmt::Display for MemoryCategory {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Core => write!(f, "core"),
            Self::Daily => write!(f, "daily"),
            Self::Conversation => write!(f, "conversation"),
            Self::Custom(name) => write!(f, "{name}"),
        }
    }
}

/// Core memory trait — implement for any persistence backend
#[async_trait]
pub trait Memory: Send + Sync {
    /// Backend name
    fn name(&self) -> &str;

    /// Store a memory entry, optionally scoped to a session
    async fn store(
        &self,
        key: &str,
        content: &str,
        category: MemoryCategory,
        session_id: Option<&str>,
    ) -> anyhow::Result<()>;

    /// Recall memories matching a query (keyword search), optionally scoped to a session
    async fn recall(
        &self,
        query: &str,
        limit: usize,
        session_id: Option<&str>,
    ) -> anyhow::Result<Vec<MemoryEntry>>;

    /// Get a specific memory by key
    async fn get(&self, key: &str) -> anyhow::Result<Option<MemoryEntry>>;

    /// List all memory keys, optionally filtered by category and/or session
    async fn list(
        &self,
        category: Option<&MemoryCategory>,
        session_id: Option<&str>,
    ) -> anyhow::Result<Vec<MemoryEntry>>;

    /// Remove a memory by key
    async fn forget(&self, key: &str) -> anyhow::Result<bool>;

    /// Count total memories
    async fn count(&self) -> anyhow::Result<usize>;

    /// Health check
    async fn health_check(&self) -> bool;
}
```

---

## 4. Backend Registry (src/memory/backend.rs) — COMPLETE

```rust
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum MemoryBackendKind {
    Sqlite,
    Lucid,
    Markdown,
    None,
    Unknown,
}

#[allow(clippy::struct_excessive_bools)]
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct MemoryBackendProfile {
    pub key: &'static str,
    pub label: &'static str,
    pub auto_save_default: bool,
    pub uses_sqlite_hygiene: bool,
    pub sqlite_based: bool,
    pub optional_dependency: bool,
}

const SQLITE_PROFILE: MemoryBackendProfile = MemoryBackendProfile {
    key: "sqlite",
    label: "SQLite with Vector Search (recommended) — fast, hybrid search, embeddings",
    auto_save_default: true,
    uses_sqlite_hygiene: true,
    sqlite_based: true,
    optional_dependency: false,
};

const LUCID_PROFILE: MemoryBackendProfile = MemoryBackendProfile {
    key: "lucid",
    label: "Lucid Memory bridge — sync with local lucid-memory CLI, keep SQLite fallback",
    auto_save_default: true,
    uses_sqlite_hygiene: true,
    sqlite_based: true,
    optional_dependency: true,
};

const MARKDOWN_PROFILE: MemoryBackendProfile = MemoryBackendProfile {
    key: "markdown",
    label: "Markdown Files — simple, human-readable, no dependencies",
    auto_save_default: true,
    uses_sqlite_hygiene: false,
    sqlite_based: false,
    optional_dependency: false,
};

const NONE_PROFILE: MemoryBackendProfile = MemoryBackendProfile {
    key: "none",
    label: "None — disable persistent memory",
    auto_save_default: false,
    uses_sqlite_hygiene: false,
    sqlite_based: false,
    optional_dependency: false,
};

const CUSTOM_PROFILE: MemoryBackendProfile = MemoryBackendProfile {
    key: "custom",
    label: "Custom backend — extension point",
    auto_save_default: true,
    uses_sqlite_hygiene: false,
    sqlite_based: false,
    optional_dependency: false,
};

const SELECTABLE_MEMORY_BACKENDS: [MemoryBackendProfile; 4] = [
    SQLITE_PROFILE,
    LUCID_PROFILE,
    MARKDOWN_PROFILE,
    NONE_PROFILE,
];

pub fn selectable_memory_backends() -> &'static [MemoryBackendProfile] {
    &SELECTABLE_MEMORY_BACKENDS
}

pub fn default_memory_backend_key() -> &'static str {
    SQLITE_PROFILE.key
}

pub fn classify_memory_backend(backend: &str) -> MemoryBackendKind {
    match backend {
        "sqlite" => MemoryBackendKind::Sqlite,
        "lucid" => MemoryBackendKind::Lucid,
        "markdown" => MemoryBackendKind::Markdown,
        "none" => MemoryBackendKind::None,
        _ => MemoryBackendKind::Unknown,
    }
}

pub fn memory_backend_profile(backend: &str) -> MemoryBackendProfile {
    match classify_memory_backend(backend) {
        MemoryBackendKind::Sqlite => SQLITE_PROFILE,
        MemoryBackendKind::Lucid => LUCID_PROFILE,
        MemoryBackendKind::Markdown => MARKDOWN_PROFILE,
        MemoryBackendKind::None => NONE_PROFILE,
        MemoryBackendKind::Unknown => CUSTOM_PROFILE,
    }
}
```

---

## 5. Memory Config (src/config/schema.rs — memory section)

```rust
pub struct MemoryConfig {
    pub backend: String,                    // "sqlite" | "lucid" | "markdown" | "none"
    pub auto_save: bool,                    // default: true
    pub hygiene_enabled: bool,              // default: true
    pub archive_after_days: u32,            // default: 7
    pub purge_after_days: u32,              // default: 30
    pub conversation_retention_days: u32,   // default: 30
    pub embedding_provider: String,         // "none" | "openai" | "custom:URL"
    pub embedding_model: String,            // default: "text-embedding-3-small"
    pub embedding_dimensions: usize,        // default: 1536
    pub vector_weight: f64,                 // default: 0.7
    pub keyword_weight: f64,                // default: 0.3
    pub embedding_cache_size: usize,        // default: 10000
    pub chunk_max_tokens: usize,            // default: 512
    pub response_cache_enabled: bool,       // default: false
    pub response_cache_ttl_minutes: u32,    // default: 60
    pub response_cache_max_entries: usize,  // default: 5000
    pub snapshot_enabled: bool,             // default: false
    pub snapshot_on_hygiene: bool,          // default: false
    pub auto_hydrate: bool,                 // default: true
    pub sqlite_open_timeout_secs: Option<u64>,
}
```

### config.toml [memory] section — full reference

```toml
[memory]
backend = "sqlite"                        # sqlite | lucid | markdown | none
auto_save = true                          # persist user messages to memory
hygiene_enabled = true                    # automated cleanup
archive_after_days = 7                    # move old files to archive
purge_after_days = 30                     # permanently delete archived files
conversation_retention_days = 30          # keep conversation memories for N days
embedding_provider = "none"              # none | openai | custom:URL
embedding_model = "text-embedding-3-small"
embedding_dimensions = 1536
vector_weight = 0.7                       # hybrid search vector weight (0.0–1.0)
keyword_weight = 0.3                      # hybrid search keyword weight (0.0–1.0)
embedding_cache_size = 10000
chunk_max_tokens = 512
response_cache_enabled = false
response_cache_ttl_minutes = 60
response_cache_max_entries = 5000
snapshot_enabled = false
snapshot_on_hygiene = false
auto_hydrate = true
# sqlite_open_timeout_secs = 30          # optional, capped at 300s

# Embedding routes (optional, for hint-based resolution)
[[memory.embedding_routes]]
hint = "text-embedding-3-small"
provider = "openai"
model = "text-embedding-3-small"
embedding_provider_env_key = "OPENAI_API_KEY"
```

---

## 6. Cargo.toml — COMPLETE

```toml
[workspace]
members = ["."]
resolver = "2"

[package]
name = "zeroclaw"
version = "0.1.0"
edition = "2021"
authors = ["theonlyhennygod"]
license = "Apache-2.0"
description = "Zero overhead. Zero compromise. 100% Rust. The fastest, smallest AI assistant."
repository = "https://github.com/zeroclaw-labs/zeroclaw"

[dependencies]
clap = { version = "4.5", features = ["derive"] }
tokio = { version = "1.42", default-features = false, features = ["rt-multi-thread", "macros", "time", "net", "io-util", "sync", "process", "io-std", "fs", "signal"] }
reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "blocking", "multipart", "stream"] }
serde = { version = "1.0", default-features = false, features = ["derive"] }
serde_json = { version = "1.0", default-features = false, features = ["std"] }
directories = "6.0"
toml = "1.0"
shellexpand = "3.1"
tracing = { version = "0.1", default-features = false }
tracing-subscriber = { version = "0.3", default-features = false, features = ["fmt", "ansi", "env-filter"] }
anyhow = "1.0"
thiserror = "2.0"
uuid = { version = "1.11", default-features = false, features = ["v4", "std"] }
chacha20poly1305 = "0.10"
hmac = "0.12"
sha2 = "0.10"
hex = "0.4"
rand = "0.9"
parking_lot = "0.12"
async-trait = "0.1"
ring = "0.17"
rusqlite = { version = "0.38", features = ["bundled"] }
chrono = { version = "0.4", default-features = false, features = ["clock", "std", "serde"] }
chrono-tz = "0.10"
cron = "0.15"
dialoguer = { version = "0.12", features = ["fuzzy-select"] }
console = "0.16"
glob = "0.3"
tokio-tungstenite = { version = "0.24", features = ["rustls-tls-webpki-roots"] }
futures-util = { version = "0.3", default-features = false, features = ["sink"] }
futures = "0.3"
regex = "1.10"
axum = { version = "0.8", default-features = false, features = ["http1", "json", "tokio", "query", "ws"] }
tower = { version = "0.5", default-features = false }
tower-http = { version = "0.6", default-features = false, features = ["limit", "timeout"] }
base64 = "0.22"
prost = { version = "0.14", default-features = false }
prometheus = { version = "0.14", default-features = false }
opentelemetry = { version = "0.31", default-features = false, features = ["trace", "metrics"] }
opentelemetry_sdk = { version = "0.31", default-features = false, features = ["trace", "metrics"] }
opentelemetry-otlp = { version = "0.31", default-features = false, features = ["trace", "metrics", "http-proto", "reqwest-client", "reqwest-rustls-webpki-roots"] }
hostname = "0.4.2"
lettre = { version = "0.11.19", default-features = false, features = ["builder", "smtp-transport", "rustls-tls"] }
mail-parser = "0.11.2"
rustls = "0.23"
tokio-rustls = "0.26.4"

# Optional
fantoccini = { version = "0.22.0", optional = true, default-features = false, features = ["rustls-tls"] }
nusb = { version = "0.2", default-features = false, optional = true }
tokio-serial = { version = "5", default-features = false, optional = true }
probe-rs = { version = "0.30", optional = true }
pdf-extract = { version = "0.10", optional = true }

[target.'cfg(target_os = "linux")'.dependencies]
rppal = { version = "0.22", optional = true }
landlock = { version = "0.4", optional = true }

[features]
default = ["hardware"]
hardware = ["nusb", "tokio-serial"]
peripheral-rpi = ["rppal"]
browser-native = ["dep:fantoccini"]
fantoccini = ["browser-native"]
sandbox-landlock = ["dep:landlock"]
sandbox-bubblewrap = []
landlock = ["sandbox-landlock"]
probe = ["dep:probe-rs"]
rag-pdf = ["dep:pdf-extract"]

[profile.release]
opt-level = "z"
lto = "thin"
codegen-units = 1
strip = true
panic = "abort"

[profile.release-fast]
inherits = "release"
codegen-units = 8

[profile.dist]
inherits = "release"
opt-level = "z"
lto = "fat"
codegen-units = 1
strip = true
panic = "abort"

[dev-dependencies]
tempfile = "3.14"
criterion = { version = "0.5", features = ["async_tokio"] }

[[bench]]
name = "agent_benchmarks"
harness = false
```

---

## 7. CONTRIBUTING.md — Summary

### Quick Start
```bash
git config core.hooksPath .githooks
cargo build
cargo test --locked
./scripts/ci/rust_quality_gate.sh
```

### PR Risk Levels
- **Track A (Low)**: docs/tests/refactors — 1 reviewer + green CI
- **Track B (Medium)**: provider/channel/memory/tool changes — subsystem-aware review
- **Track C (High)**: security/runtime/gateway/CI changes — 2-pass review + rollback plan

### Definition of Ready (DoR)
- Single-concern scope
- PR template fully completed
- Local validation run (fmt, clippy, test)
- Security impact and rollback described
- No personal/sensitive data in code/docs/tests
- Tests use neutral, project-scoped wording only

### Definition of Done (DoD)
- CI Required Gate passes
- Required reviewers approved
- Risk label matches scope
- User-visible behavior/migration/rollback documented
- Follow-up TODOs tracked in issues

### Merge Policy
- Squash merge with conventional commit title
- Conventional commits: `feat(scope):`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`
- Revert fast on regressions
- License: MIT for contributions

### Agent-Assisted Contributions
- Welcome and treated as first-class
- Keep PR summaries concrete (problem, change, non-goals)
- Include reproducible validation evidence (fmt, clippy, test, scenario checks)

---

## 8. Factory Function (src/memory/mod.rs) — Summary

The module exports: `backend`, `chunker`, `embeddings`, `hygiene`, `lucid`, `markdown`, `none`, `response_cache`, `snapshot`, `sqlite`, `traits`, `vector`.

Key factory functions:
- `create_memory()` — primary factory, instantiates backend from config, handles hygiene + snapshot + auto-hydration
- `create_memory_with_sqlite_builder()` — generic helper routing through `classify_memory_backend()` to Sqlite/Lucid/Markdown/None
- `create_memory_for_migration()` — rejects "none" backend
- `create_response_cache()` — optional response cache factory

Unknown backends fall through to `MemoryBackendKind::Unknown` which uses `CUSTOM_PROFILE`.

---

## 9. SqliteMemory (src/memory/sqlite.rs) — Key Details

```rust
pub struct SqliteMemory {
    conn: Mutex<Connection>,
    db_path: PathBuf,
    embedder: Arc<dyn EmbeddingProvider>,
    vector_weight: f32,
    keyword_weight: f32,
    cache_max: usize,
}
```

Features:
- FTS5 virtual table with BM25 scoring
- Vector similarity via cosine distance on stored embedding BLOBs
- Hybrid fusion: `final_score = vector_weight * vector_score + keyword_weight * keyword_score`
- LRU embedding cache (SHA-256 key, LRU eviction)
- WAL journaling, PRAGMA tuning
- Session isolation via `session_id` column
- `store()` — upsert via key uniqueness constraint, embeds async before lock
- `recall()` — parallel keyword + vector search, then merge; falls back to LIKE on empty
- `get_or_compute_embedding()` — SHA-256 cache key, LRU eviction
- 80+ tests including schema idempotency, unicode, FTS5 special chars, SQL injection

---

## 10. Embedding System (src/memory/embeddings.rs)

### EmbeddingProvider trait
```rust
#[async_trait]
pub trait EmbeddingProvider: Send + Sync {
    fn name(&self) -> &str;
    fn dimensions(&self) -> usize;
    async fn embed(&self, texts: &[String]) -> anyhow::Result<Vec<Vec<f32>>>;
    async fn embed_one(&self, text: &str) -> anyhow::Result<Vec<f32>> { /* default */ }
}
```

### Implementations
- `NoopEmbedding` — returns empty vectors (0 dimensions), keyword-only fallback
- `OpenAiEmbedding` — configurable base URL + API key, smart endpoint detection

### Factory
```rust
fn create_embedding_provider(name: &str) -> Box<dyn EmbeddingProvider> {
    // "openai" → OpenAI official endpoint
    // "custom:URL" → custom OpenAI-compatible
    // default → NoopEmbedding
}
```

---

## 11. Vector Operations (src/memory/vector.rs)

Key functions:
- `cosine_similarity(a: &[f32], b: &[f32]) -> f32` — clamped 0.0–1.0
- `vec_to_bytes(vec: &[f32]) -> Vec<u8>` — little-endian serialization
- `bytes_to_vec(bytes: &[u8]) -> Vec<f32>` — deserialization
- `hybrid_merge(vector_results, keyword_results, vector_weight, keyword_weight, limit) -> Vec<ScoredResult>` — normalizes keyword scores to [0,1], deduplicates

---

## 12. Other Memory Backends

### NoneMemory
No-op. All operations return Ok with empty/default values. `health_check()` returns true.

### MarkdownMemory
- Core memory → `workspace/MEMORY.md`
- Daily logs → `workspace/memory/YYYY-MM-DD.md`
- Append-only (forget returns false)
- Keyword scoring: matched_keywords / total_keywords

### LucidMemory
- Wraps SqliteMemory + external lucid CLI
- Timeout protection (500ms recall, 800ms store)
- Failure cooldown (15s)
- Smart cascade: local first, Lucid if needed
- Maps MemoryCategory → Lucid types (decision, context, conversation, learning)

---

## 13. Memory Hygiene (src/memory/hygiene.rs)

- `run_if_due()` — executes cleanup every 12 hours
- Archives daily files after `archive_after_days` (default 7)
- Purges archives after `purge_after_days` (default 30)
- Prunes conversation DB rows beyond retention window
- JSON state file tracks execution history

---

## 14. Memory Snapshots (src/memory/snapshot.rs)

- `export_snapshot()` — exports Core memories from SQLite to `MEMORY_SNAPSHOT.md`
- `hydrate_from_snapshot()` — rebuilds SQLite from snapshot on cold boot
- `should_hydrate()` — detects if auto-hydration needed (DB absent, snapshot exists)

---

## 15. WASM Plugin System

From Issue #1787:
- WASM-based tool engine via wasmtime
- Skills installed from ZeroMarket registry to `~/.zeroclaw/workspace/skills/`
- `WasmTool` implements `Tool` trait (NOT Memory trait)
- opt-in via `wasm-tools` feature flag
- Skills declare tools via `SKILL.toml` [[tools]] sections
- Skills' [[tools]] are currently XML in system prompt, NOT registered as callable tool specs

WASM plugins currently interact with memory only through the Tool interface — they call memory tools, they don't implement the Memory trait directly.

---

## 16. Feature Parity (Issue #88)

From search results: Feature parity checklist comparing OpenClaw → ZeroClaw migration blockers. The issue tracks what OpenClaw features ZeroClaw needs to match.

(Issue body not accessible due to GitHub auth requirements.)

---

## 17. Key Integration Points for TotalReclaw Backend

To add a new memory backend to ZeroClaw:

1. **Implement `Memory` trait** in a new file (e.g., `src/memory/totalreclaw.rs`)
   - 7 required methods: `name()`, `store()`, `recall()`, `get()`, `list()`, `forget()`, `count()`, `health_check()`

2. **Add to `MemoryBackendKind` enum** in `backend.rs`
   - Add `TotalReclaw` variant
   - Add `TOTALRECLAW_PROFILE` const
   - Update `classify_memory_backend()` match arm
   - Update `memory_backend_profile()` match arm

3. **Register in factory** in `mod.rs`
   - Add `totalreclaw` module declaration
   - Add match arm in `create_memory_with_sqlite_builder()` (or dedicated path in `create_memory()`)

4. **Add config keys** to `MemoryConfig` in `src/config/schema.rs`
   - TotalReclaw-specific fields (e.g., `totalreclaw_server_url`, `totalreclaw_credentials_path`)
   - Document defaults and migration path

5. **Naming convention**: `TotalReclawMemory` struct, `"totalreclaw"` factory key

6. **Dependencies**: Add to `Cargo.toml`, ideally behind a feature flag to avoid bloating the default binary

7. **Validation**: `cargo fmt`, `cargo clippy`, `cargo test` must pass
