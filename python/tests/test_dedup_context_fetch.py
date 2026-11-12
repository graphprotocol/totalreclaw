"""PRD-04 DEP-3 Part B: topical dedup context for Hermes auto-extraction.

``agent.lifecycle._fetch_recent_memories`` now fetches topical top-20 (with
embeddings), the facts written in the last few minutes (write-gate ledger)
and the 10 newest facts. ``_dedup_context_block`` renders them with core
``build_dedup_context``: pinned facts first and marked read-only.
"""
from __future__ import annotations

import base64
import json
from unittest import mock
from unittest.mock import patch

import pytest

from totalreclaw import operations
from totalreclaw.agent import lifecycle
from totalreclaw.agent.extraction import (
    DEDUP_CONTEXT_HEADER,
    DEDUP_CONTEXT_PINNED_NOTE,
    ExtractedFact,
    _build_fact,
    _memories_prompt_suffix,
    deduplicate_facts_by_embedding,
)
from totalreclaw.agent.loop_runner import InterpreterShutdownError
from totalreclaw.crypto import encrypt
from totalreclaw.relay import RelayReadBlocked
from totalreclaw.reranker import RerankerResult
from totalreclaw.write_gate import ledger_for


class _State:
    def __init__(self, client):
        self._client = client

    def get_client(self):
        return self._client

    def get_max_candidate_pool(self):
        return 250


class _Client:
    def __init__(self, topical=None, recent=None, recall_exc=None, recent_exc=None):
        self.topical = topical or []
        self.recent = recent or []
        self.recall_exc = recall_exc
        self.recent_exc = recent_exc
        self.recall_calls = []

    async def recall(self, query, **kw):
        self.recall_calls.append((query, kw))
        if self.recall_exc:
            raise self.recall_exc
        return self.topical

    async def fetch_recent_facts(self, limit=10):
        if self.recent_exc:
            raise self.recent_exc
        return self.recent[:limit]


def _msgs(*contents):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": c} for i, c in enumerate(contents)]


@pytest.fixture(autouse=True)
def _no_model():
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False):
        yield


def test_topical_recall_asks_for_embeddings_top20_and_capped_query():
    client = _Client()
    long = "x" * 400
    msgs = _msgs("old message one", "old two", long, "b" * 300, "c", "d")
    lifecycle._fetch_recent_memories(_State(client), msgs, "turn")
    (query, kw), = client.recall_calls
    assert kw["include_embeddings"] is True
    assert kw["top_k"] == 20
    assert len(query) <= 500
    assert query.startswith("x")  # last 4 messages only
    assert "old" not in query


def test_window_embedding_used_only_when_model_is_local():
    client = _Client()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", return_value=[0.5, 0.5]) as emb:
        lifecycle._fetch_recent_memories(_State(client), _msgs("hello there"), "turn")
    assert client.recall_calls[0][1]["query_embedding"] == [0.5, 0.5]
    emb.assert_called_once()

    client2 = _Client()
    with patch("totalreclaw.embedding.get_embedding") as emb2:
        lifecycle._fetch_recent_memories(_State(client2), _msgs("hello there"), "turn")
    assert client2.recall_calls[0][1]["query_embedding"] is None
    emb2.assert_not_called()


def test_sections_union_ledger_and_pinned_flag():
    client = _Client(
        topical=[
            RerankerResult(id="t1", text="User lives in Lisbon", embedding=[1.0], pinned=False),
            RerankerResult(id="p1", text="User is allergic to penicillin", embedding=[0.5], pinned=True),
        ],
        recent=[
            {"id": "r1", "text": "User booked a flight", "embedding": None, "pinned": False},
            {"id": "t1", "text": "User lives in Lisbon", "embedding": None, "pinned": False},
        ],
    )
    ledger_for(client).record("l1", "User just said this", [0.1])
    mems = lifecycle._fetch_recent_memories(_State(client), _msgs("hi there"), "turn")
    assert [(m["id"], m["section"], m["pinned"]) for m in mems] == [
        ("t1", "topical", False),
        ("p1", "topical", True),
        ("l1", "ledger", False),
        ("r1", "recent", False),
    ]
    block = lifecycle._dedup_context_block(mems)
    lines = block.split("\n")
    assert lines[0] == DEDUP_CONTEXT_HEADER
    assert lines[1] == "[ID: p1] [pinned, read-only] User is allergic to penicillin"
    assert lines[2] == "[ID: t1] User lives in Lisbon"
    assert lines[3] == "[ID: l1] User just said this"
    assert lines[4] == "[ID: r1] User booked a flight"
    assert lines[-1] == DEDUP_CONTEXT_PINNED_NOTE


def test_forgotten_fact_is_dropped_from_context():
    client = _Client(topical=[RerankerResult(id="gone", text="old fact", embedding=[1.0])])
    ledger_for(client).forget("gone")
    assert lifecycle._fetch_recent_memories(_State(client), _msgs("hi there"), "turn") == []


def test_one_failing_section_does_not_empty_the_others():
    client = _Client(
        recall_exc=RuntimeError("subgraph 502"),
        recent=[{"id": "r1", "text": "Recent fact", "embedding": None}],
    )
    mems = lifecycle._fetch_recent_memories(_State(client), _msgs("hi there"), "turn")
    assert [m["id"] for m in mems] == ["r1"]


