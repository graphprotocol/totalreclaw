# Spec: Surface relay read denials in the Python client and Hermes (#662)

| | |
|---|---|
| **Status** | Draft, ready for implementation |
| **Issue** | [p-diogo/totalreclaw#662](https://github.com/p-diogo/totalreclaw/issues/662) |
| **Baseline** | `origin/main` `2a75c49` (public). Relay reference `b7eefc8` (read-only) |
| **Implementer** | Sonnet coding agent, one PR, `python/` + docs only |
| **Sibling** | Relay spec B, `totalreclaw-internal/docs/specs/ops/relay-read-quota-rework.md` (new `read_quota_exceeded` 429 contract, `error_code: "rate_limited"`, `/v1/billing/status` read fields) |
| **Review** | Sonnet-high review before merge. Transport/UX path only, so no crypto review is needed |

## 1. Problem

Incident, 2026-09-18 to 2026-09-24: the founder's Hermes (`totalreclaw` 2.5.1rc1, prod relay) hit the monthly read cap. Every `POST /v1/subgraph` returned 403 `quota_exceeded`. Recall silently returned `[]` for 6 days. The only trace was WARNING lines in `~/.hermes/logs/errors.log`. The relay saw about 3,200 calls/day because each recall fans out:

- `search_facts` runs one query per 5-trapdoor chunk (`DEFAULT_TRAPDOOR_BATCH_SIZE = 5`, usually 5 to 8 chunks), paginates, and always runs the broadened query.
- The extraction path adds one recall for dedup (`lifecycle._fetch_recent_memories`) plus **one recall per fact** for contradiction detection (`contradiction.py`).
- Every failure is `except Exception: log; continue`.

Root cause: `RelayClient.query_subgraph` (`python/src/totalreclaw/relay.py:313`) calls `resp.raise_for_status()` and never parses the relay body. The callers can't tell "quota exhausted" from "vault empty".

**Goal:** a read denial becomes a typed error. It stops the fan-out at the first HTTP call, pauses reads for a bounded window across every call site, and reaches the user as a plain-language notice. Writes are unaffected.

## 2. Relay contract the client must accept

Match on the `error_code` field first, then `error`, by **string equality**, never by substring. `read_quota_exceeded` contains the substring `quota_exceeded`, and MCP's `isQuotaExceededError` makes exactly this mistake.

| Case | Status | Body (relevant keys) | Headers | Source |
|---|---|---|---|---|
| Legacy read quota (live today) | 403 | `{"error":"quota_exceeded","message":"Read limit reached…","upgrade_url":"https://totalreclaw.xyz/pricing"}` | none | `proxy.ts:326` |
| New read quota (spec B §4.5, only when `READ_QUOTA_MODE=enforce`) | 429 | `{"error":"read_quota_exceeded","error_code":"read_quota_exceeded","message","tier","limit","used","resets_at","retry_after","upgrade_url"\|null}` | `Retry-After: <s>` | spec B |
| Rate limit (per-IP 5000/h, per-wallet 500/h, becoming per-user 1000/h) | 429 | `{"success":false,"error":"Rate limit exceeded. Try again later.","retry_after":N}`, plus `error_code:"rate_limited"` after spec B | `Retry-After` (express-rate-limit `standardHeaders`) | `rate-limit.ts:45` |

Notes:
- The relay's `checkReadQuota` currently **fails closed** on DB errors with the same legacy 403 body. So a legacy 403 can be a transient DB blip. §4.3 re-probes for exactly this reason. Spec B flips the read path to fail-open.
- On `/v1/subgraph`, 401 comes only from auth and 400 only from a bad wallet format. Neither is quota.
- The write-path 403 `quota_exceeded` on `/v1/bundler` (`proxy.ts:191`) is **out of scope and must not change**. `submit_userop` keeps `raise_for_status()`.

## 3. Shared-core check

Apply CLAUDE.md's test: would the same logic give the same outputs from any client?
- **Transport, back-off state and host UX: no, so they stay in the adapter.** They depend on the HTTP client, the per-process client lifetime, and Hermes' injection channels.
- **Error-body classification (`status + body + Retry-After → kind`) and notice wording: technically pure.** They are not hoisted now, because only Hermes is active and core has no HTTP concerns. Add one line to `totalreclaw-internal/docs/plans/core-hoist-backlog.md`: "candidate `classify_relay_read_error` + `format_read_block_notice` (precedent: `format_recall_context`), hoist when MCP/plugin/ZeroClaw un-park (#662)".

## 4. Design

### 4.1 Typed errors (`python/src/totalreclaw/relay.py`)

All four classes subclass **`httpx.HTTPStatusError`**. Existing `except httpx.HTTPStatusError` and `.response.status_code` checks keep working, including `crystals/recrystallize.py::_is_quota_exhausted_error`.

```python
class RelayReadError(httpx.HTTPStatusError):
    """Non-2xx from POST /v1/subgraph."""
    status_code: int
    error_code: Optional[str]      # body error_code, else body error (str), else None
    relay_message: Optional[str]   # body message, truncated to 200 chars
    def __init__(self, response: httpx.Response, *, error_code=None, relay_message=None): ...
    # str(): "TotalReclaw relay read failed (HTTP 502): <relay_message or code>"

class RelayReadBlocked(RelayReadError):
    """Reads are blocked for a window. Triggers the client-wide read pause (§4.3)."""
    kind: ClassVar[str]            # "read_quota" | "rate_limited"
    retry_after_s: Optional[float] # from body retry_after, else the Retry-After header

class RelayReadQuotaExceeded(RelayReadBlocked):
    kind = "read_quota"
    upgrade_url: Optional[str]; tier: Optional[str]
    limit: Optional[int]; used: Optional[int]
    resets_at: Optional[datetime]  # aware UTC. Parsed from body; None on legacy
    legacy: bool                   # True = 403 quota_exceeded contract
    # str(): "TotalReclaw memory reads are paused: monthly read limit reached."

class RelayRateLimited(RelayReadBlocked):
    kind = "rate_limited"
    limit_scope: Optional[str]     # spec B additive field
    # str(): "TotalReclaw memory reads are paused: too many requests (retry in ~N min)."
```

Friendly `__str__` matters. Every Hermes tool ends in `json.dumps({"error": str(e)})`, so pin, unpin, retype, set_scope and any path not special-cased still say something sensible.

New private helpers in `relay.py`:
- `_classify_subgraph_error(resp: httpx.Response) -> RelayReadError`. Tolerate a non-JSON body (Cloudflare HTML). Take `code = body.get("error_code") or body.get("error")` if it is a `str`. Rules, first match wins:
  1. `code == "read_quota_exceeded"` (any status) → `RelayReadQuotaExceeded(legacy=False)`
  2. `status == 403 and code == "quota_exceeded"` → `RelayReadQuotaExceeded(legacy=True)`
  3. `status == 429` → `RelayRateLimited` (covers the legacy text body and `error_code == "rate_limited"`)
  4. otherwise → `RelayReadError`. A 403 with a non-JSON or unknown body is **not** quota.
- `_retry_after_seconds(resp, body) -> Optional[float]`. Use the body's `retry_after` (a number) first, then the `Retry-After` header as delta-seconds or an HTTP-date via `email.utils.parsedate_to_datetime`. Negative or unparseable → `None`. Mirror `agent/llm_client.py::_parse_retry_after`; don't import it, because `relay.py` must not import `agent/`.

Export the four classes from `totalreclaw/__init__.py`.

### 4.2 `query_subgraph` changes

```python
async def query_subgraph(self, query, variables, chain=None) -> dict:
    blk = self.read_block()
    if blk is not None:                       # active pause: NO HTTP
        logger.debug("subgraph read short-circuited (%s paused until %s)", ...)
        raise blk.error.with_traceback(None)  # stop traceback growth across raises
    ... existing request (wallet header unchanged, #486) ...
    if resp.is_success:
        self._clear_read_block()
        return resp.json()
    err = _classify_subgraph_error(resp)
    if isinstance(err, RelayReadBlocked):
        self._set_read_block(err)
    raise err
```

This is the first line of the short-circuit. Once one call is blocked, **every** `query_subgraph` call site gets zero-HTTP failures until the window expires: recall chunks, pagination, broadened search, dedup, contradiction and `confirm_indexed`. The per-site decisions in §4.4 control what each caller does with the error.

### 4.3 Client-wide read pause (on `RelayClient`)

The `RelayClient` lives as long as `AgentState`'s `TotalReclaw` client: one per daemon process, shared by the sync-loop hooks and the Hermes async tools. That makes it the right owner. Plain attribute writes are enough; no lock is needed.

```python
@dataclass
class ReadBlockState:
    error: RelayReadBlocked
    paused_until: float        # time.monotonic() deadline
    paused_until_utc: datetime # for display
    since_utc: datetime        # start of the episode
    episode: int               # monotonically increasing per RelayClient

def read_block(self) -> Optional[ReadBlockState]   # the active state, or None once the deadline passes (the episode record is kept)
def _set_read_block(self, err) -> None             # reuses the episode id if the previous episode was never cleared by a success
def _clear_read_block(self) -> None                # on any 2xx; the next block starts a new episode
```

Log a WARNING once per episode: `"TotalReclaw: relay reads paused (%s) until %s UTC; recall will show a notice"`. Re-blocks inside the same episode log at DEBUG. That ends the errors.log spam.

Pause length, `_pause_seconds(err)`:
- `RelayRateLimited`: `clamp(retry_after_s or 300, 30, 3600)`.
- `RelayReadQuotaExceeded`: `min(REPROBE, seconds_until(resets_at))` when `resets_at` is known, else `REPROBE`. `REPROBE` comes from env `TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS` (default **900**, floor 5, so the E2E can shorten it).

**Why re-probe instead of pausing until month-end.** In the incident the cap was raised mid-episode (1k → 50k). The legacy 403 can also be a DB blip. A pause until `resets_at` would have kept memory off after the fix. One probe every 15 minutes (about 4 calls/h) self-heals within 15 minutes of a cap raise, upgrade or DB recovery.

**User-facing reset time:**
- `resets_at` when present.
- Legacy 403: display "the 1st of next month (UTC)", since the relay's `getMonthStart` is the UTC month. The display never promises that a success can't come earlier.

Expose `TotalReclaw.read_block` as a property on `client.py`: `return self._relay.read_block()`.

The pause is **not persisted** to disk. A daemon restart costs one probe. See open question Q2.

### 4.4 Call sites of `query_subgraph` and their decisions

| # | Site | Used by | Decision | Change |
|---|---|---|---|---|
| 1 | `operations.py:831` `search_facts` chunk query | `client.recall` | **Propagate** | `except RelayReadBlocked: raise` before the generic `except` |
| 2 | `operations.py:845` `search_facts` pagination | same | **Propagate** | inside the same `try`, so covered by #1 |
| 3 | `operations.py:876` `search_facts` broadened | same | **Propagate** | same pattern. Not reached after #1 raises |
| 4 | `operations.py:1107` `_fetch_fact_by_id` | `pin_fact`/`unpin_fact` (`_change_claim_status` L1380) | **Propagate** (already un-caught) | none; tools render it (§4.6) |
| 5 | `operations.py:1741` `export_facts` | `client.export_all` → `totalreclaw_export` | **Propagate** | remove the outer `except Exception: break`. Today it turns *any* failure into a silent partial or empty export. Keep the per-fact decrypt `except` |
| 6 | `operations.py:1834` `find_existing_content_fps` | `client.find_duplicate_texts` → import engine pre-write dedup | **Degrade** (fail-open, existing) | add `except RelayReadBlocked: logger.debug(...); return set()` so it does not log at WARNING |
| 7 | `confirm_indexed.py:118` poll loop | forget (`operations.py:1053`), pin (`:1611`), retype/set_scope (`retype_setscope.py:539`), recrystallize (`:900`) | **Degrade, fast** | `except RelayReadBlocked: logger.info(...); return False` immediately. No polling to timeout. The write already landed and callers already map `False` to `partial=True` |
| 8 | `retype_setscope.py:355` fact fetch | `totalreclaw_retype`/`set_scope` | **Propagate** (already un-caught) | none; tools render it (§4.6) |
| 9 | `userop.py:1483` `session_key_grant_was_installed` | Option E Phase 3 (parked, never deployed) | **Ignore** (existing degrade to "not installed") | none |
| 10 | `crystals/recrystallize.py:437` `_fetch_decrypted_facts` | operator backfill CLI | **Propagate** (already un-caught) | none. The message is now readable. Leave `_is_quota_exhausted_error` alone: it is the *write* pause |

Indirect readers (via `client.recall`):

| Site | Decision | Change |
|---|---|---|
| `agent/recall.py` `auto_recall` / `auto_recall_async` | **Surface** | §4.5 |
| `hermes/tools.py:231` `totalreclaw_recall` | **Surface** | §4.6 |
| `agent/lifecycle.py:121` `_fetch_recent_memories` (dedup context) | **Degrade** | `except RelayReadBlocked: return []` (debug log) before the generic handler; keep the `InterpreterShutdownError` re-raise first |
| `agent/contradiction.py:168` per-fact recall | **Degrade + stop the loop** | on `RelayReadBlocked`, append this fact **and all remaining facts** to `kept`, log INFO once, `break`. Contradiction detection is skipped for the rest of the batch |

**Generic failures (5xx, transport, 401) in `search_facts`.** Keep the per-chunk degrade: partial results beat none. But track `ok_queries` and `last_error`. If **no** query succeeded, `raise last_error` instead of returning `[]`, so a total outage is no longer "vault empty". `auto_recall` still just logs these (unchanged UX, Q4). The recall tool reports them as `{"error": ...}`.

**Writes.** Writes are unaffected. `submit_userop`, `store_fact*`, `remember` and auto-extraction never consult `read_block()`. While reads are paused, pre-write dedup and contradiction detection fail open, so a duplicate fact is possible (Q5).

### 4.5 Hermes surfacing: auto-recall and the per-turn channel

New module `python/src/totalreclaw/agent/read_block.py` (framework-agnostic, beside `recall.py`):

```python
def format_read_block_notice(blk: ReadBlockState, *, compact: bool = False) -> str
def read_block_tool_payload(err: RelayReadBlocked, blk: Optional[ReadBlockState]) -> dict
```

Wording is plain, for non-technical users. `{…}` are the fields. Every notice ends with an agent instruction.

- **Quota, full:** `[totalreclaw] Memory lookups are paused: this account has used its monthly memory-read allowance. Saved memories are safe, and new ones are still being saved, but I can't search them until {reset_phrase}. TotalReclaw retries automatically every {reprobe_min} minutes, so if the limit is raised it resumes on its own.{ " To lift the limit now: " + upgrade_url if upgrade_url }` Then the agent instruction: `Tell the user this in one or two short sentences. Do NOT say they have no memories.`
- **Rate limited, full:** `[totalreclaw] Memory lookups are briefly paused (too many requests). They resume automatically in about {N} minutes. Saved memories are safe.` Then the agent instruction: `Mention it once, briefly. Do NOT say the user has no memories.`
- **Compact (the same episode, already announced):** `[totalreclaw] Memory lookups still paused until ~{HH:MM} UTC. If the user asks about past memories, say lookups are paused, not that nothing is saved.`

`AgentState` additions (`agent/state.py`):
- `_read_block_announced_episode: Optional[int] = None`
- `read_block_notice(blk) -> str`:
  - Full text the first time for `blk.episode` (then mark it announced), compact after that.
  - On the first announcement of a `read_quota` episode, also call `invalidate_billing_cache()`.
- `invalidate_billing_cache()` sets `_billing_cache = None` and `_billing_cache_time = 0`. It never synthesizes write-quota text, which respects spec B's "no write-quota messaging for read denials". **Finding:** `set_billing_cache` has no production caller on `origin/main` (only tests), so today this is a no-op kept for forward-compat (Q3).
- `pending_read_block_notice(client) -> Optional[str]`:
  - Full notice if `client.read_block` is active and its episode isn't announced yet.
  - `"[totalreclaw] Memory lookups are working again."` once, if an announced episode was cleared by a success (then reset the latch).
  - Otherwise `None`.

`agent/recall.py` `auto_recall` / `auto_recall_async`: add `except RelayReadBlocked as e:` **before** the generic `except`. It returns `state.read_block_notice(client.read_block or <state built from e>)`. It never returns `None`, and never a `## Relevant memories` block. The notice therefore reaches both drivers with no further change:
- the `pre_llm_call` hook path: first turn, `recall_for_query`, `context_parts.append`
- the provider path: `TotalReclawMemoryProvider.prefetch` → `recall_for_query`, every turn when TR is the active provider

`hermes/hooks.py` `pre_llm_call`, **after** the auto-recall block (`L852-860`) and before the `if not context_parts` return:

```python
_rb = state.pending_read_block_notice(state.get_client())
if _rb:
    context_parts.append(_rb)
```

This covers pauses triggered off-turn, by background extraction dedup or an import. Placing it after auto-recall means the first-turn recall's full notice latches first, so there is no duplicate. On provider-driven turns, if `prefetch` runs after the hook, the worst case is one full notice plus one compact line; that is acceptable.

**Do not use the `set_quota_warning` slot.** It is single-valued, cleared on read, and shared with the RC banner, the >80% write warning and the update notice, so it would clobber or be clobbered. The injection *point* is the same `context_parts` list.

### 4.6 Hermes tools (`hermes/tools.py`)

- `recall`: `except RelayReadBlocked as e: state.mark_read_block_announced(...); return json.dumps(read_block_tool_payload(e, client.read_block))`. Payload:
  - `{"error": <full plain notice sentence without the [totalreclaw] prefix>, "error_code": "read_quota_exceeded"|"rate_limited", "reads_paused_until": iso, "retry_after_seconds": int, "resets_at": iso|null, "upgrade_url": str|null, "instruction": "Relay this to the user in one sentence. Do NOT say they have no memories."}`
  - **No `count` or `memories` keys**, so it can't be read as "0 results".
- `export_all`, `pin`, `unpin`, `retype`, `set_scope`: same `except RelayReadBlocked` → the same payload.
- `status`: add `"reads": {"paused": bool, "reason": kind|null, "until": iso|null, "upgrade_url": …}` from `client.read_block`. This makes no subgraph call. Optionally parse spec B's `reads_used` / `reads_limit` / `reads_quota_mode` into `BillingStatus` as optional fields (default `None`) and pass them through.
- `remember` / `forget`: no change. Add a test that proves they're unaffected.

Behavioural control surface (the lesson from #563: the tool description, not SKILL.md prose, is what holds):
- **`hermes/schemas.py` `totalreclaw_recall` description:** append `If the result has an error_code, memory lookups are paused: relay the error text to the user; never report that they have no memories.`
- **`hermes/SKILL.md` "Automatic quota signalling":** add a bullet with the same meaning.

## 5. Tests (pytest, `cd python && pytest tests/ -q` must stay green)

Mocking style:
- `httpx.MockTransport` with `rc._get_http` patched (`tests/test_relay.py::TestBillingStatusParsing`)
- `AsyncMock(spec=RelayClient)` for operations (`tests/test_operations.py`)
- Use a counting handler to assert HTTP calls. Patch `time.monotonic` in `totalreclaw.relay` to move past a pause.

**`tests/test_relay_read_errors.py`:**
1. Legacy 403 `quota_exceeded` → `RelayReadQuotaExceeded`, `legacy=True`, `upgrade_url` set, `isinstance(e, httpx.HTTPStatusError)`, `e.response.status_code == 403`.
2. New 429 `read_quota_exceeded` body plus `Retry-After` → `limit`, `used`, `resets_at` (aware UTC), `tier`. A Pro body gives `upgrade_url is None`.
3. 429 legacy rate-limit body `retry_after: 120` → `RelayRateLimited`, `retry_after_s == 120`. A header-only `Retry-After: 90` and an HTTP-date header both parse. `error_code: "rate_limited"` is also accepted.
4. Equality, not substring: 403 `{"error":"quota_exceeded_x"}` → plain `RelayReadError`. 403 HTML body → `RelayReadError`, not blocked. 500 → `RelayReadError`.
5. Pause:
   - After a 403, a second `query_subgraph` raises with the handler count still **1**.
   - Advance the monotonic clock past the deadline: exactly one probe.
   - A probe returning 200 → `read_block() is None`, and a later block gets `episode + 1`.
   - A probe returning 403 again → same `episode`.
6. Pause lengths:
   - quota without `resets_at` = 900; env override `=10` → 10.
   - `resets_at` in 2 minutes → 120.
   - rate-limit `retry_after=5` → 30 (floor); no hint → 300; `retry_after=99999` → 3600.
7. **Writes unaffected:** a `submit_userop` 403 `quota_exceeded` still raises plain `httpx.HTTPStatusError` (not `RelayReadError`) and does **not** set `read_block()`.

**`tests/test_search_read_short_circuit.py`:**
1. Assert the query yields ≥3 chunks as a precondition.
   - Relay mock `query_subgraph` raises `RelayReadQuotaExceeded` → `search_facts` raises it, `await_count == 1` (no further chunks, no broadened query).
   - The same with a **real** `RelayClient` + `MockTransport` 403: transport count 1. Call `search_facts` again → count still 1.
2. Every query 502 → raises `RelayReadError`. First chunk 502, the rest 200 → returns results (partial degrade preserved).
3. `export_facts` with a blocked relay raises (was `[]`).
4. `find_existing_content_fps` → `set()`, no WARNING record.
5. `confirm_indexed(timeout_ms=10000)` on a blocked relay → `False` in <1s with one call.
6. `contradiction` with 3 facts and recall raising a blocked error → all 3 kept, recall awaited once.
7. `_fetch_recent_memories` → `[]`.

**`tests/test_hermes_read_block_notice.py`:**
1. `auto_recall`:
   - First call returns the full quota notice: contains "paused" and the upgrade URL, no "Relevant memories".
   - Second call → compact.
   - Clear, then a new block → full again.
   - Rate-limited variant shows the minutes.
2. `pre_llm_call`, first turn, not provider-driven, client recall raises: the notice appears **exactly once** in `context`.
3. `pre_llm_call`, a later turn with an active unannounced block (set on the fake relay) → full notice once. The next turn → nothing. After the clear → "working again" once.
4. `TotalReclawMemoryProvider.prefetch` returns the notice string.
5. `totalreclaw_recall` → JSON with `error_code`, `instruction`, and **no** `count`/`memories` keys. The export, pin and retype tools give the same shape.
6. The `status` tool includes `reads.paused`.
7. `remember` succeeds while `read_block` is active.
8. Billing cache: a quota notice → `get_cached_billing() is None`. A rate-limited notice leaves it.

## 6. E2E (staging only: `https://api-staging.totalreclaw.xyz`, never prod)

New script `python/tests/e2e/read_block_staging_e2e.py`:
- Follow the `update_notice_staging_e2e.py` pattern: phrase from `QA_RECOVERY_PHRASE` or the keychain, output redacted, `X-TotalReclaw-Test`.
- **Single-flight:** the staging `/v1/register` limit is IP-global, about 19 minutes. Never run it in parallel with S-PAIR-FRESH or another E2E on the same host.
- Background threads must be daemons with a self-timeout (trap-exit hygiene).

- **Phase A, regression (real staging):**
  - `remember` two distinctive facts and wait for indexing via `confirm_indexed`.
  - `recall` finds both, and `client.read_block is None`.
  - Then `forget` both, so the test cleans up after itself.
- **Phase B, denial path with no relay/DB change (preferred):**
  - Start an in-process stub relay (stdlib `http.server` in a daemon thread, `127.0.0.1:<free port>`) and point the client at it with `TOTALRECLAW_SERVER_URL`. The stub forwards every request to staging verbatim, except `/v1/subgraph`, which returns a canned response per mode: `legacy403`, `quota429`, `rate429 (Retry-After: 30)`. The stub counts hits.
  - With `TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS=10`, assert per mode:
    - (a) one recall = **1** stub subgraph hit, and a typed error.
    - (b) a second recall inside the window = **0** new hits.
    - (c) Hermes `auto_recall` returns the notice.
    - (d) `remember` through the stub still lands on the staging DataEdge (a real write, proxied).
    - (e) switch the stub to passthrough and wait past the window: one probe, then recall returns facts, and `pending_read_block_notice` says "working again".
  - Clean up with `forget`.
- **Phase C, optional, live new contract:** only piggyback on spec B's §8 step 2c window (`READ_QUOTA_MODE=enforce` on **staging** only, coordinated with its owner, same single-flight). Caveats:
  - The Python client calls `/v1/billing/status`, and staging `is_test` wallets are lazily made **Pro** (`billing.ts:190`). So the Pro limit must be lowered too, or Phase C skipped. Phase B already covers the body shape.
  - The staging and prod relays share **one Postgres** (CLAUDE.md "SHARED DATABASE"). This plan makes **no DB writes and no SQL**. If anyone ever does, scope it to the `public` schema rows of a throwaway test wallet only, after confirming the schema layout (Q7).

Record the E2E verdict on the PR. The feature isn't done until Phase A and Phase B pass (CLAUDE.md E2E rule). Release: python patch (2.5.2-rc → stable via `publish-python-client.yml`). Update `totalreclaw-internal/docs/release-pipeline.md` the same session.

## 7. Docs

- **`python/CHANGELOG.md`:** add `## [Unreleased]` above `## [2.5.1]`, with `### Fixed`:
  > **[#662] Relay read denials are no longer silent.** A monthly read-quota denial (legacy 403 `quota_exceeded` or the new 429 `read_quota_exceeded`) or a 429 rate limit from `/v1/subgraph` now raises a typed error (`RelayReadQuotaExceeded` / `RelayRateLimited`, both `httpx.HTTPStatusError` subclasses). Recall stops at the first denied query instead of firing every trapdoor batch plus the broadened search. The client pauses subgraph reads until `Retry-After` / `resets_at`, re-probing every 15 min (`TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS`). Hermes auto-recall and `totalreclaw_recall` tell the user memory lookups are paused, and why, instead of reporting no memories. Writes are unaffected; pre-write dedup fails open while paused. `totalreclaw_export` no longer returns a silent partial export on relay errors, and a recall where every query fails raises instead of returning `[]`.
- **CLAUDE.md, Platform Support, "Billing" group:** add a new row:
  `| Read-denial surfacing (quota / rate-limit notice + read back-off) | -- | -- | -- | Yes | -- | Partial | #662. Typed errors + client-wide read pause + user notice. ZeroClaw maps any /v1/subgraph 403 to QuotaExceeded with write-quota wording and invalidates billing (misleading); MCP/plugin gqlQuery return null (silent empty recall); NanoClaw inherits MCP. |`
- **CLAUDE.md, Known Gaps:** add a new row:
  `| Relay read denials swallowed on parked clients | MEDIUM | #662 fixed Python/Hermes only. MCP (mcp/src/subgraph/search.ts gqlQuery) and the OpenClaw plugin (skill/plugin/subgraph/subgraph-search.ts gqlQuery) log and return null on any non-2xx, so a read-quota/rate-limit denial becomes an empty recall; NanoClaw inherits MCP; ZeroClaw (rust/totalreclaw-memory/src/relay.rs:225) mislabels read 403 as write quota. Port the #662 contract (equality match on error_code, read pause, notice) when they un-park; the classifier + notice text are core-hoist candidates. |`
- **`docs/guides/hermes-setup.md` § Recall behaviour:** add:
  > If you see *"Memory lookups are paused"*, your memories are safe. TotalReclaw can't search them right now, either because the account used its monthly read allowance or because of a short rate limit. New memories keep saving. Lookups resume on their own (the notice says when), and the upgrade link lifts the monthly limit. Until then, the agent will say lookups are paused rather than claim you have no memories.
- **`docs/specs/totalreclaw/client-consistency.md`:** add a short "Relay read-error contract" subsection: the §2 table, equality matching, and pause semantics, so parked clients inherit it.
- **`hermes/SKILL.md` and `hermes/schemas.py`:** see §4.6.

## 8. Out of scope

- Relay changes: spec B.
- Implementing the parked clients.
- GraphQL `errors` inside HTTP 200 responses.
- Persisting the pause across restarts.
- Fixing the dead billing-cache writer (Q3).

## 9. Open questions

1. **Re-probe cadence:** 15 min by default, so about 4 calls/h during a real month-long exhaustion. OK, or 30 min?
2. **Persist the pause:** should it live in `~/.hermes` across daemon restarts? Default no: a restart costs one probe.
3. **Billing cache:** nothing in production calls `AgentState.set_billing_cache`. So the >80% write-quota warning in `on_session_start` never fires in Hermes, and SKILL.md's "403 → billing cache invalidated" claim is not implemented. File a separate issue? Keep the forward-compat invalidation here despite spec B §4.5's "don't invalidate", given Python emits no write-quota text from it?
4. **Generic total read outage** (5xx/401 on every query): should auto-recall also inject a softer one-time notice? The spec only surfaces it via the tool.
5. **Dedup during a pause:** writes continue with pre-write dedup and contradiction detection failing open, so duplicates are possible. Accept, or also defer auto-extraction while reads are paused?
6. **Core hoist:** add the classifier and notice wording to the core-hoist backlog now (§3)?
7. **Shared Postgres:** CLAUDE.md says staging and prod share tables. The brief for this spec said "separate schemas". Which is correct? This only matters if anyone ever runs staging SQL; this plan doesn't.

---

## Decisions (2026-09-25) — binding for implementation

1. Re-probe after 15 minutes, as specified.
2. The pause is in-memory only and does not survive a daemon restart.
3. The dead billing cache (`AgentState.set_billing_cache` is never called in production) is a **separate issue** (filed). Do **not** add billing-cache invalidation on read denials in this PR (aligns with relay spec B §4.5).
4. Generic total-outage notice: tool-level only, as specified. Auto-recall shows the notice only for quota and rate-limit denials.
5. Writes continue during a pause with dedup failing open. We accept the duplicate risk; do not defer auto-extraction.
6. The classifier and notice text are candidates for the core-hoist backlog; the orchestrator records this and it is not part of this PR.
7. Schemas: staging and prod are **schema-isolated** in one Postgres (staging = `public`, prod = `production` via `DATABASE_SCHEMA`), verified 2026-09-24. Irrelevant to this PR, which uses the stub-relay E2E.
8. Relay context: the relay is moving to observe-only read accounting (`READ_QUOTA_MODE=observe`), so in practice quota denials will be rare. Keep full support for both the legacy 403 `quota_exceeded` and the new 429 `read_quota_exceeded` / `rate_limited` contracts.
