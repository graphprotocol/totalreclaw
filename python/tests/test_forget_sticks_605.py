"""Forget must stick: later auto-extraction may not re-create a forgotten fact.

Reproduces internal#605 / #640 with a real ``TotalReclaw`` client against an
in-memory vault (mocked relay + chain write) and the real Hermes tools:

1. The user forgets a fact through ``totalreclaw_forget``.
2. A later turn restates it: the agent answers from Hermes' own session
   search, and the extractor (mocked with the outputs QA observed) returns
   the restatement, a "user asked to forget X" note, and an unrelated fact.
3. Only the unrelated fact may be stored.

Before the fix all three were stored: the tombstoned fact is not in the
active-only dedup context, so nothing told the write path the user deleted it.
"""
from __future__ import annotations

import base64
import json
import math
from unittest.mock import AsyncMock, patch

import pytest

from totalreclaw.agent.debrief import Crystal
from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.agent.loop_runner import run_sync
from totalreclaw.crypto import encrypt, encrypt_embedding

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
SA = "0x2c0cf74b2b76110708ca431796367779e3738250"
PGB = "Inês proposed using PgBouncer for connection pooling on the Postgres database"
VEG = "User is vegetarian"

_TOPICS = ("pgbouncer", "vegetarian", "postgres", "jazz")


def fake_embedding(text: str) -> list[float]:
    """Deterministic 640-d stand-in for Harrier: one axis per topic word plus
    a shared baseline axis, so texts on the same topic have cosine ~0.9+."""
    v = [0.0] * 640
    low = text.lower()
    for i, t in enumerate(_TOPICS):
        if t in low:
            v[i] = 1.0
    v[639] = 0.3
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class Vault:
    """In-memory on-chain state: fact id -> (text, active)."""

    def __init__(self, client, facts: dict[str, str]):
        self.client = client
        self.facts = {fid: {"text": t, "active": True} for fid, t in facts.items()}
        self.n = 0

    def subgraph_fact(self, fact_id):
        f = self.facts.get(fact_id)
        if not f:
            return None
        keys = self.client.keys
        blob = json.dumps({"text": f["text"], "type": "claim", "source": "user", "schema_version": "1.0"})
        return {
            "id": fact_id,
            "owner": SA,
            "encryptedBlob": "0x" + base64.b64decode(encrypt(blob, keys.encryption_key)).hex(),
            "encryptedEmbedding": encrypt_embedding(fake_embedding(f["text"]), keys.encryption_key),
            "isActive": f["active"],
        }

    async def query_subgraph(self, query, variables):
        return {"data": {"fact": self.subgraph_fact(variables.get("id"))}}

    async def forget_fact(self, fact_id, *a, **kw):  # chain write
        self.facts[fact_id]["active"] = False
        return True

    async def remember_batch(self, facts, source="python-client"):
        ids = []
        for f in facts:
            self.n += 1
            fid = f"new-{self.n}"
            self.facts[fid] = {"text": f["text"], "active": True}
            ids.append(fid)
        return ids

    async def remember(self, text, **kw):
        return (await self.remember_batch([{"text": text}]))[0]

    def active_texts(self):
        return sorted(f["text"] for f in self.facts.values() if f["active"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    from totalreclaw.client import TotalReclaw
    from totalreclaw.hermes.state import PluginState

    client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=SA)
    client._registered = True
    client._chain_id_resolved = True
    client._chain_id = 100
    state = PluginState()
    state._client = client
    vault = Vault(client, {"f1": PGB, "f2": VEG})
    client._relay.query_subgraph = vault.query_subgraph
    client.remember_batch = vault.remember_batch
    client.remember = vault.remember

    async def passthrough(facts, *a, **kw):
        return facts

    patches = [
        patch("totalreclaw.client.forget_fact", new=vault.forget_fact),
        patch("totalreclaw.embedding.get_embedding", new=fake_embedding),
        patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough),
        patch(
            "totalreclaw.agent.lifecycle._fetch_recent_memories",
            new=lambda st, *a, **k: [
                {"id": fid, "text": f["text"], "embedding": fake_embedding(f["text"])}
                for fid, f in vault.facts.items()
                if f["active"]
            ],
        ),
    ]
    for p in patches:
        p.start()
    yield state, client, vault
    for p in patches:
        p.stop()


def _extract_returning(texts):
    facts = [
        ExtractedFact(text=t, type=ty, importance=7, action="ADD", confidence=0.9, source="user", scope="unspecified")
        for t, ty in texts
    ]

    async def fake(*a, **kw):
        return list(facts)

    return patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake)


def _forget(state, fact_id):
    from totalreclaw.hermes import tools

    return json.loads(run_sync(tools.forget({"fact_id": fact_id}, state)))


