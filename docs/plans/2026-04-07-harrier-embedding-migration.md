# Embedding Model Migration: e5-small (384d) -> Harrier q4 (640d)

> **Status: COMPLETE**
> **Date: 2026-04-07 (executed 2026-04-07)**

## Overview

Replace Xenova/multilingual-e5-small (384d, q8, ~34MB, mean pooling) with
onnx-community/harrier-oss-v1-270m-ONNX (640d, q4, ~344MB, pre-pooled
`sentence_embedding` output) as the default embedding model across all
clients (TypeScript, Python, Rust).

**Why now:** The GatherBlockQuantized ONNX op that blocked q4 Harrier is
now supported on onnxruntime 1.24.4. Harrier q4 gives significantly better
retrieval accuracy than e5-small at a reasonable size increase.

**Why it's safe:** No users in production yet. All existing on-chain data
is on Base Sepolia testnet and can be discarded. No data migration needed.

**Key model details (Harrier q4):**
- HuggingFace ID: `onnx-community/harrier-oss-v1-270m-ONNX`
- Quantization: q4 (was blocked, now works)
- Download size: ~344MB (one-time, cached in `~/.cache/huggingface/`)
- Dimensions: 640
- Pooling: **pre-pooled** -- the ONNX model has a `sentence_embedding` output
  with shape `(batch, 640)`. No manual last-token or mean pooling needed.
- No instruction prefix on queries (confirmed by prior spike -- hurts accuracy)
- L2 normalized output

---

## Task 0: Spike -- Verify Harrier q4 Works in onnxruntime 1.24

**Goal:** Confirm the q4 variant loads and produces correct 640d embeddings
on onnxruntime 1.24.4 in both TypeScript (`@huggingface/transformers`) and
Python (`onnxruntime` direct).

**Steps:**

1. Create a temporary spike script in `client/tests/harrier-q4-spike.ts`:
   ```typescript
   import { pipeline } from '@huggingface/transformers';
   const extractor = await pipeline('feature-extraction',
     'onnx-community/harrier-oss-v1-270m-ONNX', { dtype: 'q4' });
   const output = await extractor('Hello world', { pooling: 'none', normalize: false });
   // Check if model has sentence_embedding output, otherwise use last_token pooling
   console.log('output shape:', output.dims);
   console.log('output data length:', output.data.length);
   ```

2. Test in Python:
   ```python
   from huggingface_hub import hf_hub_download
   import onnxruntime as ort
   path = hf_hub_download('onnx-community/harrier-oss-v1-270m-ONNX',
                          filename='onnx/model_q4.onnx')
   session = ort.InferenceSession(path)
   # Check output names -- look for 'sentence_embedding'
   print([o.name for o in session.get_outputs()])
   ```

3. Verify:
   - Model loads without `GatherBlockQuantized` errors
   - Output dimension is 640
   - Determine exact output name and whether manual pooling is needed
   - Cross-check: same input text produces same (or close) embeddings in both runtimes

**Pass criteria:** Both TS and Python produce 640d L2-normalized vectors
from the q4 variant. Document the exact `dtype`, `pooling`, and output
extraction approach.

**Important:** If the `@huggingface/transformers` pipeline does not
automatically use the `sentence_embedding` output, you may need to use
`pooling: 'none'` and extract the named output manually. The Python
`onnxruntime.InferenceSession` approach already handles named outputs.
Determine the exact approach in the spike before proceeding.

---

## Task 1: TypeScript -- MCP Server Embedding (`mcp/src/subgraph/embedding.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/mcp/src/subgraph/embedding.ts`

This is the MCP server's embedding module. Currently hardcoded to e5-small.

### Current state (lines 1-68):
- Line 22: `const MODEL_ID = 'Xenova/multilingual-e5-small';`
- Line 25: `const EMBEDDING_DIM = 384;`
- Lines 31-39: JSDoc says "384-dimensional", "e5-small"
- Line 47: `pipeline('feature-extraction', MODEL_ID, { dtype: 'q8' })`
- Line 54: `pooling: 'mean'` and `query: ` prefix for isQuery
- Line 67: `return EMBEDDING_DIM;`

