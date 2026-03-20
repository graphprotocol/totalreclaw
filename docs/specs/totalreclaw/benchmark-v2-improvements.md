<!--
Product: TotalReclaw
Version: Draft
Last updated: 2026-02-27
-->

# OMBH v2 Benchmark — Improvements over v1

**Status:** Draft — learnings from v1 benchmark run  
**Goal:** Make the benchmark closer to real-world OpenClaw usage patterns

---

## What v1 Does Well

- ✅ Same LLM (glm-5) across all instances — isolates memory system quality
- ✅ 4-way comparison (TotalReclaw, Mem0, QMD, LanceDB)
- ✅ GPT-4.1 Mini ground truth — high quality fact extraction
- ✅ 981 synthetic conversations, 8,268 facts, 3,308 queries
- ✅ Docker-based isolation with clean state per run
- ✅ Cross-conversation queries (connecting facts across sessions)

## v1 Gaps — What Real Usage Looks Like vs What We Test

### Gap 1: Single-Shot vs Multi-Session

**Real life:** A user has 50+ conversations with their agent over weeks/months. Memory accumulates incrementally. Each session adds a few facts. The agent must recall facts from session 3 during session 47.

**v1:** We dump an entire conversation as one big message array. The memory plugin sees it once and extracts everything at once. No incremental accumulation, no session boundaries.

**v2 fix:**
- Replay conversations turn-by-turn across multiple API calls
- Insert session breaks (new chat thread) every 5-10 turns
- Test recall at session boundaries ("What did I tell you last time?")
- Track memory accuracy as a function of session distance (does recall degrade for older sessions?)

### Gap 2: Fact Evolution & Contradiction

**Real life:** "I work at Google" → 6 months later → "I just joined Meta." Memory systems must supersede old facts, not stack them.

**v1:** All facts are independent. No fact contradicts another.

**v2 fix:**
- Generate 10-20% of conversations with explicit fact updates:
  - Job changes, location moves, preference reversals
  - "Actually, I changed my mind about X"
- Test: Does the system return the LATEST fact or the old one?
- New metric: **Freshness@K** — fraction of retrieved facts that reflect the most recent state

### Gap 3: Compaction Survival

**Real life:** OpenClaw compacts conversation history when context grows too long. The `before_compaction` hook is the memory plugin's last chance to extract facts before context is lost.

**v1:** No compaction pressure. Full conversation always available.

**v2 fix:**
- After every 20 turns, trigger a `/compact` command via WebSocket
- Then continue the conversation — the agent loses older context
- Test: Can the system still recall facts from before compaction?
- New metric: **Post-Compaction Recall** — recall accuracy for pre-compaction facts

### Gap 4: Scoring Methodology

**Real life:** Memory recall quality is semantic, not lexical. "The user likes sourdough baking" and "They enjoy making artisan bread" are functionally equivalent.

**v1:** Keyword overlap scorer. Misses semantic matches, inflates false negatives.

**v2 fix:**
- **Primary scorer:** LLM judge (GPT-4.1 Mini) — ask: "Does this response contain information equivalent to [fact text]? Score 0-1."
- **Secondary scorer:** Embedding cosine similarity between response and expected fact
- **Tertiary scorer:** Keyword overlap (fast baseline, for debugging)
- Run LLM judge on a stratified 20% sample to keep costs manageable
- New metric: **Hallucination Rate** — facts stated confidently that don't match any ground truth

### Gap 5: Negative & Adversarial Queries

**Real life:** Users ask about things the agent doesn't know. Good memory systems say "I don't know." Bad ones hallucinate.

**v1:** Only 2 negative queries (0.1%) due to prompt generation bug.

**v2 fix:**
- Dedicated negative query generation pass: 15-20% of all queries
- Categories:
  - **Plausible negatives:** Topics similar to stored facts but not actually discussed
  - **Temporal negatives:** "What did I say about X last week?" when X was actually discussed 3 months ago
  - **Confused identity:** Facts about persona A asked in persona B's session (tests isolation)
- New metric: **False Positive Rate** — fraction of negative queries that get a confident (wrong) answer

### Gap 6: Conversation Realism

**Real life:** Real conversations have:
- Interruptions, topic changes, tangents
- Ambiguous or implicit facts ("the usual place" = "Starbucks on 5th")
- Emotional context ("I'm stressed about the deadline")
- Meta-conversation ("remember what I told you last time?")

**v1:** Clean, topical conversations. Facts are explicit and unambiguous.

