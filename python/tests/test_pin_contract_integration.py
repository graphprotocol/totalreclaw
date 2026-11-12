"""PRD-04 G2 (in-process): a pinned fact survives three contradicting extractions.

Runs the real ``auto_extract`` pipeline three times against an in-memory vault.
Real: ``apply_pin_guard``, ``detect_and_resolve_contradictions`` and
``totalreclaw_core.resolve_with_candidates`` (core is NOT mocked). Stubbed:
the extraction LLM (one scripted fact per round) and embeddings (fixed
vectors). The dedup-context fetch runs for real against the in-memory vault.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from totalreclaw.agent.extraction import ExtractedEntity, ExtractedFact
from totalreclaw.reranker import RerankerResult

PINNED_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PINNED_TEXT = "Pedro's home city is Lisbon"
PINNED_EMBEDDING = [1.0, 0.0, 0.0, 0.0]
# cosine 0.6 against PINNED_EMBEDDING: inside core's contradiction band [0.3, 0.85)
NEW_FACT_EMBEDDING = [0.6, 0.8, 0.0, 0.0]


class InMemoryVault:
    """The slice of ``TotalReclaw`` the auto-extraction pipeline calls."""

    def __init__(self, pinned: bool) -> None:
        self.facts: dict[str, dict] = {
            PINNED_ID: {
                "text": PINNED_TEXT,
                "embedding": list(PINNED_EMBEDDING),
                "pinned": pinned,
                "active": True,
                "entities": [{"n": "Pedro", "tp": "person"}],
            }
        }
        self.forgotten: list[str] = []
        self.stored_texts: list[str] = []
        self.resolved_chain_id = 100
        self._relay = None
        self._next = 0

    async def recall(self, query, query_embedding=None, top_k=8, max_candidates=250):
        out = []
        for fact_id, f in self.facts.items():
            if not f["active"]:
                continue
            out.append(RerankerResult(
                id=fact_id, text=f["text"], embedding=list(f["embedding"]),
                importance=0.9, created_at=time.time() - 86400, category="claim",
                pinned=f["pinned"], entities=list(f["entities"]),
            ))
        return out[:top_k]

    async def get_fact_pin_status(self, fact_id):
        f = self.facts.get(fact_id)
        return bool(f and f["active"] and f["pinned"])

    async def forget(self, fact_id):
        self.forgotten.append(fact_id)
        if fact_id in self.facts:
            self.facts[fact_id]["active"] = False
        return True

    async def remember_batch(self, facts, source="python-client"):
        ids = []
        for d in facts:
            self._next += 1
            fact_id = f"new-{self._next}"
            self.facts[fact_id] = {
                "text": d["text"], "embedding": list(d.get("embedding") or []),
                "pinned": False, "active": True, "entities": [],
            }
            self.stored_texts.append(d["text"])
            ids.append(fact_id)
        return ids


def _state_with(vault: InMemoryVault):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = vault
    return state


def _extract_round(state, fact: ExtractedFact):
    from totalreclaw.agent.lifecycle import auto_extract

    async def scripted_llm(*args, **kwargs):
        return [fact]

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=scripted_llm), \
         patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_FACT_EMBEDDING)):
        state.add_message("user", "An update about where I live")
        state.add_message("assistant", "Got it")
        return auto_extract(state)


CONTRADICTIONS = [
    # 1. LLM rewrites the pinned fact (no entities -> the resolver skips it,
    #    the pin guard must stop it).
    ExtractedFact(text="Pedro's home city is Porto", type="claim", importance=8,
                  action="UPDATE", existing_fact_id=PINNED_ID, source="user"),
    # 2. LLM deletes the pinned fact (pin guard).
    ExtractedFact(text=PINNED_TEXT, type="claim", importance=8,
                  action="DELETE", existing_fact_id=PINNED_ID, source="user"),
    # 3. LLM adds a contradicting fact sharing the "Pedro" entity (core resolver
    #    with pin state: SkipNew { ExistingPinned }).
    ExtractedFact(text="Pedro's home city is Madrid", type="claim", importance=8,
                  action="ADD", source="user",
                  entities=[ExtractedEntity(name="Pedro", type="person")]),
]


def test_pinned_fact_survives_three_contradicting_extractions() -> None:
    vault = InMemoryVault(pinned=True)
    state = _state_with(vault)
    for fact in CONTRADICTIONS:
        _extract_round(state, fact)
    assert vault.forgotten == []
    assert vault.facts[PINNED_ID]["active"] is True
    assert vault.stored_texts == []


def test_control_unpinned_fact_is_updated() -> None:
    """Same first round with the pin removed: the harness must observe the
    tombstone, so the pinned test above cannot pass vacuously."""
    vault = InMemoryVault(pinned=False)
    state = _state_with(vault)
    _extract_round(state, CONTRADICTIONS[0])
    assert vault.forgotten == [PINNED_ID]
    assert vault.stored_texts == ["Pedro's home city is Porto"]