### Changes:
```
Line 4:  "Uses Xenova/multilingual-e5-small to generate 384-dimensional text"
      -> "Uses onnx-community/harrier-oss-v1-270m-ONNX to generate 640-dimensional text"

Lines 7-14: Update all comment references:
  - "~34MB download" -> "~344MB download"
  - "384-dimensional" -> "640-dimensional"
  - "mean pooling" -> "pre-pooled sentence_embedding output"
  - Remove "100+ languages (multilingual)" if not applicable or update

Line 22: const MODEL_ID = 'Xenova/multilingual-e5-small';
      -> const MODEL_ID = 'onnx-community/harrier-oss-v1-270m-ONNX';

Line 25: const EMBEDDING_DIM = 384;
      -> const EMBEDDING_DIM = 640;

Line 46-47: Update console.error message:
  "~34MB, first run only" -> "~344MB, first run only"

Line 47: pipeline options:
  { dtype: 'q8' } -> { dtype: 'q4' }

Line 53-54: Remove the query prefix and change pooling:
  const input = options?.isQuery ? `query: ${text}` : text;
  const output = await extractor(input, { pooling: 'mean', normalize: true });
  ->
  const output = await extractor(text, { pooling: 'none', normalize: false });
  // Extract sentence_embedding output (pre-pooled, 640d)
  // NOTE: Exact extraction depends on spike results. If pipeline returns
  // the sentence_embedding directly, use { pooling: 'last_token', normalize: true }
  // or extract manually and L2-normalize.

Lines 31-39, 60-67: Update JSDoc references to 640d, remove e5-small mentions
```

**Critical:** The exact pooling/extraction approach depends on Task 0 spike
results. The `@huggingface/transformers` pipeline may or may not auto-detect
the `sentence_embedding` output from the Harrier ONNX model. Two paths:

- **Path A:** Pipeline supports it natively via `pooling: 'last_token'` or
  a new pooling mode. Use that directly.
- **Path B:** Pipeline does not support pre-pooled output. Use `pooling: 'none'`,
  then extract the `sentence_embedding` tensor manually from the raw output
  and L2-normalize it.

The Python client (Task 4) already does manual extraction via
`onnxruntime.InferenceSession`, so parity between TS and Python is the key
validation criterion.

---

## Task 2: TypeScript -- OpenClaw Plugin Embedding (`skill/plugin/embedding.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/skill/plugin/embedding.ts`

The plugin already has a `harrier` entry in its MODELS map but uses
`default: e5-small`. The default must switch to Harrier q4.

### Current state (lines 28-50):
```typescript
const MODELS: Record<string, ModelConfig> = {
  default: {
    id: 'Xenova/multilingual-e5-small',
    dims: 384,
    pooling: 'mean',
    size: '~34MB',
    dtype: 'q8',
  },
  harrier: {
    id: 'onnx-community/harrier-oss-v1-270m-ONNX',
    dims: 640,
    pooling: 'last_token',
    size: '~553MB',
    dtype: 'fp16',  // q4 uses unsupported GatherBlockQuantized op
  },
  // ...
};
```

### Changes:

1. **Swap the default to Harrier q4:**
   ```typescript
   const MODELS: Record<string, ModelConfig> = {
     default: {
       id: 'onnx-community/harrier-oss-v1-270m-ONNX',
       dims: 640,
       pooling: 'none',  // pre-pooled sentence_embedding -- see spike
       size: '~344MB',
       dtype: 'q4',
     },
     small: {
       id: 'Xenova/multilingual-e5-small',
       dims: 384,
       pooling: 'mean',
       size: '~34MB',
       dtype: 'q8',
     },
     large: {
       id: 'onnx-community/Qwen3-Embedding-0.6B-ONNX',
       dims: 1024,
       pooling: 'last_token',
       size: '~600MB',
       dtype: 'q8',
     },
   };
   ```

2. **Update `generateEmbedding()` (lines 67-87):**
   - Remove the `query: ` prefix logic (Harrier needs no prefix)
   - Update pooling based on spike results (may need manual extraction for
     `sentence_embedding` pre-pooled output, or `last_token` if pipeline
     handles it)
   - Keep the small/large fallback models working with their own pooling

   The current code (line 82-86):
   ```typescript
   const input = model.pooling === 'mean' && options?.isQuery
     ? `query: ${text}`
     : text;
   const output = await extractor(input, { pooling: model.pooling as any, normalize: true });
   return Array.from(output.data as Float32Array);
   ```

   Should become:
   ```typescript
   const input = model.pooling === 'mean' && options?.isQuery
     ? `query: ${text}`
     : text;
   const output = await extractor(input, {
     pooling: model.pooling as any,
     normalize: model.pooling !== 'none',  // pre-pooled models handle their own normalization
   });
   // For pre-pooled models, extract and normalize manually
   let embedding = Array.from(output.data as Float32Array);
   if (model.pooling === 'none') {
     // sentence_embedding output is pre-pooled but may need L2 normalization
     const norm = Math.sqrt(embedding.reduce((s, v) => s + v * v, 0));
     if (norm > 0) embedding = embedding.map(v => v / norm);
   }
   return embedding;
   ```

   **NOTE:** Exact approach depends on spike. The key invariant is: same
   input text must produce the same 640d vector in both TS and Python.

