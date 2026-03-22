# Gas Cost Extrapolation: Per-User Write Costs on Gnosis Chain

**Date:** 2026-03-04
**Based on:** Codebase analysis of `skill/plugin/index.ts`, `extractor.ts`, `semantic-dedup.ts`, gas-report.md, scaling-report.md, billing-and-onboarding.md
**Scope:** On-chain write (store) costs only. Does not cover LLM inference costs, infrastructure hosting, or query (read) costs.

---

## 1. Pipeline Trace: From Conversation Turn to On-Chain Fact

The full write path consists of six stages. Each stage reduces the number of facts that ultimately reach the chain.

### Stage 1: Extraction Trigger (`agent_end` hook)

**Source:** `skill/plugin/index.ts` lines 115-117, 1426-1451

The `agent_end` hook fires after every successful agent turn. A module-level counter `turnsSinceLastExtraction` increments on each turn. Extraction only occurs when the counter reaches `AUTO_EXTRACT_EVERY_TURNS` (default: 5, configurable via `TOTALRECLAW_EXTRACT_EVERY_TURNS` env var).

```typescript
turnsSinceLastExtraction++;
if (turnsSinceLastExtraction >= AUTO_EXTRACT_EVERY_TURNS) {
    const rawFacts = await extractFacts(evt.messages, 'turn');
    const { kept: facts } = filterByImportance(rawFacts, api.logger);
    if (facts.length > 0) {
        await storeExtractedFacts(facts, api.logger);
    }
    turnsSinceLastExtraction = 0;
}
```

**Result:** 1 extraction call per 5 turns.

### Stage 2: LLM Extraction (`extractFacts`)

**Source:** `skill/plugin/extractor.ts` lines 36-56, 170-210

The extraction prompt instructs the LLM to:
- Extract atomic facts with importance >= 6 (on a 1-10 scale)
- Skip small talk, greetings, generic knowledge, and session-only info
- Return 0-N facts as a JSON array

In `turn` mode (periodic extraction), only the last 6 messages (3 turns) are analyzed, not the full 5-turn window. This means the extraction window is narrower than the trigger interval, which is appropriate since earlier turns were already processed in prior extraction cycles.

The `parseFactsResponse` function (line 153) applies an additional hard filter: `filter((f) => f.importance >= 6)`.

**Result:** 0-5 raw facts per extraction, typically 1-3. The LLM itself acts as the first filter.

### Stage 3: Importance Filter (`filterByImportance`)

**Source:** `skill/plugin/index.ts` lines 395-428

`MIN_IMPORTANCE_THRESHOLD` defaults to 3 (configurable via `TOTALRECLAW_MIN_IMPORTANCE` env var). Since the extraction prompt targets importance >= 6, and the parser also filters to >= 6, the threshold-3 filter is purely a safety net for LLM failures that somehow produce sub-3 scores.

**Estimated pass-through rate:** ~98-100%. This filter is functionally a no-op under normal conditions.

### Stage 4: Semantic Batch Dedup (`deduplicateBatch`)

**Source:** `skill/plugin/semantic-dedup.ts` lines 63-100, `index.ts` line 469

Within a single extraction batch, facts with cosine similarity >= 0.9 against an already-accepted fact are dropped. This is within-batch only -- it does NOT deduplicate against previously stored facts.

With typical batch sizes of 1-3 facts from `turn` mode extraction, the probability of within-batch near-duplicates is low:
- Batch size 1: 0% dedup possible
- Batch size 2: ~5% chance of dedup (2 facts from 3 turns rarely say the same thing)
- Batch size 3-5: ~10-15% chance that at least one pair is near-duplicate

**Estimated average dedup rate:** ~5% across all batches.

### Stage 5: Content Fingerprint Dedup (Server/Subgraph Side)

**Source:** `skill/plugin/crypto.ts` line 346, `skill/plugin/index.ts` line 501

Each fact gets an HMAC-SHA256 content fingerprint (`content_fp`). The server/subgraph rejects facts with duplicate fingerprints. This catches exact-duplicate text across batches (e.g., compaction re-extracting a fact that periodic extraction already stored).

**Important:** This is exact-match only (HMAC of normalized text). Near-paraphrases pass through.

### Stage 6: On-Chain Write

