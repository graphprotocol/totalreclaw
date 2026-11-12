"""Store-time write gate: one near-duplicate check shared by every write path.

Internal #632 / #501. Before the gate, only auto-extraction checked for near
duplicates, and every check read the subgraph, so:

* the USER.md mirror, reconcile and explicit ``totalreclaw_remember`` wrote
  punctuation variants of facts already stored ("User is vegetarian" and
  "User is vegetarian.");
* two writes a minute apart could not see each other (subgraph indexing lag).

The gate (``totalreclaw.write_gate``) checks a normalized exact match and an
embedding cosine match against topical subgraph candidates plus a
process-local ledger of facts written in the last few minutes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.embedding import get_embedding as _REAL_GET_EMBEDDING
from totalreclaw.reranker import RerankerResult

DIMS = 8


def _unit(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec]


def _hash_vec(text: str) -> list[float]:
    """Deterministic pseudo-random unit vector: unrelated texts are far apart."""
    digest = hashlib.sha256(text.encode()).digest()
    return _unit([(b - 127.5) for b in digest[:DIMS]])


# Hand-placed vectors: a paraphrase pair at cosine ~0.97 and an unrelated
# fact far from both.
_VEG = _unit([1, 0, 0, 0, 0, 0, 0, 0])
_VEG_PARA = _unit([1, 0.25, 0, 0, 0, 0, 0, 0])
_TEA = _unit([0, 0, 1, 0, 0, 0, 0, 0])
_FIXED = {
    "user is vegetarian": _VEG,
    "the user follows a vegetarian diet": _VEG_PARA,
    "user prefers green tea": _TEA,
}


def fake_embedding(text: str) -> list[float]:
    key = text.lower().strip().rstrip(".!")
    return list(_FIXED.get(key, _hash_vec(key)))


class LaggyClient:
    """Fake vault client. Writes land immediately, but the subgraph (recall)
    only sees them after ``index()`` is called — the indexing lag of #501."""

    resolved_chain_id = 84532
    resolved_wallet_address = "0x000000000000000000000000000000000000beef"
    eoa_address = "0x000000000000000000000000000000000000cafe"

    def __init__(self):
        self.facts: dict[str, dict] = {}  # id -> {text, embedding, pinned}
        self.indexed: set[str] = set()
        self.writes: list[str] = []
        self.forgotten: list[str] = []
        self._n = 0

    def _new_id(self) -> str:
        self._n += 1
        return f"fact-{self._n}"

    def index(self):
        self.indexed = set(self.facts)

    def seed(self, text, pinned=False, indexed=True):
        fid = self._new_id()
        self.facts[fid] = {"text": text, "embedding": fake_embedding(text), "pinned": pinned}
        if indexed:
            self.indexed.add(fid)
        return fid

    async def remember(self, text, embedding=None, **kw):
        fid = self._new_id()
        self.facts[fid] = {"text": text, "embedding": embedding, "pinned": False}
        self.writes.append(text)
        return fid

    async def remember_batch(self, facts, source="python-client"):
        return [await self.remember(f["text"], embedding=f.get("embedding")) for f in facts]

    async def forget(self, fact_id, *, suppress_reextraction=False):
        self.facts.pop(fact_id, None)
        self.forgotten.append(fact_id)
        # Same ledger contract as TotalReclaw.forget.
        from totalreclaw.write_gate import ledger_for

        ledger_for(self).forget(fact_id)
        # The subgraph keeps showing it until index().
        return {"success": True, "fact_id": fact_id}

    async def recall(self, query, query_embedding=None, top_k=8, include_embeddings=True, **kw):
        out = []
        for fid in self.indexed:
            f = self.facts.get(fid)
            if f is None:
                # Tombstoned but the subgraph has not caught up.
                f = self._ghosts.get(fid)
            if f is None:
                continue
            out.append(
                RerankerResult(
                    id=fid,
                    text=f["text"],
                    embedding=f["embedding"] if include_embeddings else [],
                    pinned=f["pinned"],
                )
            )
        return out[:top_k]

    async def fetch_recent_facts(self, limit=10):
        return []

    async def get_fact_pin_status(self, fact_id):
        f = self.facts.get(fact_id)
        return bool(f and f["pinned"])

    _ghosts: dict = {}