3. **Update comments (lines 1-13):**
   - Line 8: `"default"` description to mention Harrier q4
   - Remove the `"q4 uses unsupported GatherBlockQuantized op"` comment

4. **Update `getEmbeddingDims()` JSDoc (lines 89-95):**
   ```
   "Returns 640 (default/Harrier), 384 (small), or 1024 (large)"
   ```
   This is already correct. No change needed.

---

## Task 3: TypeScript -- Client Library (`client/src/embedding/onnx.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/client/src/embedding/onnx.ts`

The client library is still hardcoded to e5-small. This must match MCP/plugin.

### Current state:
- Line 21: `const EMBEDDING_DIM = 384;`
- Line 24: `const MODEL_ID = 'Xenova/multilingual-e5-small';`
- Lines 1-15: All comments reference e5-small, 384d, 34MB, mean pooling
- Line 44: `console.error('[TotalReclaw] Downloading embedding model (~34MB, first run only)...');`
- Line 45-46: `pipeline('feature-extraction', MODEL_ID, { dtype: 'q8' })`
- Line 79: JSDoc says "640-dimensional" (already wrong -- says 640 but code is 384)
- Line 90: `pooling: 'mean'`
- Line 105: JSDoc says "640-dimensional" (same inconsistency)
- Line 134: `createDummyEmbedding` uses `EMBEDDING_DIM`
- Line 177: `createHashBasedEmbedding` uses `EMBEDDING_DIM`

### Changes:

```
Line 3-15: Rewrite module JSDoc:
  - Model: onnx-community/harrier-oss-v1-270m-ONNX (q4)
  - ~344MB download
  - 640-dimensional output
  - pre-pooled sentence_embedding (no manual pooling)

Line 21: const EMBEDDING_DIM = 384;
      -> const EMBEDDING_DIM = 640;

Line 24: const MODEL_ID = 'Xenova/multilingual-e5-small';
      -> const MODEL_ID = 'onnx-community/harrier-oss-v1-270m-ONNX';

Line 37: load() JSDoc: "~553MB, fp16" -> "~344MB, q4"
Line 44: console.error message: "~34MB" -> "~344MB"

Line 45-46: pipeline options:
  { dtype: 'q8' } -> { dtype: 'q4' }

Line 90: Change pooling:
  await this.extractor!(input, { pooling: 'mean', normalize: true })
  -> Update based on spike results (same approach as Task 2)

Line 89: Remove query prefix logic:
  const input = text;  // already correct (no prefix for Harrier)
  This line is already correct -- no change needed.
```

---

## Task 4: TypeScript -- Client Library LSH Init (`client/src/index.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/client/src/index.ts`

Two methods hardcode `384` for LSH hasher initialization.

### Current state:
- Line 133-134 (`initLshFromMnemonic`):
  ```typescript
  this.lshHasher = new LSHHasher(
    lshSeed,
    384, // multilingual-e5-small dimension
  ```
- Line 145-146 (`initLshFromSeed`):
  ```typescript
  this.lshHasher = new LSHHasher(
    seed,
    384, // multilingual-e5-small dimension
  ```

### Changes:

```
Line 133-134:
  384, // multilingual-e5-small dimension
  ->
  640, // harrier-oss-v1-270m dimension

Line 145-146:
  384, // multilingual-e5-small dimension
  ->
  640, // harrier-oss-v1-270m dimension
```

**Better approach:** Instead of hardcoding, import `getEmbeddingDims()` from
the embedding module and use it dynamically. But the current code imports
`EmbeddingModel` and `createHashBasedEmbedding` (line 51), so we could
import `EMBEDDING_DIM` or add a static accessor. However, since the client
library has a fixed model (not configurable like the plugin), hardcoding
640 is acceptable and consistent with the current pattern.

---

## Task 5: Python -- Embedding (`python/src/totalreclaw/embedding.py`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/python/src/totalreclaw/embedding.py`

**Good news:** Python already uses Harrier. But it may be using the wrong
quantization variant or the fp16/default variant instead of q4.