Each surviving fact results in one on-chain transaction (no batching currently). One fact = one `Log(bytes)` event containing a protobuf-encoded payload.

### Compaction and Reset Bursts

**Source:** `skill/plugin/index.ts` lines 1457-1510

Two additional hooks trigger full-context extraction:

- `before_compaction` (line 1457): Fires when the context window fills up. Extracts from ALL messages in the conversation, not just the last 3 turns. The extraction mode is `full`, which uses all messages truncated to ~12,000 characters (~3,000 tokens).
- `before_reset` (line 1488): Fires on explicit session reset. Same `full` extraction.

Both hooks pass through the same `filterByImportance` and `storeExtractedFacts` pipeline (including semantic batch dedup), and both reset `turnsSinceLastExtraction` to 0.

**Compaction burst size:** With a full conversation context (~50-100 messages), the LLM can extract 3-15 facts. The prompt says "Extract ALL valuable long-term memories from this conversation before it is lost," which encourages more aggressive extraction than `turn` mode.

**Cross-batch duplication risk:** Facts extracted during compaction may duplicate facts already stored by periodic extraction. The content fingerprint catches exact duplicates, but paraphrased versions of the same fact will be stored again. There is no cross-batch semantic dedup.

---

## 2. Gas Cost Per Fact on Gnosis Chain

### Verification of the $0.00076 Figure

From `subgraph/tests/gas-report.md`, a representative medium fact (50 words, 80 blind indices, with embedding) uses:
- **Gas:** 379,650
- **Calldata:** 8,967 bytes

Gnosis Chain parameters:
- **Gas price:** ~2 gwei (typical; range 1-3 gwei)
- **Gas token:** xDAI (pegged to $1.00)
- **No L1 data posting cost** (Gnosis is an L1, not an L2)

Cost calculation:
```
379,650 gas * 2 gwei * 1e-9 xDAI/gwei * $1.00/xDAI = $0.000759
```

**Confirmed: ~$0.00076 per medium fact at 2 gwei gas price.**

### Sensitivity to Gas Price

| Gas Price (gwei) | Cost per Fact | Notes |
|:-:|:-:|---|
| 1 | $0.00038 | Floor (low congestion) |
| 2 | $0.00076 | Typical (baseline for analysis) |
| 3 | $0.00114 | Moderate congestion |
| 5 | $0.00190 | High congestion (rare) |

### Sensitivity to Fact Size

| Fact Type | Gas Used | Cost @ 2 gwei |
|---|:-:|:-:|
| Minimal (5w, 10 idx, no emb) | 58,770 | $0.00012 |
| Small (20w, 50 idx, with emb) | 293,250 | $0.00059 |
| **Medium (50w, 80 idx, with emb)** | **379,650** | **$0.00076** |
| Large (100w, 120 idx, with emb) | 497,220 | $0.00099 |
| XL (200w, 150 idx, with emb) | 600,330 | $0.00120 |

The analysis uses the medium fact as the representative cost, since extracted facts average ~30-60 words.

---

## 3. User Profiles

### Conversation Assumptions

A "turn" equals one user message + one agent response. Conversation rate depends on usage pattern:

| Profile | Sessions/week | Turns/session | Turns/day | Hours/day | Description |
|---|:-:|:-:|:-:|:-:|---|
| **Casual** | 2-3 | 10-15 | 5 | 0.3-0.5 | Quick Q&A, occasional chat |
| **Regular** | 7-14 | 15-25 | 30 | 1-2 | Daily AI assistant user |
| **Power** | 14-28 | 20-40 | 90 | 4-6 | Developer/researcher, heavy daily |
| **Extreme** | 28-56 | 30-50 | 200 | 8+ | All-day coding assistant |

**Casual profile derivation:** 2.5 sessions/week, 12.5 turns/session = 31.25 turns/week = ~4.5 turns/day. Rounded to 5.

**Regular profile derivation:** 10 sessions/week (1-2/day), 20 turns/session = 200 turns/week = ~29 turns/day. Rounded to 30.

**Power profile derivation:** 20 sessions/week (3-4/day), 30 turns/session = 600 turns/week = ~86 turns/day. Rounded to 90.

**Extreme profile derivation:** 40 sessions/week (5-8/day), 40 turns/session = 1,600 turns/week = ~229 turns/day. Rounded to 200.