def _plugin_state(client):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client
    return state


@pytest.fixture(autouse=True)
def _fake_embeddings():
    with patch("totalreclaw.embedding.get_embedding", side_effect=fake_embedding):
        yield


def _remember(state, text, **args):
    from totalreclaw.agent.loop_runner import run_sync
    from totalreclaw.hermes import tools

    return json.loads(run_sync(tools.remember({"text": text, **args}, state)))


# -- 1. mirror + explicit remember ----------------------------------------------


def test_mirror_then_explicit_remember_of_same_fact_writes_once():
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = LaggyClient()
    state = _plugin_state(client)

    assert _mirror_remember(state, {"text": "User is vegetarian."}) is True
    # Subgraph has not indexed the mirrored fact yet (lag).
    out = _remember(state, "User is vegetarian")

    assert client.writes == ["User is vegetarian."]
    assert out["stored"] is False
    assert out["already_stored"] is True
    assert out["fact_id"] == "fact-1"


def test_explicit_remember_then_mirror_of_same_fact_writes_once_after_indexing():
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = LaggyClient()
    state = _plugin_state(client)

    assert _remember(state, "User is vegetarian")["stored"] is True
    client.index()  # now visible to the subgraph as well
    assert _mirror_remember(state, {"text": "user is vegetarian."}) is True
    assert client.writes == ["User is vegetarian"]


def test_reconcile_style_store_skips_exact_copy_already_in_vault():
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = LaggyClient()
    client.seed("User is vegetarian")  # written by auto-extraction days ago
    state = _plugin_state(client)

    assert _mirror_remember(state, {"text": "User is vegetarian."}) is True
    assert client.writes == []


# -- 2. back-to-back extractions within the lag window ---------------------------


def _auto_extract_once(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*a, **kw):
        return [
            ExtractedFact(text=t, type="claim", importance=7, action="ADD", source="user")
            for t in facts
        ]

    async def passthrough(fs, *a, **kw):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough):
        state.add_message("user", "By the way, I'm vegetarian and I like green tea.")
        state.add_message("assistant", "Noted.")
        return auto_extract(state)


def test_two_back_to_back_extractions_of_same_fact_write_once():
    client = LaggyClient()
    state = _plugin_state(client)

    _auto_extract_once(state, ["User is vegetarian"])
    # Nothing indexed yet: the second run's subgraph read cannot see fact-1.
    _auto_extract_once(state, ["User is vegetarian."])

    assert client.writes == ["User is vegetarian"]


def test_back_to_back_extraction_paraphrase_is_caught_by_ledger_cosine():
    client = LaggyClient()
    state = _plugin_state(client)

    _auto_extract_once(state, ["User is vegetarian"])
    _auto_extract_once(state, ["The user follows a vegetarian diet"])

    assert client.writes == ["User is vegetarian"]


def test_back_to_back_extraction_of_different_fact_is_stored():
    client = LaggyClient()
    state = _plugin_state(client)

    _auto_extract_once(state, ["User is vegetarian"])
    _auto_extract_once(state, ["User prefers green tea"])

    assert client.writes == ["User is vegetarian", "User prefers green tea"]


def test_extraction_then_mirror_of_same_fact_writes_once():
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = LaggyClient()
    state = _plugin_state(client)

    _auto_extract_once(state, ["User is vegetarian"])
    assert _mirror_remember(state, {"text": "User is vegetarian."}) is True
    assert client.writes == ["User is vegetarian"]


# -- 3. paraphrase vs different fact ----------------------------------------------


def test_paraphrase_above_threshold_is_caught_and_different_fact_is_not():
    from totalreclaw.write_gate import decide_write

    cands = [{"id": "f1", "text": "User is vegetarian", "embedding": _VEG}]
    para = decide_write("The user follows a vegetarian diet", _VEG_PARA, cands, mode="auto")
    assert para.action == "skip"
    assert para.match.kind == "near_duplicate"
    assert para.match.similarity >= 0.85

    other = decide_write("User prefers green tea", _TEA, cands, mode="auto")
    assert other.action == "store"
    assert other.match is None