### Current state (already correct dims, need to check quantization):
- Line 25: `MODEL_ID = "onnx-community/harrier-oss-v1-270m-ONNX"`
- Line 26: `EMBEDDING_DIMS = 640`
- Line 38-41: Downloads `"onnx/model_quantized.onnx"` -- this is likely the
  default quantized variant, not specifically q4

### Changes needed:

1. **Update the ONNX filename to q4 explicitly (line 38-41):**
   ```python
   model_path = hf_hub_download(
       repo_id=MODEL_ID,
       filename="onnx/model_q4.onnx",  # was "onnx/model_quantized.onnx"
   )
   ```
   **Verify the exact filename** by checking the HuggingFace repo:
   `https://huggingface.co/onnx-community/harrier-oss-v1-270m-ONNX/tree/main/onnx`
   The file might be `model_q4.onnx`, `model_quantized.onnx`, or similar.

2. **Update onnxruntime minimum version** in `pyproject.toml` (line 16):
   ```
   "onnxruntime>=1.17",
   ->
   "onnxruntime>=1.24",
   ```
   This ensures the GatherBlockQuantized op is supported.

3. **Check if the q4 model has `sentence_embedding` output:**
   If the q4 variant has a pre-pooled output, the current last-token pooling
   logic (lines 99-103) may produce wrong results. The spike (Task 0) will
   clarify this. Two possibilities:
   - **If model has `sentence_embedding` output:** Use it directly, remove
     the KV-cache and last-token pooling code.
   - **If model only has `last_hidden_state`:** Keep current approach.

4. **Update module docstring** (lines 1-12):
   - "quantized" -> "q4 quantized"
   - Remove "last-token" if using pre-pooled output

5. **Update the test file comment** (line 3 of `python/tests/test_embedding.py`):
   ```
   "~34MB for e5-small, ~164MB for Harrier"
   ->
   "~344MB for Harrier q4"
   ```

---

## Task 6: Python -- LSH (`python/src/totalreclaw/lsh.py`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/python/src/totalreclaw/lsh.py`

No hardcoded dimension -- the LSH hasher takes `dims` as a constructor
parameter (line 28). Callers pass the dimension dynamically.

**No changes needed in this file.** The callers (in the relay/client code)
must pass `640` instead of `384`, but the Python client's relay module
already uses `get_embedding_dims()` which returns `640`.

**Verify:** Grep the Python codebase for any hardcoded `384` passed to
`LSHHasher()`:
```bash
grep -rn "LSHHasher.*384" python/
```

---

## Task 7: Rust Core -- LSH (`rust/totalreclaw-core/src/lsh.rs`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/rust/totalreclaw-core/src/lsh.rs`

The Rust LSH implementation takes `dims` as a parameter -- no hardcoded
dimension. The WASM/PyO3 bindings also take `dims` as a parameter.

**No changes needed in the core LSH code itself.**

However, **test fixtures and docstring examples** reference `1024`:

- `rust/totalreclaw-core/src/wasm.rs` line 157: JSDoc says "e.g. 1024"
  -> Change to "e.g. 640"
- `rust/totalreclaw-core/src/python.rs` line 151: JSDoc says "e.g. 1024"
  -> Change to "e.g. 640"
- `rust/totalreclaw-core/src/search.rs` line 442: test uses `LshHasher::new(&lsh_seed, 1024)` and `vec![0.5f32; 1024]`
  -> Change to 640
- `rust/totalreclaw-core/src/store.rs` lines 282, 288: test uses 1024
  -> Change to 640

These are test-only changes that don't affect functionality but keep
documentation and tests consistent with the production dimension.

---

## Task 8: Rust ZeroClaw Memory (`rust/totalreclaw-memory/`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/rust/totalreclaw-memory/src/backend.rs`

### Current state:
- Line 123: `pub embedding_dims: usize,`
- Line 136: `embedding_dims: 640,` (default)
- Line 152: `LshHasher::new(&lsh_seed, config.embedding_dims)?`

**Good news:** Already defaults to 640. No changes needed here.

**File:** `/Users/pdiogo/Documents/code/totalreclaw/rust/totalreclaw-memory/src/embedding.rs`
- Lines 262, 265, 275, 278, 301: Tests already use 640.

**No changes needed in ZeroClaw.** It was already built for Harrier 640d.

---

## Task 9: Package Version Bumps

### onnxruntime minimum versions

These must be bumped to ensure GatherBlockQuantized op support:

1. **Python `pyproject.toml`** (line 16):
   ```
   "onnxruntime>=1.17" -> "onnxruntime>=1.24"
   ```

2. **`skill/package.json`** (line 27):
   ```
   "onnxruntime-node": "^1.17.0" -> "onnxruntime-node": "^1.24.0"
   ```
   **Note:** The skill package uses `onnxruntime-node` as a direct dep.
   Verify that `@huggingface/transformers` v4.x uses onnxruntime-node or
   onnxruntime-web internally. If the skill no longer uses `onnxruntime-node`
   directly (only via `@huggingface/transformers`), this dep may be removable.

3. **`@huggingface/transformers`** -- currently `^4.0.1` in client, mcp,
   and subgraph package.json files. Verify that `^4.0.1` resolves to a
   version that bundles onnxruntime >= 1.24. If not, bump:
   ```
   "@huggingface/transformers": "^4.0.1" -> "@huggingface/transformers": "^4.3.0"
   ```
   (or whatever version first ships onnxruntime 1.24+)

### npm package versions

After all changes, version bump:
- `@totalreclaw/client` -- patch or minor bump
- `@totalreclaw/mcp-server` -- patch or minor bump

---

## Task 10: Update Tests

### 10a: Plugin E2E test (`skill/plugin/pocv2-e2e-test.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/skill/plugin/pocv2-e2e-test.ts`

- Line 209: `const EMBEDDING_MODEL_ID = 'Xenova/bge-small-en-v1.5';`
  -> This is an older test that uses bge-small. Consider updating to use
  the production model, or leave as-is since it tests LSH mechanics not
  the model itself.
- Line 210: `const LOCAL_EMBEDDING_DIM = 384;`
  -> If updating the model: `640`
- Lines 419, 425, 578, 678, 801, 809, 892: References to "384-dim" and
  "bge-small-en-v1.5" -> Update if model is changed

**Decision:** This test is a PoC integration test. It can either:
- (A) Be updated to use Harrier (more realistic but slower due to 344MB download)
- (B) Stay on e5-small/bge-small for fast CI runs

Recommendation: **(B)** -- keep it as a lightweight integration test. The
E2E tests (Task 12) will validate the full Harrier pipeline.

### 10b: Plugin LSH test (`skill/plugin/lsh.test.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/skill/plugin/lsh.test.ts`

- Line 234: `const wrongDims = makeEmbedding(1, 384);`
- Line 238: `'Throws on dimension mismatch (384 vs 1536)'`
- Line 346: `const hasher = new LSHHasher(seed1, 384, 8, 32);`
- Line 349: `assert(hasher.dimensions === 384, ...)`

These tests test dimension mismatch behavior, not the production dimension.
**The test values can stay as-is** -- they test that the hasher rejects
wrong dimensions, regardless of what the production dimension is.

### 10c: Parity test (`tests/parity/parity-test.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/tests/parity/parity-test.ts`

- Lines 405-411: Cosine similarity test uses 384-dim vectors
  ```typescript
  const embA = makeEmbedding(42, 384);
  const embB = makeEmbedding(43, 384);
  ```
  -> Change to 640:
  ```typescript
  const embA = makeEmbedding(42, 640);
  const embB = makeEmbedding(43, 640);
  ```
  And update the assertion message on line 411.

### 10d: Cross-implementation parity (`tests/parity/cross-impl-test.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/tests/parity/cross-impl-test.ts`

- The fixture format includes `embedding_dims` (line 61)
- The test reads dims from the fixture: `vectors.lsh.embedding_dims` (line 193)

The fixture is generated by `generate-fixtures.ts` which uses `REAL_DIMS = 1024`
(line 119 of generate-fixtures.ts). These are abstract parity tests -- the
dimension doesn't need to match production. **No change needed**, but updating
to 640 would be more representative.

### 10e: Fixture regeneration (`tests/parity/generate-fixtures.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/tests/parity/generate-fixtures.ts`

If changing the cross-impl fixture dims:
- Line 119: `const REAL_DIMS = 1024;` -> `const REAL_DIMS = 640;`

Then regenerate:
```bash
cd tests/parity && npx tsx generate-fixtures.ts
```

This regenerates:
- `python/tests/fixtures/crypto_vectors.json`
- `rust/totalreclaw-core/tests/fixtures/crypto_vectors.json`
- `rust/totalreclaw-memory/tests/fixtures/crypto_vectors.json`