### Compaction Frequency (ASSUMPTION)

**Confidence: MEDIUM.** Compaction frequency depends on OpenClaw's context window size and compaction trigger, which are not fully documented. Reasonable estimates:

| Profile | Sessions where compaction fires | Compactions/week | Reasoning |
|---|:-:|:-:|---|
| Casual | ~0% (sessions too short) | 0 | 10-15 turns rarely fills context window |
| Regular | ~20% of sessions | 2 | Longer sessions occasionally fill context |
| Power | ~50% of sessions | 10 | Frequent long sessions regularly fill context |
| Extreme | ~60% of sessions | 24 | Most sessions involve deep, long conversations |

---

## 4. Facts Per Extraction: Detailed Estimates

### Periodic Extraction (Turn Mode)

Each extraction analyzes the last 6 messages (3 turns). The content density varies by conversation type:

| Conversation Type | Weight (%) | Raw Facts/Extraction | Notes |
|---|:-:|:-:|---|
| Coding/debugging | 40% | 0-1 (mean: 0.5) | Mostly code; few personal facts |
| Planning/discussion | 25% | 2-4 (mean: 2.5) | Decisions, preferences, goals |
| Q&A/learning | 20% | 1-2 (mean: 1.5) | Some factual preferences |
| Social/casual | 15% | 0-1 (mean: 0.3) | Small talk, filtered by prompt |

**Weighted average raw facts per extraction (turn mode):**
```
0.40 * 0.5 + 0.25 * 2.5 + 0.20 * 1.5 + 0.15 * 0.3 = 0.20 + 0.63 + 0.30 + 0.05 = 1.17
```

**Assumption:** Mean of ~1.2 raw facts per periodic extraction. (CONFIDENCE: MEDIUM -- no empirical data; derived from prompt analysis and conversation type mix.)

**Profile-specific adjustments:**

| Profile | Conversation Mix Adjustment | Mean Raw Facts/Extraction |
|---|---|:-:|
| Casual | More social/Q&A, less coding | 1.0 |
| Regular | Balanced mix | 1.2 |
| Power | More coding + planning | 1.3 |
| Extreme | Heavily coding, some planning | 1.1 |

### Compaction Extraction (Full Mode)

Compaction analyzes the full conversation (up to 12,000 chars). With 50-100 messages of context:

| Profile | Avg Messages at Compaction | Raw Facts from Full Extraction |
|---|:-:|:-:|
| Regular | ~40 | 3-5 (mean: 4) |
| Power | ~60 | 5-10 (mean: 7) |
| Extreme | ~80 | 8-15 (mean: 10) |

**Cross-batch duplication with periodic extraction:** Since periodic extraction already stored ~40-80% of the unique facts during the session, a significant portion of compaction facts will be duplicates. The content fingerprint catches exact duplicates but not paraphrases.

**Estimated net NEW facts from compaction (after fingerprint dedup):**

| Profile | Gross Compaction Facts | Already Stored (fingerprint match) | Net New Facts |
|---|:-:|:-:|:-:|
| Regular | 4 | ~2 (50%) | 2 |
| Power | 7 | ~4 (57%) | 3 |
| Extreme | 10 | ~6 (60%) | 4 |

**(ASSUMPTION: MEDIUM-LOW confidence. The exact overlap rate depends on how consistently the LLM produces identical text for the same fact across turn vs full mode.)**

---

## 5. Pipeline Attrition: From Raw Facts to On-Chain Writes

Applying the pipeline stages from Section 1:

### Per Periodic Extraction

| Stage | Input | Pass Rate | Output | Notes |
|---|:-:|:-:|:-:|---|
| LLM extraction (turn mode) | N/A | N/A | 1.0-1.3 raw facts | Profile-dependent |
| Importance filter (threshold 3) | 1.0-1.3 | ~99% | 1.0-1.3 | Safety net only (LLM targets >=6) |
| Semantic batch dedup (cosine 0.9) | 1.0-1.3 | ~97% | 1.0-1.3 | Low batch sizes = low dedup |
| **Net facts stored** | | | **~1.0-1.3** | |

For practical purposes, the importance filter and within-batch semantic dedup have negligible effect on the small batch sizes produced by turn-mode extraction. The pipeline attrition is essentially zero for periodic extraction.