**v2 fix:**
- Conversation generation prompts that include:
  - Topic pivots mid-conversation
  - References to previous sessions ("as I mentioned before")
  - Implicit facts that require inference
  - Small talk mixed with factual content
- Test: Can the system extract implicit facts? Can it handle noisy context?

### Gap 7: Scale Testing

**Real life:** Power users accumulate 10,000+ memories over months.

**v1:** 50 conversations ingested per benchmark run. Memory pool is tiny.

**v2 fix:**
- Tiered runs: 50, 200, 500, 1000 conversations
- Measure how recall degrades with memory pool size
- Measure how latency scales
- New metric: **Recall@K vs Pool Size** curve

### Gap 8: Privacy Verification

**Real life:** TotalReclaw's key differentiator is end-to-end encryption. We should PROVE the server can't see plaintext.

**v1:** Privacy score is a static constant (100 for TotalReclaw, 0 for others).

**v2 fix:**
- **Server-side inspection:** After ingest, dump the TotalReclaw server's DB and verify:
  - No plaintext facts in `raw_events` or `facts` tables
  - Encrypted blobs are not deterministic (same fact = different ciphertext)
  - Blind indices don't leak fact content
- **Network sniff:** Capture HTTP traffic between plugin and server, verify no plaintext
- New metric: **Privacy Audit Score** — automated verification with evidence

---

## v2 Architecture Changes

### Multi-Session Replay Engine
```
For each conversation:
  For each session (5-10 turns):
    1. Start new chat thread (POST /v1/chat/completions with fresh messages)
    2. Replay user turns one at a time
    3. After each turn, wait for agent response
    4. At session boundary: trigger compaction (optional)
    5. After all sessions: run recall queries
```

### LLM Judge Scorer
```
For each (query, response, expected_facts):
  Prompt GPT-4.1 Mini:
    "Given query: {query}
     Expected facts: {facts}
     System response: {response}
     
     For each expected fact, score 0-1 whether the response contains
     equivalent information. Also flag any facts in the response that
     don't match any expected fact (hallucination).
     
     Output JSON: {fact_scores: [{fact_id, score}], hallucinated_facts: [...]}"
```

### Freshness Scorer
```
For each fact with a superseding update:
  Query the system about the updated topic
  Check: Does it return the NEW fact or the OLD one?
  Score: 1 if new, 0 if old, 0.5 if both
```

---

## v2 Metrics Summary

| Metric | v1 | v2 |
|--------|----|----|
| Recall@K | ✅ keyword | ✅ LLM judge + embedding |
| Precision@K | ✅ keyword | ✅ LLM judge |
| MRR | planned | ✅ |
| Latency p50/p95 | ✅ | ✅ |
| Freshness@K | ❌ | ✅ |
| Post-Compaction Recall | ❌ | ✅ |
| Hallucination Rate | ❌ | ✅ |
| False Positive Rate | ❌ (2 queries) | ✅ (15-20% queries) |
| Recall vs Pool Size | ❌ | ✅ |
| Privacy Audit | ❌ (static) | ✅ (automated) |
| Downstream Quality | ❌ | ✅ (LLM judge) |

---

## Estimated v2 Effort

| Component | Effort | Priority |
|-----------|--------|----------|
| Multi-session replay engine | 2-3 days | P0 |
| LLM judge scorer | 1 day | P0 |
| Fact evolution scenarios | 1 day | P1 |
| Compaction testing | 1 day | P1 |
| Negative query generator | 0.5 day | P1 |
| Scale testing (50→1000) | 0.5 day | P2 |
| Privacy audit | 1 day | P2 |
| Conversation realism | 1 day | P3 |

**Total: ~8-10 days for full v2**

---

## v2 Cost Estimate

| Phase | Tokens | Model | Cost |
|-------|--------|-------|------|
| Conversation gen (1000) | ~10M | Llama 3.3 70B | ~$1.20 |
| Fact extraction | ~2.5M | GPT-4.1 Mini | ~$1.52 |
| Query generation | ~2.5M | GPT-4.1 Mini | ~$1.52 |
| LLM judge (20% sample) | ~3M | GPT-4.1 Mini | ~$1.80 |
| Embeddings (LanceDB) | ~0.5M | text-embedding-3-small | ~$0.01 |
| LLM calls (4 instances × 1000 convs) | ~250M | glm-5 (Z.AI) | Z.AI quota |
| **Total (excl. Z.AI)** | | | **~$6.05** |