def test_reads_paused_everywhere_gives_empty_context():
    exc = RelayReadBlocked.__new__(RelayReadBlocked)
    client = _Client(recall_exc=exc, recent_exc=exc)
    mems = lifecycle._fetch_recent_memories(_State(client), _msgs("hi there"), "turn")
    assert mems == []
    assert lifecycle._dedup_context_block(mems) == ""


def test_interpreter_shutdown_propagates():
    client = _Client(recall_exc=InterpreterShutdownError("shutting down"))
    with pytest.raises(InterpreterShutdownError):
        lifecycle._fetch_recent_memories(_State(client), _msgs("hi there"), "turn")


def test_client_without_recent_fetcher_still_gets_topical():
    class Old:
        async def recall(self, query, **kw):
            return [RerankerResult(id="t1", text="Topical", embedding=[1.0])]

    mems = lifecycle._fetch_recent_memories(_State(Old()), _msgs("hi there"), "turn")
    assert [m["id"] for m in mems] == ["t1"]


def test_legacy_call_without_messages_still_works():
    client = _Client(recent=[{"id": "r1", "text": "Recent fact", "embedding": None}])
    mems = lifecycle._fetch_recent_memories(_State(client))
    assert [m["id"] for m in mems] == ["r1"]
    assert client.recall_calls == []


def test_auto_extract_passes_prebuilt_block_to_the_llm():
    from totalreclaw.agent.lifecycle import auto_extract
    from totalreclaw.hermes.state import PluginState
    import os
    from pathlib import Path

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = _Client(topical=[RerankerResult(id="p1", text="Pinned fact", embedding=[1.0], pinned=True)])
    client.remember_batch = mock.AsyncMock(return_value=[])
    state._client = client
    seen = {}

    async def fake_extract(messages, mode="turn", existing_memories=None, llm_config=None, *, dedup_context=None):
        seen["dedup_context"] = dedup_context
        return []

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.extract_facts_heuristic", return_value=[]):
        state.add_message("user", "tell me about my pinned fact")
        state.add_message("assistant", "sure")
        auto_extract(state)
    assert "[ID: p1] [pinned, read-only] Pinned fact" in seen["dedup_context"]


def test_prompt_suffix_prebuilt_block_wins_and_legacy_path_is_unchanged():
    mems = [{"id": "a", "text": "Fact A"}, {"id": "b", "text": "Fact B"}]
    assert _memories_prompt_suffix(mems, "") == ""
    assert _memories_prompt_suffix(mems, "BLOCK") == "\n\nBLOCK"
    assert _memories_prompt_suffix(mems, None) == (
        "\n\n" + DEDUP_CONTEXT_HEADER + "\n[ID: a] Fact A\n[ID: b] Fact B"
    )
    assert _memories_prompt_suffix([], None) == ""


def test_update_without_existing_id_becomes_add():
    f = _build_fact({"text": "User now lives in Porto", "importance": 8, "action": "UPDATE"})
    assert f.action == "ADD"
    g = _build_fact({"text": "User now lives in Porto", "importance": 8, "action": "UPDATE",
                     "existingFactId": "f1"})
    assert g.action == "UPDATE" and g.existing_fact_id == "f1"


def test_in_batch_cosine_pass_ignores_pinned_memories():
    fact = ExtractedFact(text="User moved the 1:1 to Thursdays", type="claim", importance=8, action="ADD")
    fact._embedding = [1.0, 0.0]
    pinned = [{"id": "p", "text": "1:1 on Tuesdays", "embedding": [0.99, 0.05], "pinned": True}]
    assert deduplicate_facts_by_embedding([fact], pinned) == [fact]
    unpinned = [dict(pinned[0], pinned=False)]
    assert deduplicate_facts_by_embedding([fact], unpinned) == []


# -- operations.fetch_recent_facts ------------------------------------------------

KEY = bytes(range(32))


def _row(fid, text, pinned=False, blob=None):
    obj = blob or {"id": fid, "text": text, "type": "claim", "source": "user",
                   "created_at": "2026-11-01T00:00:00Z", "schema_version": "1.0", "importance": 7}
    if pinned:
        obj["pin_status"] = "pinned"
    hexblob = "0x" + base64.b64decode(encrypt(json.dumps(obj), KEY)).hex()
    return {"id": fid, "encryptedBlob": hexblob, "encryptedEmbedding": None, "isActive": True}


class _Relay:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def query_subgraph(self, gql, variables):
        self.calls.append(variables)
        return {"data": {"facts": self.rows}}


def _keys():
    k = mock.Mock()
    k.encryption_key = KEY
    return k


async def test_fetch_recent_facts_decodes_skips_and_flags_pins():
    rows = [
        _row("f1", "Newest fact", pinned=True),
        {"id": "stub", "encryptedBlob": "0x00", "isActive": True},
        {"id": "bad", "encryptedBlob": "0xdeadbeef", "isActive": True},
        _row("f2", "Older fact"),
        _row("f3", "Oldest fact"),
    ]
    relay = _Relay(rows)
    out = await operations.fetch_recent_facts(_keys(), "0xABC", relay, limit=2)
    assert [(r["id"], r["text"], r["pinned"]) for r in out] == [
        ("f1", "Newest fact", True), ("f2", "Older fact", False),
    ]
    assert relay.calls == [{"owner": "0xabc", "first": 2 + operations.RECENT_FETCH_HEADROOM}]


async def test_fetch_recent_facts_zero_limit_makes_no_read():
    relay = _Relay([])
    assert await operations.fetch_recent_facts(_keys(), "0xabc", relay, limit=0) == []
    assert relay.calls == []