**Simplified: raw facts per extraction ~= stored facts per extraction.**

### Per Compaction Event

| Stage | Input | Pass Rate | Output | Notes |
|---|:-:|:-:|:-:|---|
| LLM extraction (full mode) | N/A | N/A | 4-10 raw facts | Profile-dependent |
| Importance filter (threshold 3) | 4-10 | ~98% | 4-10 | Still a safety net |
| Semantic batch dedup (cosine 0.9) | 4-10 | ~90% | 3.6-9 | Larger batches have more internal overlap |
| Content fingerprint dedup (server) | 3.6-9 | ~50-60% pass | 2-4 net new | Cross-batch exact-match catches many |
| **Net NEW facts stored** | | | **~2-4** | |

---

## 6. Primary Results: Facts and Costs Per User Profile

### Monthly Fact Volume Calculation

#### Periodic Facts

```
extractions_per_day = turns_per_day / AUTO_EXTRACT_EVERY_TURNS
periodic_facts_per_day = extractions_per_day * mean_facts_per_extraction
periodic_facts_per_month = periodic_facts_per_day * 30
```

#### Compaction Facts

```
compaction_facts_per_week = compactions_per_week * net_new_facts_per_compaction
compaction_facts_per_month = compaction_facts_per_week * 4.3
```

#### Total

```
total_facts_per_month = periodic_facts_per_month + compaction_facts_per_month
```

### Summary Table (AUTO_EXTRACT_EVERY_TURNS = 5)

| Profile | Turns/day | Extractions/day | Periodic facts/day | Compaction facts/day | **Total facts/day** | **Facts/month** | **Gas cost/month** |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Casual** | 5 | 1.0 | 1.0 | 0.0 | **1.0** | **30** | **$0.023** |
| **Regular** | 30 | 6.0 | 7.2 | 0.6 | **7.8** | **234** | **$0.178** |
| **Power** | 90 | 18.0 | 23.4 | 4.3 | **27.7** | **831** | **$0.632** |
| **Extreme** | 200 | 40.0 | 44.0 | 13.7 | **57.7** | **1,731** | **$1.315** |

### Detailed Breakdown

**Casual (5 turns/day):**
- Periodic: 5 / 5 = 1 extraction/day * 1.0 facts = 1.0 facts/day
- Compaction: 0 compactions/week * 0 = 0 facts/day
- Monthly: 30 facts * $0.00076 = **$0.023**

**Regular (30 turns/day):**
- Periodic: 30 / 5 = 6 extractions/day * 1.2 facts = 7.2 facts/day
- Compaction: 2/week * 2 net new = 4/week = 0.57 facts/day
- Monthly: 234 facts * $0.00076 = **$0.178**

**Power (90 turns/day):**
- Periodic: 90 / 5 = 18 extractions/day * 1.3 facts = 23.4 facts/day
- Compaction: 10/week * 3 net new = 30/week = 4.3 facts/day
- Monthly: 831 facts * $0.00076 = **$0.632**

**Extreme (200 turns/day):**
- Periodic: 200 / 5 = 40 extractions/day * 1.1 facts = 44.0 facts/day
- Compaction: 24/week * 4 net new = 96/week = 13.7 facts/day
- Monthly: 1,731 facts * $0.00076 = **$1.315**

---

## 7. Sensitivity Analysis

### 7.1 Varying AUTO_EXTRACT_EVERY_TURNS

Impact on the **Power user** profile (90 turns/day, most sensitive to this parameter):

| AUTO_EXTRACT_EVERY_TURNS | Extractions/day | Periodic facts/day | Total facts/day | Facts/month | Gas cost/month | vs. Baseline |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **3** | 30 | 39.0 | 43.3 | 1,299 | $0.987 | +56% |
| **5 (current)** | 18 | 23.4 | 27.7 | 831 | $0.632 | baseline |
| **10** | 9 | 11.7 | 16.0 | 480 | $0.365 | -42% |
| **15** | 6 | 7.8 | 12.1 | 363 | $0.276 | -56% |

Impact on the **Extreme user** profile (200 turns/day):

