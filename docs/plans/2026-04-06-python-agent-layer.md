# Plan: Generic Python Agent Integration Layer

**Date:** 2026-04-06
**Status:** Planning (not started)

## Problem

The Hermes plugin (`totalreclaw.hermes`) bundles framework-agnostic logic (auto-recall, auto-extract, debrief, state management, LLM client) with Hermes-specific hook registration. This makes it impossible to reuse the same logic for LangChain, CrewAI, or any other Python agent framework without duplicating code.

## Proposed Architecture

```
totalreclaw (base client)         -- remember, recall, forget, export, status
  └── totalreclaw.agent (generic) -- auto-recall, auto-extract, debrief, state mgmt
        ├── totalreclaw.hermes    -- Hermes hook registration only
        ├── totalreclaw.langchain -- LangChain callback handler (future)
        └── totalreclaw.crewai    -- CrewAI integration (future)
```

## What moves into `totalreclaw.agent`

| Current location | Module | What it does |
|---|---|---|
| `hermes/state.py` | `agent.state` | `AgentState` -- turn counter, message buffer, extraction interval, processed index |
| `hermes/llm_client.py` | `agent.llm_client` | `detect_llm_config(configured_model)`, `chat_completion()` |
| `hermes/extractor.py` | `agent.extractor` | `extract_facts_llm()`, `extract_facts_heuristic()`, `_parse_response()` |
| `hermes/hooks.py` (logic) | `agent.lifecycle` | `auto_recall(client, query)`, `auto_extract(client, messages, state)`, `session_debrief(client, messages)` |
| (new) | `agent.dedup` | Near-duplicate cosine check before storing (port from TS `consolidation.ts`) |

## What stays in `totalreclaw.hermes`

Only the thin adapter that registers Hermes lifecycle hooks (`pre_llm_call`, `post_llm_call`, `on_session_end`) and calls the generic `totalreclaw.agent` functions. Roughly 50-80 lines.

## Framework adapter contract

Each adapter implements a minimal interface:

```python
class AgentAdapter:
    """Wire totalreclaw.agent lifecycle into a specific framework."""
    def register(self, client: TotalReclawClient, state: AgentState) -> None: ...
```

For Hermes: register `pre_llm_call` -> `auto_recall`, `post_llm_call` -> `auto_extract`, `on_session_end` -> `session_debrief`.
For LangChain: subclass `BaseCallbackHandler`, call the same functions from `on_llm_start`/`on_llm_end`.
For CrewAI: use CrewAI's task hooks or agent callbacks.

## Migration path

1. Create `totalreclaw/agent/` package with modules extracted from `hermes/`
2. Re-export from `hermes/` for backward compatibility (no breaking changes)
3. Add `totalreclaw.langchain` adapter when there is demand
4. Deprecate direct imports from `totalreclaw.hermes.extractor` etc. after one release cycle

## Non-goals

- No new functionality. This is a refactor to enable reuse.
- No immediate LangChain/CrewAI implementation. Just the generic layer.
- No changes to the base `totalreclaw` client (crypto, relay, LSH).