def test_explicit_remember_of_paraphrase_asks_for_confirmation_then_force_stores():
    client = LaggyClient()
    client.seed("User is vegetarian")
    state = _plugin_state(client)

    out = _remember(state, "The user follows a vegetarian diet")
    assert out["stored"] is False
    assert out["possible_duplicate"] is True
    assert out["existing_text"] == "User is vegetarian"
    assert client.writes == []

    forced = _remember(state, "The user follows a vegetarian diet", force=True)
    assert forced["stored"] is True
    assert client.writes == ["The user follows a vegetarian diet"]


def test_explicit_remember_of_different_fact_is_stored():
    client = LaggyClient()
    client.seed("User is vegetarian")
    state = _plugin_state(client)

    assert _remember(state, "User prefers green tea")["stored"] is True
    assert client.writes == ["User prefers green tea"]


# -- pins, forget, edits ---------------------------------------------------------


def test_exact_duplicate_of_pinned_fact_is_already_stored_and_pin_untouched():
    client = LaggyClient()
    pid = client.seed("User is allergic to penicillin", pinned=True)
    state = _plugin_state(client)

    out = _remember(state, "user is allergic to penicillin.", force=True)
    assert out["stored"] is False
    assert out["already_stored"] is True
    assert out["fact_id"] == pid
    assert out["pinned"] is True
    assert client.writes == [] and client.forgotten == []


def test_extraction_add_near_a_pinned_fact_is_stored_as_new_fact():
    """The prompt tells the LLM to ADD new information about a pinned fact;
    the cosine gate must not then drop that ADD."""
    client = LaggyClient()
    client.seed("User is vegetarian", pinned=True)
    state = _plugin_state(client)

    _auto_extract_once(state, ["The user follows a vegetarian diet"])
    assert client.writes == ["The user follows a vegetarian diet"]
    assert client.forgotten == []


def test_remember_after_forget_within_lag_window_is_stored():
    from totalreclaw.agent.loop_runner import run_sync
    from totalreclaw.hermes import tools

    client = LaggyClient()
    state = _plugin_state(client)
    first = _remember(state, "User is vegetarian")
    client.index()
    LaggyClient._ghosts = {first["fact_id"]: dict(client.facts[first["fact_id"]])}
    try:
        res = json.loads(run_sync(tools.forget({"fact_id": first["fact_id"]}, state)))
        assert res.get("error") is None
        # The subgraph still lists the tombstoned fact (lag); the ledger
        # knows it was forgotten.
        again = _remember(state, "User is vegetarian")
        assert again["stored"] is True
    finally:
        LaggyClient._ghosts = {}
    assert client.writes == ["User is vegetarian", "User is vegetarian"]


def test_mirror_replace_is_not_blocked_by_the_fact_it_replaces():
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = LaggyClient()
    old = client.seed("User is vegetarian")
    state = _plugin_state(client)

    # Same text after normalization would normally be "already stored", but
    # the caller is replacing exactly that fact.
    assert _mirror_remember(state, {"text": "User is vegetarian!"}, exclude_ids=[old]) is True
    assert client.writes == ["User is vegetarian!"]


# -- ledger ------------------------------------------------------------------------


def test_ledger_expires_entries_and_tracks_forgets():
    from totalreclaw.write_gate import RecentWriteLedger

    now = [1000.0]
    led = RecentWriteLedger(ttl_seconds=60, clock=lambda: now[0])
    led.record("a", "User is vegetarian", [1.0, 0.0])
    led.record("b", "User likes tea", None)
    assert {c["id"] for c in led.candidates()} == {"a", "b"}
    led.forget("a")
    assert {c["id"] for c in led.candidates()} == {"b"}
    assert "a" in led.forgotten_ids()
    now[0] += 61
    assert led.candidates() == []
    assert led.forgotten_ids() == set()