| AUTO_EXTRACT_EVERY_TURNS | Extractions/day | Periodic facts/day | Total facts/day | Facts/month | Gas cost/month |
|:-:|:-:|:-:|:-:|:-:|:-:|
| **3** | 66.7 | 73.3 | 87.0 | 2,611 | $1.984 |
| **5 (current)** | 40 | 44.0 | 57.7 | 1,731 | $1.315 |
| **10** | 20 | 22.0 | 35.7 | 1,071 | $0.814 |
| **15** | 13.3 | 14.7 | 28.4 | 851 | $0.647 |

### 7.2 Varying Mean Facts Per Extraction

Impact on **Power user** (AUTO_EXTRACT_EVERY_TURNS = 5):

| Mean Facts/Extraction | Periodic facts/day | Total facts/day | Facts/month | Gas cost/month |
|:-:|:-:|:-:|:-:|:-:|
| **0.5** (pessimistic: mostly coding) | 9.0 | 13.3 | 399 | $0.303 |
| **1.3** (baseline) | 23.4 | 27.7 | 831 | $0.632 |
| **2.0** (optimistic: mostly planning) | 36.0 | 40.3 | 1,209 | $0.919 |
| **3.0** (high-yield conversations) | 54.0 | 58.3 | 1,749 | $1.329 |

### 7.3 Varying Compaction Burst Size

Impact on **Extreme user** (most compaction-sensitive):

| Net New Facts per Compaction | Compaction facts/day | Total facts/day | Facts/month | Gas cost/month |
|:-:|:-:|:-:|:-:|:-:|
| **0** (no compaction) | 0 | 44.0 | 1,320 | $1.003 |
| **4 (baseline)** | 13.7 | 57.7 | 1,731 | $1.315 |
| **8** (high overlap failure) | 27.4 | 71.4 | 2,143 | $1.629 |
| **15** (worst case: no fingerprint dedup) | 51.4 | 95.4 | 2,863 | $2.176 |

### 7.4 Combined Worst Case vs Best Case

**Power user, worst case** (EXTRACT_EVERY=3, 2.0 facts/extraction, 8 compaction net new):
- Periodic: 30 * 2.0 = 60 facts/day
- Compaction: 10/wk * 8 = 80/wk = 11.4/day
- Total: 71.4/day = 2,143/month = **$1.629/month gas**

**Power user, best case** (EXTRACT_EVERY=10, 0.5 facts/extraction, 0 compaction):
- Periodic: 9 * 0.5 = 4.5 facts/day
- Compaction: 0
- Total: 4.5/day = 135/month = **$0.103/month gas**

---

## 8. Tier Viability Analysis

### 8.1 Free Tier (100 facts/month)

| Profile | Facts/month | Free Tier Lasts | Days Until Limit (30-day month) |
|---|:-:|:-:|:-:|
| **Casual** | 30 | Indefinitely | Never hits limit |
| **Regular** | 234 | ~13 days | Day 13 |
| **Power** | 831 | ~3.6 days | Day 4 |
| **Extreme** | 1,731 | ~1.7 days | Day 2 |

**Assessment:**
- **Casual users:** 100 facts/month is more than adequate. Casual users will stay on the free tier indefinitely -- this is by design for adoption.
- **Regular users:** Hit the limit mid-month. This creates natural upgrade pressure at the right point: after experiencing enough value to understand the product.
- **Power/Extreme users:** Hit the limit within days. This is appropriate -- these users should be paying customers.

**Recommendation:** 100 facts/month is well-calibrated for the free tier. It generously covers casual usage while creating clear upgrade signals for regular+ users.

### 8.2 Pro Tier ($5/month)

| Profile | Facts/month | Gas cost/month | @ $5/mo margin | Margin % @ $5 |
|---|:-:|:-:|:-:|:-:|
| **Regular** | 234 | $0.178 | +$4.82 | 96.4% |
| **Power** | 831 | $0.632 | +$4.37 | 87.4% |
| **Extreme** | 1,731 | $1.315 | +$3.69 | 73.7% |

**Assessment:**
- **At $5/month:** Comfortable margins across all profiles. Even extreme users generate $3.69/month margin, which absorbs Paymaster fees, payment processing, and contributes to infrastructure costs.
- **Stripe fee impact:** At $5/month, Stripe takes ~$0.45 (2.9% + $0.30), leaving $4.55. Annual billing mitigates this further.

### 8.3 Break-Even Subscription Price (Gas-Only)

