# Re-crystallize / Re-key Backfill — Design Spec

**Status:** Write path IMPLEMENTED + staging-E2E-validated (#438 → `feat/438-recrystallize-write-path`). The dry-run planner, checkpoint persistence, and the guarded on-chain write/tombstone loop all ship in `python/src/totalreclaw/recrystallize.py`. Not yet run against any real user vault (Phase B/C, gated on Pedro).
**Owner:** Pedro (product) / coordinator (architect)
**Depends on:** Hermes write-side `session_id` fix (#429 + #434, both merged) shipping in an RC that the target user is running.
**Scope:** Managed Service (on-chain) vaults only. Self-hosted is out of scope (no session-collapse bug there).

---

## 1. Problem

A Hermes write-side bug keyed on-chain `session_id` off a **chat-level host id** that
persists across process restarts. Every conversation in a single Telegram DM therefore
collapsed into **one** (or a few) giant `session_id`(s). The on-chain vault ends up with:

- **Mis-grouped atomic facts** — unrelated conversations share a `session_id`.
- **Mixed "Crystals"** — a Crystal is a `type=summary` claim with
  `metadata.subtype=session_crystal` that summarizes a session. Because sessions were
  collapsed, a Crystal either summarizes a mash-up of unrelated conversations, or a giant
  session has one Crystal that covers dozens of real conversations.

The write-side fix (#429 honours the host `session_id`; #434 adds a content-aware Crystal
gate) stops *new* writes from collapsing. **It does not touch data already on-chain.** The
existing vault stays mis-grouped forever unless we rewrite it. Pedro greenlit a **full
re-key backfill**: client-side, decrypt the vault → re-segment into coherent sessions →
write corrected data with fresh `session_id`s + fresh Crystals → tombstone the old facts and
old mixed Crystals.

### Ordering dependency (hard)

The backfill **MUST run after** the write-side fix is live for the target user. If it runs
first (or the user's client still has the buggy plugin), fresh auto-extraction writes will
re-collapse under the buggy `session_id` while the backfill is rewriting — a moving target.
The tool refuses to run unless the operator confirms the fix is deployed (a
`--write-side-fix-confirmed` flag or an interactive prompt; see §7).

---

## 2. Where it runs

The backfill is **client-side only** — the server never sees plaintext, so all
decrypt → re-segment → re-encrypt → write logic runs on the machine holding the recovery
phrase. Two realistic hosts:

| Host | Pros | Cons |
|------|------|------|
| **Standalone script in `python/tools/`** using the `totalreclaw` Python client | Reuses the exact write/tombstone/crypto path the vault was written with; one-off operational tool, not a shipped user feature; no plugin lifecycle to disturb; easy to gate behind a flag and run manually against staging | Not surfaced as a Hermes tool (fine — it's an operator action, not a user action) |
| **A Hermes tool** (`totalreclaw_recrystallize`) | Discoverable by the agent; reuses provider wiring | Wrong altitude: this is a destructive, multi-thousand-write migration, not a conversational action. Registering it as a tool invites accidental invocation. Lifecycle hooks would fire mid-migration. |

**Recommendation: a standalone Python script/module under `python/tools/` (a
`recrystallize` module in `python/src/totalreclaw/`), driven by a thin CLI.** Justification:

1. **It already speaks the right protocol.** The Python `TotalReclaw` client speaks the
   managed-relay protocol (`/v1/subgraph`, `/v1/bundler`, `/v1/billing/*`) and owns the full
   crypto + UserOp + tombstone path (`remember`, `remember_batch`, `forget`). The Rust
   client speaks the same protocol but lacks import/segmentation plumbing. The TS clients
   (plugin / MCP / NanoClaw) also work, but the segmentation core (`segment_sessions`) and
   the Crystal-builder logic already live in the **Python** `import_engine` — reusing them is
   the shortest path.
2. **It is an operator tool, not a user feature.** A destructive re-key of an entire vault
   should be run deliberately, with dry-run output reviewed by a human, against staging first.
   A standalone script keeps it out of the agent's tool surface and out of lifecycle hooks.
3. **Precedent.** The import engine (`python/src/totalreclaw/import_engine.py`) already does
   the hard half of this job — segment turns into sessions, mint `session_id`s, build
   Crystals, batch-write. The backfill is "import, but the source is your own decrypted
   vault." Reusing that machinery in Python is far cheaper than re-implementing it in Rust or
   TS.

The **write-side plugin fix is a separate track** (Hermes plugin) and is unaffected by where
the backfill lives.

---

## 3. Re-segmentation approach

The backfill reuses the **same centroid-walk semantic segmenter** that flat Gemini imports
use (`totalreclaw.session_segmentation.segment_sessions`, core-hoisted as
`totalreclaw_core.segment_sessions` / WASM `segmentSessions`, #368). Crucially — unlike the
read-only SPA view — **embeddings are available here**, so segmentation is high-quality:

### Input

Fetch **all active facts** for the owner from the subgraph (paginated, via the relay
`/v1/subgraph` proxy). For each fact we need the fields the standard `export`/`recall`
queries drop:

- `id`
- `encryptedBlob` → decrypt → the **raw v1 JSON blob** (NOT `read_blob_unified`, which
  whitelists `metadata` and drops the stored `metadata` dict — see §3.1)
- `encryptedEmbedding` → `decrypt_embedding` → the 640d Harrier vector (already L2-normalised)
- `createdAt` (per-fact ISO/Unix timestamp) — the ordering key for the segmenter's time-gap rule
- `timestamp` (block time) — fallback ordering only

We then split the decrypted set into:

- **Atomic facts** — everything that is not a Crystal.
- **Old Crystals** — `metadata.subtype == "session_crystal"`.

Only **atomic facts** feed the segmenter; old Crystals are re-derived from scratch.

### 3.1 Recovering `session_id` / `subtype` (important gotcha)

The standard read path `read_blob_unified()` (a.k.a. `read_claim_from_blob`) for a **v1
blob** rebuilds `metadata` from a fixed whitelist (`type/source/scope/volatility/reasoning/
created_at/schema_version`) and **discards the stored `metadata` dict** — so
`session_id` and `subtype` are *not* visible through it. The backfill must
`json.loads()` the decrypted blob directly and read `blob["metadata"]["session_id"]` /
`blob["metadata"]["subtype"]`. The scaffold does this in `_decode_raw_blob()`.

### Segmentation call

```
sessions = segment_sessions(
    timestamps=[f.created_at for f in atomic_facts_sorted_by_time],
    embeddings=[f.embedding for f in atomic_facts_sorted_by_time],
    gap_seconds=1800,      # 30-min hard boundary, same as imports
    sim_threshold=0.55,    # validated default
)
```

Facts are sorted **chronologically by `created_at`** first (the segmenter assumes time
order). `segment_sessions` returns `list[list[int]]` — ordered, contiguous, ascending
index groups into the sorted atomic-fact list.

### Output

Each returned group is a **coherent re-segmented session**. For each group:

- Facts with ≥2 originating turns' worth of content → a **multi-fact session** that gets a
  fresh `session_id` (UUIDv7) + a fresh Crystal.
- A singleton group (1 fact) → keep it, re-key with fresh provenance/metadata but **no
  Crystal** (mirrors the import singleton rule).

> **Caveat — facts are not turns.** The import segmenter runs over *conversation turns*;
> here we run over *extracted facts*. A fact's embedding is a fair proxy for topical
> grouping, and time-gap still fires on `created_at`, so the grouping is good but not
> identical to what the fixed write-side plugin would have produced live. This is acceptable
> for a backfill (the goal is "coherent groups," not "byte-identical to a live run"). The
> dry-run output lets a human sanity-check the grouping before any write. This is an open
> question flagged in §10.

---

## 4. Re-key write plan

For each corrected multi-fact session (and each re-keyed singleton), the write path mirrors
the import engine + the retype/pin tombstone-and-rewrite pattern already in the codebase.

### 4.1 Per corrected session

1. **Rewrite atomic facts.** For every old fact in the group, write a **new** fact with:
   - identical `text`, `importance`, `fact_type`, `provenance`, `entities`,
   - the **fresh `session_id`** in `extra_metadata`,
   - `import_source` preserved if present,
   - the original embedding reused (re-encrypted) so LSH trapdoors + search are unchanged.
   Batched via `client.remember_batch(...)` — up to **30 facts per `executeBatch` UserOp**
   (core 2.5.5, #392 Part 2).
2. **Write a fresh Crystal** (`type=summary`, `metadata.subtype=session_crystal`,
   `metadata.session_id=<fresh>`) summarizing *only this coherent session's* facts, reusing
   the import engine's `_make_crystal` prompt/builder. One LLM summary call per corrected
   multi-fact session.
3. **Tombstone the old atomic facts** in the group (`client.forget(old_id)` →
   `decayScore=0` / `isActive=false`). No batch-delete exists on managed service; deletes are
   per-fact tombstones. Tombstones **can** be batched into an `executeBatch` UserOp the same
   way stores are (each inner call is one tombstone protobuf) — the scaffold groups them ≤30
   per UserOp. (The Python client currently exposes `forget` per-fact; a batched-tombstone
   helper is a small addition noted as a TODO in the scaffold — until it lands, tombstones go
   one-per-UserOp, which changes the *UserOp* count but **not** the quota cost, since quota
   bills facts not UserOps — see §5.)

### 4.2 Old mixed Crystals

Every old Crystal (`subtype=session_crystal`) is **tombstoned** — the fresh per-session
Crystals from §4.1 replace them. Old Crystals are never rewritten; they summarized
mash-ups and have no coherent successor.

### 4.3 Ordering within a run

Per session, the safe order is **write-new → confirm-indexed → tombstone-old**. Writing the
corrected data *before* tombstoning the old means an interruption between the two leaves the
vault with duplicates (recoverable, idempotent — see §6) rather than a hole. The reverse
order risks a window where the data is gone but not yet rewritten.

---

## 5. Quota cost estimate + batching

**Quota unit = memories *written*, counted per-fact by the relay** — confirmed in
`totalreclaw-relay/src/services/userop-decoder.ts` + `src/routes/proxy.ts`: the relay
decodes the UserOp calldata and bills `extractFactCount(userOp)` = the number of inner calls
(`executeBatch` array length, clamped to 30; `execute` = 1) against `checkWriteQuota`. This
counting is **payload-agnostic** — it does not distinguish a store from a tombstone. A
tombstone is a `Log(bytes)` write submitted via `eth_sendUserOperation` (a write RPC method),
so **tombstones count against quota**, one unit each.

Batching (`executeBatch`) reduces **UserOp count** (and thus Pimlico cost) but **not quota
cost** — quota is per-fact regardless of batching.

### 5.1 Formula

Let, over the whole vault:

- `F` = number of active atomic facts (non-Crystal)
- `C_old` = number of old (mixed) Crystals
- `S_multi` = number of corrected sessions with ≥2 facts (each gets one fresh Crystal)

Then:

```
writes_new       = F              (rewrite every atomic fact with fresh session_id)
                 + S_multi        (one fresh Crystal per corrected multi-fact session)
tombstones       = F              (tombstone every old atomic fact)
                 + C_old          (tombstone every old mixed Crystal)

TOTAL_QUOTA_COST = writes_new + tombstones
                 = 2·F + S_multi + C_old
```

UserOp count (Pimlico, not quota) with 30-fact batching:

```
userops ≈ ceil(writes_new / 30) + ceil(tombstones / 30)   (+ a few for confirm/retry)
```

### 5.2 Worked example

Take a plausible mid-size vault:

- `F = 600` atomic facts
- `C_old = 20` old mixed Crystals
- Re-segmentation yields, say, `S_multi = 45` coherent multi-fact sessions
  (plus some singletons that need no Crystal)

```
writes_new       = 600 + 45  = 645
tombstones       = 600 + 20  = 620
TOTAL_QUOTA_COST = 2·600 + 45 + 20 = 1265 memories
```

At the larger end the task brief anticipates (~2,000–3,000 facts):

- `F = 2500`, `C_old = 60`, `S_multi ≈ 180`
- `TOTAL_QUOTA_COST = 2·2500 + 180 + 60 = 5240 memories`

**Pro is ~3000 memories/month.** So:

- A ~600-fact vault (1,265 units) fits inside one Pro month with headroom.
- A ~2,500-fact vault (5,240 units) **exceeds a single Pro month** and must span **two
  monthly quota windows** (or use top-up packs, #392). This is *exactly* why the tool must be
  **resumable across quota windows** (§6): it will 403 partway through and must pick up where
  it left off next month.

The dry-run planner (`plan_recrystallize()`) computes `F`, `C_old`, `S_multi`, and the full
formula from the actual fetched vault and prints the number **before any write**.

### 5.3 Cost-reduction levers (open questions for §10)

- **Skip re-keying already-coherent singletons.** A singleton fact whose `session_id`
  already isolates it correctly needn't be rewritten — but detecting "already coherent" is
  fuzzy. Conservative default: rewrite everything (the formula above); an opt-in
  `--skip-coherent-singletons` could shave the `2·(singleton count)` term.
- **In-place metadata edit is not possible** — the blob is immutable on-chain and metadata
  is inside the encrypted payload, so "just change the session_id" always means
  write-new + tombstone-old (2 units), never 1. There is no cheaper primitive.

---

## 6. Idempotency / resumability

The run can exceed a monthly quota window (a large vault spans ≥2 months) or be interrupted
(crash, 403, Ctrl-C). It must be **safely re-runnable**.

### 6.1 Checkpoint file

Following the import-state precedent (`~/.totalreclaw/import-state/*.json`,
`totalreclaw.import_state`), the backfill persists a checkpoint at
**`~/.totalreclaw/recrystallize-state/<vault_fingerprint>.json`** where `vault_fingerprint`
is a hash of the owner address (so re-running against the same vault resumes; a different
vault starts fresh).

The checkpoint records, per corrected session:

```jsonc
{
  "owner": "0x…",
  "started_at": "…",
  "last_updated": "…",
  "status": "running|paused_quota|completed|failed",
  "sessions": {
    "<fresh_session_id>": {
      "phase": "planned|written|tombstoned|done",
      "old_fact_ids": ["0x…", …],
      "new_fact_ids": ["…", …],
      "crystal_written": true,
      "old_crystal_ids_tombstoned": ["0x…"]
    }
  },
  "quota_exhausted_at": null
}
```

### 6.2 Idempotency rules

- **Deterministic session assignment.** Segmentation is deterministic given the same fetched
  facts + embeddings, so a resumed run re-derives the same groups. Fresh `session_id`s are
  **read from the checkpoint** for already-planned sessions (not re-minted) so a resume never
  double-mints.
- **Phase gating.** Each session advances `planned → written → tombstoned → done`. On resume,
  a session in `written` skips the (already done) writes and proceeds to tombstone. Because
  writes precede tombstones (§4.3), a crash never leaves a hole — at worst it leaves the new
  copy *and* the old copy (both active), which the tombstone phase then cleans up.
- **Content-fingerprint dedup as a backstop.** The relay's server-side content fingerprint
  (HMAC-SHA256) rejects a byte-identical re-store as a 409 — but the rewritten fact carries a
  **new `session_id`**, so its fingerprint differs from the old fact and it is NOT auto-deduped
  against the original. The fingerprint *does* protect against re-writing the *same corrected
  fact twice* across two resume attempts (identical new payload → 409 → skip). The checkpoint
  is the primary guard; the fingerprint is defense-in-depth.
- **Quota-pause is a clean stop.** On a 403 `quota_exceeded`, the run marks
  `status=paused_quota`, writes the checkpoint, and exits 0 with a "resume next month"
  message. Re-running after the quota resets continues from the checkpoint.

---

## 7. Safety

- **Mandatory dry-run.** `plan_recrystallize()` (dry-run) is the **default**. It fetches the
  vault, decrypts, segments, and prints: the re-grouping (old session_ids → fresh session_ids,
  with fact counts + sample titles), `F` / `C_old` / `S_multi`, the write/tombstone counts,
  and the total quota cost — **and writes nothing on-chain.** A real run requires an explicit
  `--execute` (or `dry_run=False`) plus an interactive confirmation.
- **Staging only for all testing.** Every test run hits `api-staging.totalreclaw.xyz`
  (isolated DataEdge `0xE7a4…` + `total-reclaw-gnosis-staging` subgraph). **Never
  production.** This is a hard project rule. The scaffold defaults `server_url` to the staging
  relay and requires an explicit `--i-understand-this-is-production` to target prod.
- **Write-side-fix precondition.** The tool refuses to `--execute` unless the operator passes
  `--write-side-fix-confirmed` (attesting the target client runs the #429/#434 fix). Running
  the backfill against a still-buggy client re-collapses new writes mid-migration.
- **Confirm-indexed between write and tombstone.** Reuse `confirm_indexed` so a session's new
  facts are provably on-chain before the old ones are tombstoned.
- **No batch-delete.** All deletes are per-fact tombstones (managed service has no bulk
  delete). The tool never assumes otherwise.

---

## 8. Composition with the write-side fix (explicit dependency)

```
   [write-side plugin fix #429 + #434]  ──ships in RC──▶  user's client stops collapsing
                                                                  │
                                                                  ▼
                            [backfill runs, --write-side-fix-confirmed]
                                                                  │
                                            rewrites existing on-chain data into
                                            coherent sessions + fresh Crystals
```

- The backfill is **downstream** of the write-side fix. It corrects *history*; the fix
  corrects *the future*.
- If the backfill ran first (or while the client is still buggy), live auto-extraction would
  keep writing new facts under the buggy `session_id`, so the vault would re-collapse as fast
  as the backfill repairs it. Hence the hard precondition in §7.

---

## 9. Testing

- **Unit** (pure logic, shipped with the scaffold): the cost estimator / dry-run planner —
  `plan_recrystallize()` over synthetic decrypted-fact fixtures asserting the `2·F + S_multi +
  C_old` formula, singleton handling, and old-Crystal accounting. No network.
- **Staging smoke** (`tests/e2e-batch/`, when the write path is implemented): seed a fresh
  staging vault with a deliberately-collapsed session (many unrelated facts under one
  `session_id`), run the backfill in `--execute` mode against staging, assert the re-grouping,
  fresh Crystals, and tombstones. **Staging only.**
- **E2E is MANDATORY before this is considered done** (per project rule) — the on-chain
  write path is not complete until validated end-to-end against staging.

---

## 10. Open questions for a human to decide before build

1. **Segment over facts vs. reconstructed turns.** We segment over *extracted facts*, not the
   original conversation *turns* (turns aren't on-chain). Is fact-level grouping good enough,
   or should the tool attempt to reconstruct turn boundaries (e.g. from `created_at`
   clustering) for closer parity with a live write-side run? (§3 caveat.)
2. **Cost ceiling / two-month spans.** A ~2.5k-fact vault costs ~5.2k quota units — >1 Pro
   month. Do we (a) let it span two monthly windows via checkpoint-resume, (b) buy top-up
   packs (#392), or (c) temporarily raise the target user's quota out-of-band for the
   migration? This is a billing/product call.
3. **Rewrite-everything vs. skip-coherent.** Default rewrites every fact (simple, correct,
   2·F cost). Is the extra cost of rewriting already-fine singletons worth the simplicity, or
   do we invest in "already-coherent" detection to shave cost? (§5.3.)
4. **Crystal LLM provider + cost.** Each corrected multi-fact session needs one LLM summary
   call. Which provider/key runs those during the migration, and is that latency/cost
   acceptable for `S_multi` ≈ hundreds of calls?
5. **`session_id` recovery on read.** Confirmed: `read_blob_unified` drops stored
   `metadata` for v1 blobs (§3.1). The scaffold decodes the raw blob directly. Should
   `read_blob_unified` be *fixed* to round-trip `metadata` (a broader change touching recall)
   or is the backfill-local raw decode the right containment? (Prefer containment for now.)
6. **Batched-tombstone helper.** The Python client exposes per-fact `forget` only. Adding a
   `forget_batch` (`executeBatch` of tombstone protobufs) cuts UserOp count ~30× (not quota).
   Worth adding for this migration, or is per-fact `forget` acceptable given quota — not
   UserOps — is the binding constraint?

---

## 11. Non-goals

- Not a shipped user-facing feature or Hermes tool (operator script).
- Not a self-hosted concern (no session-collapse bug there).
- Not a batch-delete (managed service has none; per-fact tombstones only).
- Does **not** modify the write-side plugin (separate track).

---

## 12. Usage (operator CLI + API)

The backfill ships as `python/src/totalreclaw/recrystallize.py`, driven either
via the module CLI or the async API.

### 12.1 CLI

The recovery phrase is read **only** from an env var (never a CLI arg) and is
never printed. Staging is the default relay; production requires an explicit
`--i-understand-this-is-production`.

```bash
# 1. DRY-RUN (default — writes nothing). Prints the re-grouping + quota cost.
export TOTALRECLAW_RECOVERY_PHRASE="…12 words…"
cd python
PYTHONPATH=src python -m totalreclaw.recrystallize \
  --server-url https://api-staging.totalreclaw.xyz

# 2. EXECUTE (writes on-chain). Requires --write-side-fix-confirmed (attesting
#    the target client runs the #429/#434 fix) + interactive "yes" confirm.
#    Resumes automatically from ~/.totalreclaw/recrystallize-state/<fp>.json.
PYTHONPATH=src python -m totalreclaw.recrystallize \
  --server-url https://api-staging.totalreclaw.xyz \
  --execute --write-side-fix-confirmed
```

On a 403 `quota_exceeded` mid-run the tool marks the checkpoint `paused_quota`,
prints "resume next month", and exits 0. Re-running the **same command** after
the quota resets continues from the checkpoint (idempotent — already-completed
sessions are skipped).

### 12.2 API

```python
from totalreclaw import TotalReclaw
from totalreclaw.recrystallize import (
    plan_recrystallize, execute_recrystallize, RecrystallizeCheckpoint,
)

client = TotalReclaw(recovery_phrase=PHRASE,
                     server_url="https://api-staging.totalreclaw.xyz")

plan = await plan_recrystallize(client)         # dry-run: fetch + segment + estimate
print("\n".join(plan.summary_lines()))

checkpoint = RecrystallizeCheckpoint.load(plan.owner)   # resume if a run exists
await execute_recrystallize(
    client, plan,
    write_side_fix_confirmed=True, confirm=True,
    checkpoint=checkpoint,
    llm_completion=my_async_llm,                # optional — Crystal summaries
)
```

### 12.3 Staging E2E (acceptance gate)

`python/tests/e2e/recrystallize_staging_e2e.py` seeds a fresh **throwaway**
vault (in-process mnemonic, never printed) with a deliberately mixed Crystal +
facts collapsed under one bad `session_id`, runs plan → execute against staging,
and verifies on the subgraph that the old Crystal + facts are tombstoned and
re-keyed facts + a fresh Crystal exist with new `session_id`s.

```bash
cd python
PYTHONPATH=src python tests/e2e/recrystallize_staging_e2e.py             # real run (staging)
PYTHONPATH=src python tests/e2e/recrystallize_staging_e2e.py --self-test  # redaction check, no network
```

### 12.4 Spec deviations (implementation notes)

- **§4.1 tombstones are per-fact, not batched.** The spec flags a batched-
  tombstone helper as an optional TODO (§10.6). The implementation uses the
  existing per-fact `client.forget` (one UserOp each). This changes the *UserOp*
  count, **not** the quota cost (quota bills facts, not UserOps — §5), so the
  dry-run estimate is unaffected. A `forget_batch` remains a future optimization.
- **Crystal provenance is `derived`, not `external`.** A re-derived Crystal is
  computed from the vault's own facts, so `derived` is the correct v1
  MemorySource (the import path uses `external` for provider-sourced data). The
  fresh `session_id` + `session_crystal` subtype key exactly as the fixed live
  write-side path.
- **Crystal summary is fact-only** (no transcript) — turns aren't on-chain, so
  the backfill prompt summarizes the re-segmented facts. This is the §3 caveat,
  made concrete.