After regenerating, re-run:
```bash
cd tests/parity && node --experimental-strip-types cross-impl-test.ts
cd rust/totalreclaw-core && cargo test
cd python && python -m pytest tests/ -v
```

### 10f: OMBH validation test (`subgraph/tests/e2e-ombh-validation.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/subgraph/tests/e2e-ombh-validation.ts`

- Line 69: `const EMBEDDING_DIM = 384;`
- Line 452: `const lshHasher = new LSHHasher(keys.lshSeed, EMBEDDING_DIM);`

This test runs against a local Hardhat/Graph Node. Update:
```
Line 69: const EMBEDDING_DIM = 384;
      -> const EMBEDDING_DIM = 640;
```

### 10g: Gas measurement test (`subgraph/tests/gas-measurement.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/subgraph/tests/gas-measurement.ts`

- Line 178: `A 384-dim float32 embedding = 384 * 4 = 1,536 bytes.`
- Line 183: `const embeddingBytes = 384 * 4; // float32 x 384 dims`
- Line 316: `lines.push('| Embedding dims | 384 (float32) |');`

Update all three:
```
Line 178: "A 640-dim float32 embedding = 640 * 4 = 2,560 bytes."
Line 183: const embeddingBytes = 640 * 4; // float32 x 640 dims
Line 316: lines.push('| Embedding dims | 640 (float32) |');
```

### 10h: Scaling analysis (`subgraph/tests/scaling-analysis.ts`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/subgraph/tests/scaling-analysis.ts`

- Line 570: References "384-dim float32 embeddings"
  -> Update to "640-dim"

### 10i: Gas report (`subgraph/tests/gas-report.md`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/subgraph/tests/gas-report.md`

- Line 15: `| Embedding dims | 384 (float32) |`
  -> `| Embedding dims | 640 (float32) |`

### 10j: Infrastructure test (`tests/test_infrastructure.py`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/tests/test_infrastructure.py`

- Line 59-60: `np.zeros(384, dtype=np.float32)` -> `np.zeros(640, dtype=np.float32)`
- Line 89: `np.zeros(384, dtype=np.float32)` -> `np.zeros(640, dtype=np.float32)`
- Line 166: Comment about "384 dimensions" -> "640 dimensions"

### 10k: Python embedding test (`python/tests/test_embedding.py`)

**File:** `/Users/pdiogo/Documents/code/totalreclaw/python/tests/test_embedding.py`

- Line 3: `"~34MB for e5-small, ~164MB for Harrier"` -> `"~344MB for Harrier q4"`

The test itself is dynamically driven by `get_embedding_dims()` so the
assertions will automatically work with the new dimension. No other changes
needed.

---

## Task 11: Update Documentation

### 11a: CLAUDE.md

**File:** `/Users/pdiogo/Documents/code/totalreclaw/CLAUDE.md`

Multiple references:

1. **Line 55** (Architecture diagram):
   ```
   |  Decrypt candidates -> e5-small 384d embeds -> BM25+Cosine+RRF -> Top 8 |
   ```
   -> `Decrypt candidates -> Harrier 640d embeds -> BM25+Cosine+RRF -> Top 8`

2. **Line 274** (Known Technical Gaps table):
   ```
   | Embedding model | RESOLVED | Migrated to Xenova/multilingual-e5-small (384d, ~34MB, mean pooling). Harrier-OSS-v1-270M (640d) blocked by ONNX runtime incompatibility (GatherBlockQuantized op) -- revisit when @huggingface/transformers upgrades ONNX Runtime to 1.25+. |
   ```
   -> `| Embedding model | RESOLVED | Migrated to onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4). |`

3. **Line 294** (Key Constraints):
   ```
   - **Embedding model**: Xenova/multilingual-e5-small (384d, ~34MB, mean pooling)
   ```
   -> `- **Embedding model**: onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4, pre-pooled)`

4. **Line 398** (Current Status):
   ```
   - **Embedding model**: Xenova/multilingual-e5-small (384d, ~34MB, mean pooling). Harrier-OSS-v1-270M (640d) blocked by ONNX runtime incompatibility...
   ```
   -> `- **Embedding model**: onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4, pre-pooled). e5-small (384d, ~34MB) available as fallback via TOTALRECLAW_EMBEDDING_MODEL=small.`

### 11b: Guides

**`docs/guides/claude-code-setup.md`:**
- Line 11: `~34 MB disk space` -> `~344 MB disk space`
- Line 171: `~34 MB` -> `~344 MB`