def test_ledger_rename_moves_entry_and_marks_pin():
    from totalreclaw.write_gate import RecentWriteLedger

    led = RecentWriteLedger()
    led.record("old", "User is vegetarian", [1.0])
    led.rename("old", "new", pinned=True)
    (entry,) = led.candidates()
    assert entry["id"] == "new" and entry["pinned"] is True
    assert "old" in led.forgotten_ids()


def test_ledger_is_bounded():
    from totalreclaw.write_gate import RecentWriteLedger

    led = RecentWriteLedger(max_entries=3)
    for i in range(5):
        led.record(f"f{i}", f"fact {i}", None)
    assert [c["id"] for c in led.candidates()] == ["f2", "f3", "f4"]


def test_ledger_never_persists_to_disk(tmp_path, monkeypatch):
    from totalreclaw.write_gate import RecentWriteLedger

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    led = RecentWriteLedger()
    led.record("a", "secret plaintext fact", [0.1])
    assert list(tmp_path.iterdir()) == []


# -- real client keeps the ledger in step --------------------------------------


class _StubClient:
    """Bare object for calling TotalReclaw methods unbound (no keys needed)."""

    _wallet_address = "0x" + "ab" * 20
    _relay = None
    _signing_priv_key = b"\x00" * 32
    _eoa_address = "0x" + "cd" * 20
    _data_edge_address = None
    _keys = None

    async def _ensure_address(self):
        pass

    async def _ensure_registered(self):
        pass

    async def _ensure_chain_id(self):
        return 84532


async def test_totalreclaw_forget_marks_ledger_forgotten():
    from totalreclaw.client import TotalReclaw
    from totalreclaw.write_gate import ledger_for

    stub = _StubClient()
    ledger_for(stub).record("f1", "User is vegetarian", None)
    with patch("totalreclaw.client.forget_fact", return_value=True):
        res = await TotalReclaw.forget(stub, "f1")
    assert bool(res)
    assert ledger_for(stub).candidates() == []
    assert "f1" in ledger_for(stub).forgotten_ids()


async def test_totalreclaw_pin_moves_ledger_entry_to_new_id():
    from totalreclaw.client import TotalReclaw
    from totalreclaw.write_gate import ledger_for

    stub = _StubClient()
    ledger_for(stub).record("f1", "User is vegetarian", None)
    result = {"success": True, "fact_id": "f1", "new_fact_id": "f2"}
    with patch("totalreclaw.client.pin_fact", return_value=result):
        assert await TotalReclaw.pin_fact(stub, "f1") == result
    (entry,) = ledger_for(stub).candidates()
    assert entry["id"] == "f2" and entry["pinned"] is True


async def test_totalreclaw_failed_forget_leaves_ledger_alone():
    from totalreclaw.client import TotalReclaw
    from totalreclaw.write_gate import ledger_for

    stub = _StubClient()
    ledger_for(stub).record("f1", "User is vegetarian", None)
    with patch("totalreclaw.client.forget_fact", return_value=False):
        await TotalReclaw.forget(stub, "f1")
    assert [c["id"] for c in ledger_for(stub).candidates()] == ["f1"]


# -- real model (only when Harrier is already cached; never downloads) ---------


def _harrier_cached() -> bool:
    try:
        from totalreclaw.embedding import is_model_available_locally

        return is_model_available_locally()
    except Exception:
        return False


@pytest.mark.skipif(not _harrier_cached(), reason="Harrier model not cached locally")
def test_real_embeddings_paraphrase_caught_unrelated_not():
    """Measured with Harrier: paraphrase 0.944, unrelated 0.734 (threshold 0.85)."""
    from totalreclaw.write_gate import decide_write

    embed = _REAL_GET_EMBEDDING
    base = "User drinks black coffee every morning"
    cands = [{"id": "f1", "text": base, "embedding": embed(base)}]
    para_text = "User has black coffee each morning"
    other_text = "User prefers green tea in the afternoon"
    para = decide_write(para_text, embed(para_text), cands, mode="auto")
    other = decide_write(other_text, embed(other_text), cands, mode="auto")
    assert para.action == "skip", para
    assert other.action == "store", other
