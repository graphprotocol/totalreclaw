# Rust Core Rollout Plan

> **Status:** Core crate built, WASM + PyO3 bindings verified (245 tests passing). Ready for client migration.

## What's Done

| Component | Status | Tests |
|-----------|--------|-------|
| `totalreclaw-core` Rust crate | Complete | 50 |
| WASM bindings (`@totalreclaw/core`) | Complete | 60 parity |
| PyO3 bindings (`totalreclaw_core`) | Complete | 31 standalone + 14 parity |
| `totalreclaw-memory` re-exports from core | Complete | 78 |
| Cross-impl parity (TS ↔ Python) | Verified | 12 |

## What's Left: Client Migration

### Phase 1: Publish Packages

**1a. Publish WASM npm package**
```bash
cd rust/totalreclaw-core
wasm-pack build --target nodejs --out-dir pkg --features wasm
cd pkg && npm publish --access public
```
Package: `@totalreclaw/core@0.1.0`

**1b. Publish Python wheel**
```bash
cd rust/totalreclaw-core
maturin build --release --features python
# Upload wheels to PyPI
twine upload target/wheels/totalreclaw_core-*.whl
```
Package: `totalreclaw-core` on PyPI

**1c. Publish Rust crate (optional)**
```bash
cd rust/totalreclaw-core && cargo publish
```
Only needed if external Rust consumers exist (ZeroClaw is internal).

---

### Phase 2: Migrate MCP Server (highest priority — most users)

**Files to change:**
- `mcp/package.json` — add `@totalreclaw/core` dependency
- `mcp/src/subgraph/crypto.ts` — replace implementations with WASM wrapper calls
- `mcp/src/subgraph/lsh.ts` — replace with WASM `WasmLshHasher`

**Migration strategy:**
1. Add `@totalreclaw/core` as a dependency
2. Create a `mcp/src/subgraph/crypto-wasm.ts` adapter that wraps WASM calls with the same function signatures as `crypto.ts`
3. Update imports in `mcp/src/index.ts` and other files to use `crypto-wasm.ts`
4. Keep old `crypto.ts` temporarily as fallback (feature flag: `TOTALRECLAW_USE_NATIVE_CRYPTO=true` to use old TS)
5. Run full MCP test suite (222 tests)
6. Run MCP debrief E2E tests (17 staging tests)
7. Once verified, remove old `crypto.ts` and rename `crypto-wasm.ts` to `crypto.ts`

**What stays in TS:**
- `deriveKeys()` Argon2id legacy path (self-hosted mode only, not BIP-39)
- Smart Account address derivation (uses `viem`)
- HTTP relay client
- Tool handlers

**Verification:**
```bash
cd mcp && npm run build && npm test  # 222+ tests
TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz npx tsx ../tests/e2e-debrief/mcp-debrief-e2e.ts  # staging E2E
```

---

### Phase 3: Migrate OpenClaw Plugin

**Files to change:**
- `skill/plugin/package.json` — add `@totalreclaw/core`
- `skill/plugin/crypto.ts` — replace with WASM wrappers (same adapter pattern as MCP)
- `skill/plugin/lsh.ts` — replace with WASM `WasmLshHasher`

**Note:** The plugin shares the same crypto API as MCP. The adapter code from Phase 2 can be copied directly.

**Verification:**
- Plugin has no test runner, but compile check + OpenClaw E2E covers it

---

### Phase 4: Migrate Client Library

**Files to change:**
- `client/package.json` — add `@totalreclaw/core`
- `client/src/crypto/seed.ts` — BIP-39 functions → WASM
- `client/src/crypto/aes.ts` — encrypt/decrypt → WASM
- `client/src/crypto/blind.ts` — blind indices → WASM
- `client/src/crypto/fingerprint.ts` — content fingerprint → WASM
- `client/src/lsh/` — LSH hasher → WASM

**What stays in TS:**
- `client/src/crypto/kdf.ts` — Argon2id (native C binding, faster than WASM)
- Smart Account management

**Verification:**
```bash
cd client && npm test  # client library tests
```

---

### Phase 5: Migrate Python Client

**Files to change:**
- `python/pyproject.toml` — add `totalreclaw-core` dependency, remove `PyStemmer`
- `python/src/totalreclaw/crypto.py` — replace with thin PyO3 wrapper
- `python/src/totalreclaw/lsh.py` — replace with PyO3 `LshHasher`

**Wrapper pattern:**
```python
import totalreclaw_core

def derive_keys_from_mnemonic(mnemonic: str) -> DerivedKeys:
    result = totalreclaw_core.derive_keys_from_mnemonic(mnemonic)
    return DerivedKeys(
        salt=result['salt'],
        auth_key=result['auth_key'],
        encryption_key=result['encryption_key'],
        dedup_key=result['dedup_key'],
    )

def encrypt(plaintext: str, encryption_key: bytes) -> str:
    return totalreclaw_core.encrypt(plaintext, encryption_key)

# ... etc
```