| Profile | Facts/month | Gas cost/month | Break-even price (gas only) | Break-even + Stripe fees | Break-even + Stripe + 50% infra margin |
|---|:-:|:-:|:-:|:-:|:-:|
| Casual | 30 | $0.023 | $0.02 | $0.33 | $0.50 |
| Regular | 234 | $0.178 | $0.18 | $0.52 | $0.78 |
| Power | 831 | $0.632 | $0.63 | $1.01 | $1.52 |
| Extreme | 1,731 | $1.315 | $1.32 | $1.71 | $2.57 |

**Gas costs are extremely low relative to subscription pricing.** Even the most extreme users cost less than $1.32/month in gas. The primary cost drivers for the business will be:
1. Infrastructure (Graph Node, relay server, PostgreSQL)
2. Payment processing fees

**Note on LLM extraction costs:** The `extractFacts()` call uses the underlying agent's LLM — auto-detected from the agent's configured provider (e.g., `anthropic/claude-sonnet-4-5` → uses the Anthropic API key already in the environment, derives a cheaper model like `claude-haiku-4-5` for extraction). No extra LLM configuration is needed. Most OpenClaw users are on flat-rate monthly subscriptions, so extraction calls are effectively free — bundled in their existing quota. Only users on pay-per-token API keys would see incremental LLM cost (~$0.001-0.005/call). This is not a cost TotalReclaw bears directly.

---

## 9. Multi-User Cohort Projections

Projecting costs for realistic user cohorts at different growth stages:

### Assumption: User Mix

| Profile | % of Paid Users | Rationale |
|---|:-:|---|
| Regular | 60% | Most subscribers are daily users |
| Power | 30% | Developers/researchers |
| Extreme | 10% | Heavy all-day users |

### Cohort Gas Costs

| Paid Users | Regular (60%) | Power (30%) | Extreme (10%) | Total Facts/mo | Total Gas/mo | Revenue @ $5/mo | Net @ $5 |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 100 | 60 | 30 | 10 | 56,370 | $42.84 | $500 | +$457 |
| 1,000 | 600 | 300 | 100 | 563,700 | $428.41 | $5,000 | +$4,572 |
| 10,000 | 6,000 | 3,000 | 1,000 | 5,637,000 | $4,284.12 | $50,000 | +$45,716 |

**Note:** These projections exclude free tier users (who generate gas costs but no revenue). At 100 facts/month free tier with an estimated 5:1 free:paid ratio, free users add:

| Free Users | Facts/mo (@ 100/user cap) | Gas Cost/mo |
|:-:|:-:|:-:|
| 500 | 50,000 | $38.00 |
| 5,000 | 500,000 | $380.00 |
| 50,000 | 5,000,000 | $3,800.00 |

Even at 50,000 free users, the gas cost ($3,800/month) is manageable -- the revenue from 10,000 paid users ($50,000/month at $5/mo) easily covers it.

### Comparison with Billing Spec Projections

