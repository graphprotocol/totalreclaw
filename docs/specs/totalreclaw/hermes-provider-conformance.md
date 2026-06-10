# Hermes Provider Conformance — make TotalReclaw a native Hermes `MemoryProvider`

**Status:** Design / proposal (2026-06-10)
**Owner:** TBD · **Reviewer:** Pedro (product owner)
**Prereq:** [#346](https://github.com/p-diogo/totalreclaw/pull/346) (sidecar path fix) — draft, ships as part of this work
**Tracking issue:** _(linked on open)_

---

## 1. Problem

On Hermes, TotalReclaw integrates **differently from every other memory system**, and the difference is the direct cause of two live defects observed on a production box (pop-os, 2.4.5rc1, 2026-06-10):

- **Split-brain.** `memory.provider: totalreclaw` is set in `~/.hermes/config.yaml`, but Hermes cannot resolve it (the discovery sidecar is installed at the wrong path — see #346), so Hermes registers only its **builtin** provider. With `memory_enabled: true`, the builtin local store loads/injects `~/.hermes/memories/MEMORY.md` and maintains `~/.hermes/profiles/` **in parallel** with TotalReclaw (which writes on-chain via a separate hook path). The user cannot be sure TR is the sole memory system — because it isn't.
- **Background-review denial + double-fire risk.** TR drives auto-memory through raw `pre_llm_call`/`post_llm_call` lifecycle hooks registered by a generic tools-plugin, *and* ships a half-implemented `MemoryProvider` sidecar. Activating the provider (the #346 path fix alone) makes **both** paths run → `on_session_start`/`on_session_end` fire twice + memory injected twice. The explicit `totalreclaw_*` tools also live in toolset `totalreclaw` (not `memory`), so the background-review sandbox denies `totalreclaw_recall`.

## 2. Evidence — the universal provider contract

Hermes ships 8 memory providers (`plugins/memory/`): `honcho`, `mem0`, `supermemory`, `hindsight`, `byterover`, `holographic`, `openviking`, `retaindb`. **All 8 follow an identical shape** (surveyed 2026-06-10):

```python
# plugins/memory/<name>/__init__.py
class XMemoryProvider(MemoryProvider): ...
def register(ctx):
    ctx.register_memory_provider(XMemoryProvider())
```

Method contract implemented by **every** one:

| Method | Role |
|---|---|
| `prefetch()` | **auto-recall** — context for the upcoming turn |
| `sync_turn()` | **auto-persist** — extract/store the completed turn |
| `get_tool_schemas()` + `handle_tool_call()` | explicit memory tools |
| `system_prompt_block()` | prompt injection |
| `initialize` / `is_available` / `name` / `shutdown` | lifecycle |

Hermes's `agent/memory_manager.py` orchestrates all of it (`prefetch_all`, `queue_prefetch_all`, `sync_turn` per turn). **Zero** of the 8 use `pre_llm_call` / `post_llm_call` / `register_hook`. Auto-memory *is* `prefetch` + `sync_turn`. Backends differ wildly (honcho = HTTP SaaS, hindsight = local, mem0 = SaaS); the **interface is uniform**.

## 3. How TotalReclaw deviates

| | The 8 native providers | TotalReclaw (today) |
|---|---|---|
| Registration | `ctx.register_memory_provider(...)` | `hermes_agent.plugins` **entry point** (generic tools-plugin) — only one |
| Auto-memory | provider `prefetch` + `sync_turn` | raw `pre_llm_call` / `post_llm_call` hooks |
| Provider methods | implement `prefetch`/`sync_turn` | **no-ops** — sidecar is a half-provider |
| Sidecar discovery path | n/a (bundled) | wrong path → never loads (#346) |

The deviation is **accidental, not essential**:

1. **History** — TR's hooks/tools layer was built first to serve all clients (OpenClaw, MCP, ZeroClaw, Hermes) uniformly; the Hermes `MemoryProvider` was bolted on later as a re-export sidecar without migrating the recall/extract logic into `prefetch`/`sync_turn`.
2. **Multi-client** — TR is the only one of the 9 targeting 5 runtimes. But this is satisfied *better* by conformance, not worse: the shared recall/extract logic becomes a core that the provider (Hermes) and the lifecycle hooks (other clients) both call. This is exactly the `CLAUDE.md` principle: **shared core, client-native adapter.**

There is **no E2EE / on-chain reason** to deviate — the crypto/on-chain specifics live *inside* the methods, precisely where every other provider's backend calls live.

## 4. Goal

Make TotalReclaw's Hermes integration conform to the universal `MemoryProvider` contract: **one provider, one driver, zero raw lifecycle hooks on Hermes.** Preserve the multi-client shared core unchanged.

Definition of done:
- `load_memory_provider("totalreclaw")` returns an `is_available()` provider that implements `prefetch` + `sync_turn`.
- On Hermes, TR auto-recall/extract runs **only** through the provider (no `pre_llm_call`/`post_llm_call`).
- `memory.provider: totalreclaw` resolves; builtin no longer injects local memory (no split-brain).
- Background-review can use the native `memory` path; `totalreclaw_recall` denial gone.
- No double-fire of `on_session_start`/`on_session_end`/`system_prompt_block`.
- OpenClaw / MCP / ZeroClaw behavior **unchanged** (they keep their lifecycle-hook wiring).

## 5. Design

### 5.1 Shared core (no behavior change)
The recall + extract logic currently invoked from `hermes/hooks.py:pre_llm_call`/`post_llm_call` must be expressed as plain functions on `PluginState` (or a small service object) with **no Hermes-hook coupling** — e.g. `state.recall_for_query(query) -> context_str` and `state.ingest_turn(messages) -> None`. These are already most of what the hooks call; this step is mostly extracting/naming, not rewriting. Both the provider and the legacy hooks call these.

### 5.2 Provider conformance (`hermes/memory_provider.py`)
Implement the two missing methods over the shared core:

```python
def prefetch(self, query: str, *, session_id: str = "") -> str:
    return self._state().recall_for_query(query)          # auto-recall

def sync_turn(self, messages, *, session_id="", **kw) -> None:
    self._state().ingest_turn(messages)                   # auto-persist (extract)
```

Keep `get_tool_schemas`/`handle_tool_call` (already wired → routes the `totalreclaw_*` tools through the provider). **Drop** the provider's `on_turn_start`/`on_session_end`/`initialize` side-effects that duplicate the hooks once 5.3 lands — or keep exactly one of {provider, hooks} owning each lifecycle moment. (Decide per-method; see Open Questions.)

### 5.3 Hermes wiring — single driver
In `hermes/__init__.py:register()`, **stop registering** `pre_llm_call` / `post_llm_call` (and reconcile `on_session_start`/`on_session_end`) when running under Hermes-as-provider. Options:

- **A (preferred):** `register()` registers **tools only**; all auto-memory + lifecycle moves to the provider. Cleanest, matches the 8. Requires the provider to own `prefetch`/`sync_turn`/session lifecycle.
- **B:** keep a single set of hooks as the driver and make the provider expose **tool-routing + `system_prompt_block` only** (drop its lifecycle overrides). Less native; still single-fire.

Recommendation: **A.** It is what every other provider does and removes the entry-point memory hooks entirely from the Hermes path.

> Multi-client note: the lifecycle hooks are **not** deleted from the codebase — they remain the wiring for OpenClaw / MCP / ZeroClaw. Only the **Hermes** registration stops wiring memory through them. Gate by client, not by deletion.

### 5.4 Config / builtin
`install_memory_provider` already writes `provider: totalreclaw` (`set_active_provider`). Ensure the installer also disables the builtin local store so it stops injecting `MEMORY.md`:
- `memory_enabled: false` **and** `user_profile_enabled: false` (the builtin `MemoryStore` loads if *either* is true — `agent_init.py:1117`).
- Verify the **external provider still runs** with those false (the provider block at `agent_init.py:1131+` is gated on `memory.provider` being set, independent of `memory_enabled` — confirm in E2E).
- Local-files mode = user sets `provider: builtin` (or `none`) — the explicit opt-in described in the requirement.

### 5.5 Sidecar path (#346)
Prerequisite. Sidecar at `$HERMES_HOME/plugins/<name>/` (not `…/plugins/memory/<name>/`). Includes legacy migration. Already implemented in #346; lands as part of this work (un-draft once 5.1–5.4 are green).

## 6. Rollout

1. Land shared-core extraction (5.1) — pure refactor, no behavior change, full test parity.
2. Land provider conformance (5.2) behind a flag/unreleased.
3. Land Hermes single-driver wiring (5.3) + config (5.4) + #346.
4. **E2E gate** (§7) on a live Hermes before release.
5. Cut RC → QA → promote.

## 7. Validation (E2E on a live Hermes box)

1. `load_memory_provider("totalreclaw")` → provider, `is_available: True`; `memory.provider: totalreclaw` resolves.
2. One conversation turn: assert **single** `on_session_start`, **single** recall, **single** extract (log-count, no doubling).
3. Builtin silent: `MEMORY.md` not injected; no new local-profile writes; status/recall sourced only from TR.
4. Background-review can call the native `memory` path; no `non-whitelisted tool: totalreclaw_recall` denial.
5. `provider: builtin` switch → local mode works (round-trip), then switch back.
6. Cross-client regression: OpenClaw/MCP/ZeroClaw memory unchanged (their hooks still fire).

## 8. Open questions

- **Lifecycle ownership** — for `on_session_start`/`on_session_end`/session-finalize (debrief/flush), which single owner: provider or a retained Hermes hook? `sync_turn` is per-turn; debrief is session-scoped — map carefully to avoid losing debrief or double-running it.
- **`memory_enabled:false` vs provider liveness** — confirm empirically the provider's `prefetch`/`sync_turn` are driven when the builtin is disabled. If `memory_manager` is gated off when builtin is off, we need a different lever to silence builtin while keeping the provider live.
- **Tool surface** — once the provider exposes tools via `get_tool_schemas`, do we still also register the standalone `totalreclaw_*` tools via the entry point on Hermes? Avoid registering the same tool twice (provider + entry point). Pick one surface on Hermes.
- **Quota/billing-cache, import tools, pair tool** — these live in the entry-point plugin today; confirm they remain available when `register()` drops the memory hooks (they are tools, not memory hooks, so should be unaffected — verify).

## 9. Non-goals

- Changing OpenClaw / MCP / ZeroClaw integration (they stay hook-based).
- Reworking the on-chain / E2EE pipeline (unchanged — it lives inside the shared core).
- Upstreaming anything to NousResearch's hermes-agent (the user-provider path needs no upstream change).

## 10. Interim stopgap (separate, shippable now)

Until this lands, **Strategy 1** makes TR the sole memory safely: set `memory_enabled: false` + `user_profile_enabled: false` so the builtin stops injecting, leaving TR's existing hook-based integration as the lone memory system. No provider activation, no double-fire. (Also fix the installer so this reliably sticks — it did not on the 2026-06-10 box.)