**`docs/guides/hermes-setup.md`:**
- Line 10: `~34 MB disk space` -> `~344 MB disk space`
- Line 61: `~34 MB, one-time` -> `~344 MB, one-time`
- Line 184: `~34 MB (e5-small model)` -> `~344 MB (Harrier model)`

**`docs/guides/beta-tester-guide.md`:**
- Line 53: `~34MB embedding model` -> `~344MB embedding model`

### 11c: Roadmap

**`docs/ROADMAP.md`:**
- Line 294: `Switched to multilingual-e5-small (384d, 34MB)` -> Update to reflect Harrier migration

### 11d: Plugin config comment

**`skill/plugin/config.ts`:**
- Line 73: `// Embedding model: "default" (640d, fp16 ~553MB)...`
  -> `// Embedding model: "default" (640d, q4 ~344MB), "small" (384d, q8 ~34MB), or "large" (1024d, q8 ~600MB)`

### 11e: TEE spec

**`docs/specs/tee/tdx-saas.md`:**
- Line 55: `embedding float[384]` -> `embedding float[640]`

---

## Task 12: E2E Testing

### 12a: Cross-client parity (embedding output)

Before any deployment, verify Python and TypeScript produce **identical**
(or near-identical, within floating-point tolerance) embeddings for the
same input text using the q4 Harrier model.

```bash
# Create a parity check script that:
# 1. Embeds "Pedro is the founder of TotalReclaw" in both TS and Python
# 2. Compares the 640d vectors element-by-element (tolerance: 1e-4)
# 3. Computes cosine similarity (should be > 0.999)
```

**This is the most critical validation.** If TS and Python produce different
embeddings, LSH bucket hashes will differ and cross-agent recall will fail.

Known risk: `@huggingface/transformers` (TS) and raw `onnxruntime` (Python)
may use different quantization rounding or different tokenizers. The spike
(Task 0) should surface this.

### 12b: OpenClaw E2E

```bash
# Using staging relay (api-staging.totalreclaw.xyz)
# 1. Install plugin
# 2. Setup with recovery phrase
# 3. Store a fact ("Pedro's favorite color is blue")
# 4. New session: recall ("What is Pedro's favorite color?")
# 5. Verify recalled text matches
```

### 12c: MCP E2E

```bash
cd ../totalreclaw-internal/e2e
npm run test:mcp
```

### 12d: Hermes E2E

```bash
# 1. pip install totalreclaw[dev]
# 2. Configure with same recovery phrase as OpenClaw
# 3. Store a fact
# 4. Recall the fact
```

### 12e: Cross-agent recall

**Critical test:** Store on OpenClaw, recall on Hermes (same recovery phrase).

This validates:
1. Same wallet address derivation (already works)
2. Same encryption keys (already works)
3. Same LSH bucket hashes (depends on identical embeddings -- Task 12a)
4. Same blind indices (already works)
5. Successful decryption and reranking

```bash
# 1. Store "Pedro prefers dark mode" via OpenClaw plugin
# 2. Recall "What mode does Pedro prefer?" via Hermes Python client
# 3. Verify the fact is found and decrypted correctly
```

### 12f: Dimension check in stored data

After storing a fact via the new model, verify the encrypted embedding
decrypts to a 640d vector (not 384d):

```bash
# After storing, export facts and check:
# - Decrypted embedding length === 640
# - L2 norm approximately 1.0
```

---

## Task 13: Config and Env Var Updates

The OpenClaw plugin already supports `TOTALRECLAW_EMBEDDING_MODEL` env var
with values `default`, `small`, `large`. After this migration:

- `default` = Harrier q4 (640d, ~344MB)
- `small` = e5-small (384d, ~34MB) -- fallback for low-resource environments
- `large` = Qwen3-Embedding-0.6B (1024d, ~600MB) -- legacy

Document this in the setup guides so users on constrained machines can
set `TOTALRECLAW_EMBEDDING_MODEL=small`.

**The MCP server does NOT currently support model selection** -- it's
hardcoded. Consider adding `TOTALRECLAW_EMBEDDING_MODEL` support to
`mcp/src/subgraph/embedding.ts` for parity with the plugin. This is
a nice-to-have, not a blocker.

---

## Execution Order