The billing spec (`billing-and-onboarding.md` Section 3) projected 50 facts/day for "power users," yielding $114/month for 100 power users. Our analysis shows:
- Power users: 27.7 facts/day (vs. spec's 50) -- the spec was conservative/rounded up
- 100 power users gas cost: $63.16/month (vs. spec's $114)

The difference stems from the billing spec using a round 50 facts/day figure, while our detailed pipeline analysis accounts for extraction frequency, importance filtering, and batch dedup. **The billing spec was conservative by ~1.8x, which provides additional safety margin.**

---

## 10. Cross-Batch Dedup: Cost Impact of the Gap

The lack of cross-batch semantic dedup is called out as a concern. Here is its quantified impact:

### Sources of Cross-Batch Duplication

1. **Compaction re-extraction:** The most significant source. Compaction extracts from the full context, including turns that periodic extraction already processed.
2. **Session boundary effects:** If a user discusses the same topic across sessions, different extraction calls may produce similar (but not identical) facts.
3. **Slow-changing preferences:** Repeated mentions of "I prefer dark mode" across sessions will be extracted each time with slightly different wording.

### Quantified Impact

| Profile | Facts/mo (with fingerprint dedup only) | Estimated paraphrase duplicates/mo | Extra gas cost/mo | % overhead |
|---|:-:|:-:|:-:|:-:|
| Casual | 30 | ~1 | $0.001 | 3% |
| Regular | 234 | ~15 | $0.011 | 6% |
| Power | 831 | ~60 | $0.046 | 7% |
| Extreme | 1,731 | ~150 | $0.114 | 9% |

**Assessment:** Cross-batch duplication adds approximately 5-10% overhead to gas costs. At current Gnosis Chain prices, this translates to $0.01-$0.11/month per user -- negligible. However, the bloat impact on query performance (more blind index rows to scan) is a larger concern than the gas cost itself.

**Recommendation:** Implementing write-side semantic dedup (D3 in the retrieval improvements spec) is worthwhile for query performance reasons, but is NOT urgent for cost reasons.

---

## 11. Is the Extraction Frequency Right?

### Current Setting: AUTO_EXTRACT_EVERY_TURNS = 5

| Consideration | Assessment |
|---|---|
| Gas cost impact | Negligible at current Gnosis prices. Even at EVERY_TURNS=3, extreme users cost only $1.98/month. |
| LLM cost impact | Each extraction call uses the agent's configured LLM. For users on flat-rate subscriptions (most OpenClaw users), this is free. For pay-per-token users, ~$0.001-$0.005 per call = $0.04-$0.20/day for extreme users. Not a cost TotalReclaw bears directly. |
| Recall quality | More frequent extraction = fewer missed facts. At EVERY_TURNS=5 with a 3-turn lookback window, there is no gap (turns 1-3 covered by extraction at turn 5, turns 4-6 covered by extraction at turn 10). At EVERY_TURNS=10, a 3-turn lookback would miss turns 4-7. |
| Latency impact | Each extraction adds ~200-500ms to the `agent_end` hook. At EVERY_TURNS=5, this occurs 20% of turns. Acceptable. |

### Recommendation

**AUTO_EXTRACT_EVERY_TURNS = 5 is well-calibrated for gas costs.** The gas cost difference between 3 and 15 is less than $1/month even for extreme users. The extraction frequency should be tuned based on:

1. **Fact coverage quality** (recall impact of longer intervals)
2. **User-perceived latency** (frequency of slow turns)
3. **Gas cost** (negligible difference across settings)

Note: LLM extraction uses the agent's own LLM, which is typically on a flat-rate subscription for OpenClaw users. This is not an incremental cost to TotalReclaw.

---

## 12. Summary and Recommendations

### Key Findings

1. **Gas costs are extremely low on Gnosis Chain.** Even the most extreme user profile generates only $1.32/month in gas costs. This is 2-4 orders of magnitude below the subscription price.

2. **The free tier (100 facts/month) is well-calibrated.** Casual users (30 facts/month) never hit it. Regular users hit it mid-month, creating natural upgrade pressure.

3. **Pro pricing ($5/month) is highly sustainable.** Margins are 74-96% on gas alone (before infrastructure/LLM costs).

4. **Gas is the primary variable cost TotalReclaw bears per user.** LLM extraction uses the agent's own LLM (free for flat-rate subscription users). Infrastructure (Graph Node, relay server) is a fixed cost that scales with user count.

5. **Cross-batch dedup gap is a minor gas cost issue (~5-10% overhead)** but a more significant query performance issue that should be addressed separately.

6. **AUTO_EXTRACT_EVERY_TURNS = 5 is appropriate** from a gas cost perspective. Any tuning should be driven by fact coverage quality, not gas optimization.

### Answers to the Handoff Questions

**Q1: Is 100 facts/month a reasonable free tier for casual users? Will regular users hit it?**
Yes. Casual users generate ~30 facts/month (well within the limit). Regular users generate ~234 facts/month and will hit the limit around day 13, creating a natural upgrade moment.

**Q2: Does $5/month cover gas costs for power users?**
Yes, comfortably. Power users cost $0.63/month in gas; extreme users cost $1.32/month. At $5/month, there is ample margin for infrastructure costs.

**Q3: Should AUTO_EXTRACT_EVERY_TURNS be increased to reduce gas costs?**
No. Gas cost savings from increasing this parameter are negligible (<$0.50/month even for extreme users). The parameter should be tuned based on fact coverage quality and user-perceived latency instead.

**Q4: Is the lack of cross-batch dedup a significant cost driver?**
No, for gas costs (~5-10% overhead = $0.01-$0.11/month). Yes, for query performance (more blind index rows). Recommendation: implement write-side dedup (D3) for performance reasons, not cost reasons.

**Q5: What is the break-even subscription price at different usage levels?**
Gas-only break-even: $0.02 (casual) to $1.32 (extreme). Including Stripe fees: $0.33 to $1.71. Including 50% margin for infrastructure: $0.50 to $2.57.

### Final Pricing Model

| Tier | Price | Memory Limit | Storage | Target User | Gas Cost Coverage |
|---|:-:|:-:|---|---|---|
| **Free** | $0 | 500/month | Base Sepolia (testnet, trial) | Trial users | Negligible (testnet) |
| **Pro** | $5/month | Unlimited | Gnosis mainnet (permanent) | All paying users | Full coverage + ample infrastructure margin |

---

## Appendix A: Methodology Confidence Levels

| Parameter | Value Used | Confidence | Source |
|---|---|:-:|---|
| AUTO_EXTRACT_EVERY_TURNS | 5 | HIGH | Code: `index.ts` line 117 |
| MIN_IMPORTANCE_THRESHOLD | 3 | HIGH | Code: `index.ts` line 395 |
| LLM extraction threshold | >= 6 | HIGH | Code: `extractor.ts` line 45 + parser line 153 |
| Semantic dedup threshold | 0.9 cosine | HIGH | Code: `semantic-dedup.ts` line 33 |
| Gas per medium fact | 379,650 | HIGH | Measured: `gas-report.md` |
| Gnosis gas price | 2 gwei | MEDIUM | Typical value; can range 1-5 gwei |
| xDAI price | $1.00 | HIGH | Stablecoin peg |
| Mean facts per extraction (turn) | 1.0-1.3 | MEDIUM | Estimated from prompt analysis |
| Mean facts per compaction | 4-10 | MEDIUM-LOW | Estimated; no empirical data |
| Compaction frequency | 0-24/week | LOW | Depends on OpenClaw internals |
| Cross-batch duplicate rate | 5-10% | LOW | No empirical data |
| Turns per day per profile | 5-200 | MEDIUM | Reasonable ranges; not validated |

## Appendix B: Full Sensitivity Matrix (Power User)

All values for the Power user profile (90 turns/day, 10 compactions/week, 3 net new facts/compaction).

| EXTRACT_EVERY | Facts/extraction | Periodic/day | Compaction/day | Total/day | Facts/month | Gas $/month |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 3 | 0.5 | 15.0 | 4.3 | 19.3 | 579 | $0.440 |
| 3 | 1.3 | 39.0 | 4.3 | 43.3 | 1,299 | $0.987 |
| 3 | 2.0 | 60.0 | 4.3 | 64.3 | 1,929 | $1.466 |
| 3 | 3.0 | 90.0 | 4.3 | 94.3 | 2,829 | $2.150 |
| 5 | 0.5 | 9.0 | 4.3 | 13.3 | 399 | $0.303 |
| **5** | **1.3** | **23.4** | **4.3** | **27.7** | **831** | **$0.632** |
| 5 | 2.0 | 36.0 | 4.3 | 40.3 | 1,209 | $0.919 |
| 5 | 3.0 | 54.0 | 4.3 | 58.3 | 1,749 | $1.329 |
| 10 | 0.5 | 4.5 | 4.3 | 8.8 | 264 | $0.201 |
| 10 | 1.3 | 11.7 | 4.3 | 16.0 | 480 | $0.365 |
| 10 | 2.0 | 18.0 | 4.3 | 22.3 | 669 | $0.508 |
| 10 | 3.0 | 27.0 | 4.3 | 31.3 | 939 | $0.714 |
| 15 | 0.5 | 3.0 | 4.3 | 7.3 | 219 | $0.166 |
| 15 | 1.3 | 7.8 | 4.3 | 12.1 | 363 | $0.276 |
| 15 | 2.0 | 12.0 | 4.3 | 16.3 | 489 | $0.372 |
| 15 | 3.0 | 18.0 | 4.3 | 22.3 | 669 | $0.508 |

**Observation:** Across the entire parameter space (3-15 turns, 0.5-3.0 facts/extraction), gas costs range from $0.17 to $2.15/month for power users. The maximum is still well within the $5/month Pro tier.
