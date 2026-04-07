# Conversation Import — Production Design Notes

**Context:** Bulk import of conversation history (Gemini, ChatGPT, Claude) into TotalReclaw vaults. Validated locally with Gemini Takeout (~3,500 conversations → ~7K facts).

---

## Batching for Bulk Imports

The current `MAX_BATCH_SIZE = 15` in `client/src/userop/batcher.ts` is tuned for the live extraction cycle (max 15 facts per `agent_end` hook). For bulk imports, this is too conservative.

### Recommended: Import Batch Size = 50

| Batch Size | UserOps for 7K facts | Gas per UserOp | Total Gas | Pimlico quota % |
|-----------|---------------------|-----------------|-----------|-----------------|
| 15 (current) | 467 | ~47,500 | ~22M | 0.9% of 50K/mo |
| **50** | **140** | ~135,000 | ~19M | **0.3%** |
| 100 | 70 | ~265,000 | ~18.5M | 0.1% |

**Why 50, not 100:**
- Conservative margin for Gnosis block gas limit (30M)
- Pimlico paymaster may have per-UserOp gas caps
- 140 UserOps is already very comfortable (0.3% of monthly quota)
- Diminishing returns — gas savings from 50→100 are minimal (~3%)

### Implementation

Add an `IMPORT_BATCH_SIZE` constant or parameter to the import tool:

```typescript
// In the import tool handler (plugin + MCP):
const batchSize = isImport ? 50 : MAX_BATCH_SIZE; // 50 for imports, 15 for live extraction
```

The existing `sendBatchOnChain()` in `batcher.ts` already supports arbitrary batch sizes up to the validated limit. Just increase `MAX_BATCH_SIZE` for import contexts or add a separate constant.

### Cost to User

For a ~7K fact Gemini import on Gnosis mainnet:
- **Gas**: ~$0.04 (Pimlico-sponsored, $0 to user)
- **Subscription**: $3.99/month Pro tier
- **LLM extraction**: User's own model/key (local Ollama = free)
- **Total**: $3.99/month — the import itself is essentially free

---

## Production Import Flow

```
1. User uploads Gemini/ChatGPT/Claude export file
2. Client-side: parse → chunk into sessions → LLM extraction (user's model)
3. Client-side: encrypt facts (AES-256-GCM) + generate embeddings (e5-small)
4. Client-side: encode protobuf with ORIGINAL conversation timestamps
5. Client-side: batch 50 facts per UserOp → submit via relay/Pimlico
6. Subgraph indexes with per-fact `createdAt` from protobuf field 2
7. Facts are searchable alongside live-extracted facts (same embeddings, same indices)
```

### Key Differences from Live Extraction

| Aspect | Live Extraction | Bulk Import |
|--------|----------------|-------------|
| Batch size | 15 facts/UserOp | 50 facts/UserOp |
| Timestamp | `Date.now()` | Original conversation time |
| Source field | `conversation` / `pre_compaction` | `gemini-import` / `chatgpt-import` |
| Trigger | `agent_end` hook | User-initiated tool call |
| Dedup | Store-time cosine + LLM-guided | Content fingerprint + text dedup |

### Adapter Pattern

Reuse the existing `BaseImportAdapter` pattern in `skill/plugin/import-adapters/`:
- `gemini-adapter.ts` — HTML parser + temporal session grouping (built, tested)
- `chatgpt-adapter.ts` — already exists
- `claude-adapter.ts` — already exists

Add `'gemini'` to the `ImportSource` type and register in the adapter factory.