**Stemmer note:** The Python client currently uses Snowball (Porter 2) via `PyStemmer`, while the Rust core uses Porter 1. Switching to the Rust core changes the stemmer output for some words. This affects blind index generation — existing facts indexed with Snowball won't be found by Porter 1 trapdoors for those specific words. Mitigation options:
1. **Dual-stemmer mode**: Generate blind indices with both Porter 1 and Snowball stems during a transition period
2. **Accept minor recall impact**: The affected words are rare edge cases (e.g., "monday" → "mondai" in Porter 1 but unchanged in Snowball). Most search queries use multiple tokens, so missing one stem rarely affects recall.
3. **Re-index**: For users who switch from Python to TS (or vice versa), the `totalreclaw_consolidate` tool can re-index facts

**Recommended:** Option 2 (accept minor impact). The parity tests show the divergence is minimal, and the existing cross-client E2E tests pass despite the stemmer difference.

**Verification:**
```bash
cd python && pip install -e ".[dev]" && python -m pytest tests/ -v  # 214+ tests
python ../tests/e2e-debrief/cross-client-debrief-e2e.py  # cross-client E2E
```

---

### Phase 6: Cleanup & Deprecation

Once all clients are migrated and verified:

**Remove per-language crypto code:**
| File | Action |
|------|--------|
| `mcp/src/subgraph/crypto.ts` | Delete (replaced by WASM wrapper) |
| `mcp/src/subgraph/lsh.ts` | Delete (replaced by WASM wrapper) |
| `skill/plugin/crypto.ts` | Delete (replaced by WASM wrapper) |
| `skill/plugin/lsh.ts` | Delete (replaced by WASM wrapper) |
| `client/src/crypto/seed.ts` | Slim down (keep Argon2id, remove BIP-39) |
| `client/src/crypto/aes.ts` | Delete (replaced by WASM) |
| `client/src/crypto/blind.ts` | Delete (replaced by WASM) |
| `client/src/crypto/fingerprint.ts` | Delete (replaced by WASM) |
| `client/src/lsh/` | Delete (replaced by WASM) |
| `python/src/totalreclaw/crypto.py` | Slim down to PyO3 wrapper only |
| `python/src/totalreclaw/lsh.py` | Delete (replaced by PyO3) |

**Remove duplicate dependencies from package.json files:**
- `@noble/hashes` — no longer needed in MCP/Plugin (WASM handles crypto)
- `@scure/bip39` — no longer needed (WASM handles BIP-39)
- `porter-stemmer` — no longer needed (Rust core handles stemming)
- `PyStemmer` — no longer needed in Python

**Update CLAUDE.md:**
- Feature matrix: note unified core for crypto column
- Known Gaps: remove "5× crypto implementations" concern
- Build instructions: add WASM build + PyO3 build commands

**Update client-consistency spec:**
- Note that all crypto operations now delegate to `totalreclaw-core`
- Single reference implementation = guaranteed parity

---

## Rollout Order & Risk Assessment

| Phase | Risk | Rollback |
|-------|------|----------|
| 1. Publish packages | Low | Unpublish if broken |
| 2. MCP server | Medium | Revert to TS crypto (keep old file) |
| 3. OpenClaw plugin | Low | Same adapter, same WASM |
| 4. Client library | Medium | Has native Argon2id path that stays |
| 5. Python client | Medium | Stemmer change (minor recall impact) |
| 6. Cleanup | Low | No functional change, just removing dead code |

**Recommended cadence:**
- Phase 1-2: This session or next
- Phase 3-4: Next session (after MCP is validated in production)
- Phase 5: After Python stemmer impact is assessed
- Phase 6: After all phases verified for 1+ week

---

## E2E Validation Gate (Must Pass Before Each Phase Ships)

For each client migration, ALL of these must pass:
1. Client's own unit test suite (222 MCP / 214 Python / etc.)
2. Cross-impl parity (`tests/parity/cross-impl-test.ts`) — 12 tests
3. Debrief E2E (`tests/e2e-debrief/`) — 37 tests (20 offline + 17 staging)
4. Cross-client E2E (Python ↔ TS) — `tests/e2e-debrief/cross-client-debrief-e2e.py`
5. WASM parity (`rust/totalreclaw-core/tests/wasm_parity.mjs`) — 60 tests
6. PyO3 parity (`rust/totalreclaw-core/tests/python_parity_test.py`) — 14 tests