def test_605_restatement_from_session_search_is_not_restored(env):
    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    assert _forget(state, "f1") == {"deleted": True, "fact_id": "f1"}

    state.add_message("user", "Did Inês propose anything about our database setup?")
    state.add_message("assistant", "Yes. Inês proposed putting PgBouncer in front of Postgres.")
    with _extract_returning([
        ("Inês proposed putting PgBouncer in front of Postgres", "claim"),
        ("Ines proposed PgBouncer but this is now considered outdated", "claim"),
        ("User asked the assistant to forget that Inês proposed PgBouncer", "directive"),
        ("User's team listens to jazz on Fridays", "claim"),
    ]):
        stored = auto_extract(state)

    assert stored == ["User's team listens to jazz on Fridays"]
    assert not any("PgBouncer" in t for t in vault.active_texts())


def test_640_deletion_note_and_negated_restatement_are_not_stored(env):
    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    assert _forget(state, "f2")["deleted"] is True

    state.add_message("user", "Please forget that I'm vegetarian, it's not true.")
    state.add_message("assistant", "Done. I removed the record that you're vegetarian.")
    with _extract_returning([
        ("User previously identified as vegetarian and asked for that record to be removed", "directive"),
        ("User is not vegetarian", "claim"),
    ]):
        assert auto_extract(state) == []
    assert not any("vegetarian" in t for t in vault.active_texts())


def test_unrelated_facts_still_store_after_a_forget(env):
    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    _forget(state, "f2")
    state.add_message("user", "I moved the cluster to Postgres 17.")
    state.add_message("assistant", "Noted.")
    with _extract_returning([("User runs Postgres 17", "claim")]):
        assert auto_extract(state) == ["User runs Postgres 17"]


def test_session_crystal_drops_forgotten_parts(env):
    from totalreclaw.agent import lifecycle

    state, client, vault = env
    _forget(state, "f1")
    for i in range(4):
        state.add_message("user", f"turn {i}")
        state.add_message("assistant", f"reply {i}")
    crystal = Crystal(
        narrative="User reviewed the database setup. Inês proposed putting PgBouncer in front of Postgres.",
        key_outcomes=["User asked to forget that Inês proposed PgBouncer", "Team keeps jazz Fridays"],
        topics_discussed=["database setup"],
    )
    stored = {}

    async def fake_remember(text, **kw):
        stored["text"] = text
        stored["meta"] = kw.get("extra_metadata")
        return "crystal-1"

    client.remember = fake_remember
    with patch("totalreclaw.agent.lifecycle.generate_crystal", new=AsyncMock(return_value=crystal)):
        ids = lifecycle.session_debrief(state, stored_fact_texts=["a", "b"])
    assert ids == ["crystal-1"]
    assert stored["text"] == "User reviewed the database setup."
    assert stored["meta"]["key_outcomes"] == ["Team keeps jazz Fridays"]


def test_explicit_remember_brings_a_forgotten_fact_back(env):
    from totalreclaw.agent.lifecycle import auto_extract
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    assert len(client.forget_ledger) == 1
    out = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    assert out["stored"] is True
    assert len(client.forget_ledger) == 0
    state.add_message("user", "Yes, I'm vegetarian again.")
    state.add_message("assistant", "Got it.")
    # The fake embedder makes every "vegetarian" text identical, so switch off
    # the (unrelated) store-time write gate against the fact just remembered.
    from totalreclaw.write_gate import GateDecision

    with _extract_returning([("User has been vegetarian since 2019", "claim")]), \
         patch("totalreclaw.agent.lifecycle.decide_write",
               return_value=GateDecision(action="store", match=None)):
        assert auto_extract(state) == ["User has been vegetarian since 2019"]


def test_memory_mirror_neither_restores_nor_records(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    # Hermes background review re-adds the line to USER.md → mirrored remember.
    out = json.loads(run_sync(tools.remember({"text": VEG}, state, _from_mirror=True)))
    assert out == {"stored": False, "skipped": "forgotten"}
    assert len(client.forget_ledger) == 1
    # A mirrored forget (USER.md line replaced) is a supersede, not a user forget.
    run_sync(tools.forget({"fact_id": "f1"}, state, _from_mirror=True))
    assert [e["id"] for e in client.forget_ledger.entries()] == ["f2"]


def test_extraction_update_tombstone_does_not_record(env):
    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    state.add_message("user", "Actually Inês now proposes pgcat.")
    state.add_message("assistant", "Noted.")
    upd = ExtractedFact(
        text="Inês proposed pgcat instead", type="claim", importance=7, action="UPDATE",
        confidence=0.9, source="user", scope="unspecified", existing_fact_id="f1",
    )

    async def fake(*a, **kw):
        return [upd]

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake):
        auto_extract(state)
    assert vault.facts["f1"]["active"] is False  # superseded
    assert len(client.forget_ledger) == 0  # but not "forgotten"