```
Task 0: Spike (TS + Python q4 verification)
        |
        v
Tasks 1-4: TypeScript changes (MCP, plugin, client) -- can be parallel
Task 5: Python embedding changes
Task 6: Python LSH (verify no hardcoded dims)
Task 7: Rust core docstring/test updates
Task 8: Rust ZeroClaw (verify already 640d)
Task 9: Package version bumps
        |
        v
Task 10: Test updates (all in parallel)
Task 11: Documentation updates (all in parallel)
        |
        v
Task 12a: Cross-client embedding parity check (BLOCKING)
Task 12b-d: Platform E2E tests (can be parallel)
Task 12e: Cross-agent recall test (BLOCKING for ship)
Task 12f: Dimension verification
        |
        v
Task 13: Config/env var documentation
```

---

## Rollback Plan

If the q4 model causes issues (accuracy regression, runtime crashes, or
cross-client parity failures):

1. Revert all `MODEL_ID` / `EMBEDDING_DIM` / `dtype` changes
2. Keep the version bumps for onnxruntime (1.24+ is fine for e5-small too)
3. Re-run E2E tests to confirm e5-small still works

Since there are no production users, no data migration is needed for
rollback. Testnet data can be discarded.

---

## File Change Summary

| File | Change | Lines |
|------|--------|-------|
| `skill/plugin/embedding.ts` | Default model -> Harrier q4, pooling, dtype | 28-50, 67-87 |
| `skill/plugin/config.ts` | Comment update | 73 |
| `client/src/embedding/onnx.ts` | Model, dims, dtype, pooling | 1-25, 37-48, 79-90, 134, 177 |
| `client/src/index.ts` | LSH dims 384 -> 640 | 133-134, 145-146 |
| `mcp/src/subgraph/embedding.ts` | Model, dims, dtype, pooling, comments | 1-68 (entire file) |
| `python/src/totalreclaw/embedding.py` | ONNX filename to q4, docstring | 1-12, 38-41 |
| `python/pyproject.toml` | onnxruntime >= 1.24 | 16 |
| `skill/package.json` | onnxruntime-node >= 1.24 | 27 |
| `rust/totalreclaw-core/src/wasm.rs` | Docstring "e.g. 1024" -> "e.g. 640" | 157 |
| `rust/totalreclaw-core/src/python.rs` | Docstring "e.g. 1024" -> "e.g. 640" | 151 |
| `rust/totalreclaw-core/src/search.rs` | Test dims 1024 -> 640 | 442, 444 |
| `rust/totalreclaw-core/src/store.rs` | Test dims 1024 -> 640 | 282, 288, 503 |
| `tests/parity/parity-test.ts` | Cosine test dims 384 -> 640 | 405-411 |
| `tests/parity/generate-fixtures.ts` | REAL_DIMS 1024 -> 640 (optional) | 119 |
| `subgraph/tests/e2e-ombh-validation.ts` | EMBEDDING_DIM 384 -> 640 | 69 |
| `subgraph/tests/gas-measurement.ts` | Embedding bytes calculation | 178, 183, 316 |
| `subgraph/tests/gas-report.md` | Dims in table | 15 |
| `subgraph/tests/scaling-analysis.ts` | Comment update | 570 |
| `tests/test_infrastructure.py` | np.zeros dims 384 -> 640 | 59-60, 89, 166 |
| `python/tests/test_embedding.py` | Comment update | 3 |
| `CLAUDE.md` | Multiple references to e5-small/384d | 55, 274, 294, 398 |
| `docs/guides/claude-code-setup.md` | Model size references | 11, 171 |
| `docs/guides/hermes-setup.md` | Model size references | 10, 61, 184 |
| `docs/guides/beta-tester-guide.md` | Model size reference | 53 |
| `docs/ROADMAP.md` | Embedding model row | 294 |
| `docs/specs/tee/tdx-saas.md` | Vec dimension | 55 |

**Total: ~27 files, mostly small constant/comment changes.**

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| q4 variant still fails on some platforms | HIGH | Task 0 spike validates before any code changes |
| TS and Python produce different embeddings | HIGH | Task 12a cross-client parity test blocks ship |
| 344MB download too slow for first-time users | MEDIUM | Document in guides; offer `TOTALRECLAW_EMBEDDING_MODEL=small` fallback |
| `@huggingface/transformers` doesn't support q4 dtype | MEDIUM | Spike will surface this; fallback to fp16 (~553MB) |
| On-chain payload size increase (640d vs 384d) | LOW | 640*4=2560 bytes vs 384*4=1536. ~1KB increase per fact. Gas cost negligible on Base Sepolia/Gnosis. |
| Cross-impl fixture regeneration breaks tests | LOW | Regenerate and re-run all parity tests in Task 10e |
