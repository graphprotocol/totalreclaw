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


def _remember_confirmed(state, args, **kw):
    """Explicit remember of a restated forgotten fact: the first call asks for
    confirmation; after a completed user turn the same text plus the token
    stores it. Returns the second call's result."""
    from totalreclaw.hermes import tools
    from totalreclaw.pii import note_turn_completed

    first = json.loads(run_sync(tools.remember(dict(args), state, **kw)))
    assert first["confirmation_required"] == "restates_forgotten"
    note_turn_completed()
    return json.loads(run_sync(tools.remember({**args, "confirm_token": first["confirm_token"]}, state, **kw)))


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
    out = _remember_confirmed(state, {"text": VEG})
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


# ---------------------------------------------------------------------------
# S36 / Hermes QA H13: padded restatements stay forgotten; explicit remember
# releases the entry even when it is padded or the write is skipped.
# ---------------------------------------------------------------------------

H13_STORED = (
    "User is vegetarian (does not eat meat) and always orders the lentil curry "
    "at their usual Indian restaurant near Bolhão."
)
H13_REMEMBER = (
    "User is vegetarian again (does not eat meat), resuming as of November 18, 2026 — "
    "they had lapsed from vegetarianism at some point before this and have now returned to it."
)


def test_h13_padded_restatement_is_skipped_as_clause(env, caplog):
    import logging

    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    _forget(state, "f2")  # ledger holds "User is vegetarian"
    submitted = []

    async def spy_batch(facts, *a, **kw):
        submitted.extend(facts)
        return await vault.remember_batch(facts, *a, **kw)

    client.remember_batch = spy_batch
    state.add_message("user", "Their lentil curry is the only thing I ever order there, no meat for me.")
    state.add_message("assistant", "The whole vegetarian half of the menu is yours then.")
    # The padded text scores 0.71 against the forgotten fact: below the
    # superset floor (0.80), above the clause floor (0.60).
    base = fake_embedding(VEG)
    side = [0.0] * 640
    side[100] = 1.0
    padded = [0.71 * a + math.sqrt(1 - 0.71 ** 2) * b for a, b in zip(base, side)]

    def embed(text):
        return padded if text == H13_STORED else fake_embedding(text)

    with caplog.at_level(logging.INFO), patch("totalreclaw.embedding.get_embedding", new=embed), \
         _extract_returning([(H13_STORED, "claim")]):
        assert auto_extract(state) == []
    assert submitted == []
    assert any("Skipping extracted ADD: restates forgotten fact f2 (clause)" in r.getMessage() for r in caplog.records)
    assert not any("vegetarian" in t.lower() for t in vault.active_texts())


def _at(c, axis):
    """Embedding at cosine ``c`` from the VEG vector, orthogonal remainder on ``axis``."""
    base = fake_embedding(VEG)
    side = [0.0] * 640
    side[axis] = 1.0
    return [c * a + math.sqrt(1 - c * c) * b for a, b in zip(base, side)]


def _embed_from(table):
    return lambda text: table.get(text) or fake_embedding(text)


def test_715_disjoint_paraphrase_of_forgotten_fact_is_skipped(env, caplog):
    import logging

    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    _forget(state, "f2")
    text = "User doesn't eat meat."
    state.add_message("user", "No meat for me, thanks.")
    state.add_message("assistant", "Understood.")
    with caplog.at_level(logging.INFO), \
         patch("totalreclaw.embedding.get_embedding", new=_embed_from({text: _at(0.83, 100)})), \
         _extract_returning([(text, "preference")]):
        assert auto_extract(state) == []
    assert any("Skipping extracted ADD: restates forgotten fact f2 (paraphrase)" in r.getMessage() for r in caplog.records)
    assert vault.active_texts() == [PGB]


def test_715_new_value_and_other_subject_still_store(env):
    from totalreclaw.agent.lifecycle import auto_extract

    state, client, vault = env
    _forget(state, "f2")
    vegan, sister = "User is vegan.", "User's sister is vegetarian."
    state.add_message("user", "I went vegan, and my sister is vegetarian.")
    state.add_message("assistant", "Noted.")
    table = {vegan: _at(0.93, 100), sister: _at(0.87, 101)}
    with patch("totalreclaw.embedding.get_embedding", new=_embed_from(table)), \
         _extract_returning([(vegan, "claim"), (sister, "claim")]):
        stored = auto_extract(state)
    assert sorted(stored) == sorted([vegan, sister])


def test_715_explicit_remember_of_paraphrase_keeps_ledger_entry(env, caplog):
    import logging

    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    text = "User doesn't eat meat."
    with caplog.at_level(logging.INFO), \
         patch("totalreclaw.embedding.get_embedding", new=_embed_from({text: _at(0.83, 100)})):
        out = json.loads(run_sync(tools.remember({"text": text}, state)))
        if out.get("confirmation_required"):
            # A core with the paraphrase rule matches this text, so the user
            # is asked first (internal#715); an older core stores directly.
            from totalreclaw.pii import note_turn_completed

            note_turn_completed()
            out = json.loads(run_sync(tools.remember({"text": text, "confirm_token": out["confirm_token"]}, state)))
    assert out["stored"] is True
    assert _remember_logs(caplog) == ["forget ledger: explicit remember released 0 of 1 entries"]
    assert len(client.forget_ledger) == 1


def _two_entry_ledger(env_tuple):
    state, client, vault = env_tuple
    _forget(state, "f1")
    _forget(state, "f2")
    assert len(client.forget_ledger) == 2
    return state, client, vault


def _remember_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("forget ledger: explicit remember")]


def test_remember_logs_release_counts_even_when_zero(env, caplog):
    import logging

    from totalreclaw.hermes import tools

    state, client, vault = _two_entry_ledger(env)
    with caplog.at_level(logging.INFO):
        out = json.loads(run_sync(tools.remember({"text": "User listens to jazz on Fridays"}, state)))
    assert out["stored"] is True
    assert _remember_logs(caplog) == ["forget ledger: explicit remember released 0 of 2 entries"]
    assert len(client.forget_ledger) == 2


def test_remember_releases_padded_text(env, caplog):
    import logging

    from totalreclaw.hermes import tools

    state, client, vault = _two_entry_ledger(env)
    with caplog.at_level(logging.INFO):
        out = _remember_confirmed(state, {"text": H13_REMEMBER})
    assert out["stored"] is True
    assert _remember_logs(caplog) == ["forget ledger: explicit remember released 1 of 2 entries"]
    assert [e["id"] for e in client.forget_ledger.entries()] == ["f1"]


def test_remember_stopped_by_write_gate_still_releases(env):
    from totalreclaw.hermes import tools
    from totalreclaw.write_gate import GateDecision, GateMatch

    state, client, vault = _two_entry_ledger(env)
    decision = GateDecision(action="skip", match=GateMatch("d5916f82", H13_STORED, "near_duplicate", 0.93, False))
    with patch("totalreclaw.write_gate.check_store", new=AsyncMock(return_value=decision)):
        out = _remember_confirmed(state, {"text": H13_REMEMBER})
    assert out["already_stored"] is True
    assert [e["id"] for e in client.forget_ledger.entries()] == ["f1"]


def test_remember_suppressed_by_f7_still_releases(env):
    from totalreclaw.hermes import tools

    state, client, vault = _two_entry_ledger(env)
    with patch.object(state, "manual_remember_is_dup_of_pending", return_value=True):
        out = _remember_confirmed(state, {"text": H13_REMEMBER})
    assert out["suppressed"] == "duplicate_of_pending_auto_extract"
    assert [e["id"] for e in client.forget_ledger.entries()] == ["f1"]


def test_pii_blocked_remember_does_not_release(env):
    from totalreclaw.hermes import tools

    state, client, vault = _two_entry_ledger(env)
    notice = {"stored": False, "blocked": "sensitive"}
    with patch("totalreclaw.hermes.tools.screen_explicit", return_value=(object(), notice)):
        out = json.loads(run_sync(tools.remember({"text": H13_REMEMBER}, state)))
    assert out == notice
    assert len(client.forget_ledger) == 2


def test_mirror_path_never_releases_or_logs(env, caplog):
    import logging

    from totalreclaw.hermes import tools

    state, client, vault = _two_entry_ledger(env)
    with caplog.at_level(logging.INFO):
        out = json.loads(run_sync(tools.remember({"text": H13_REMEMBER}, state, _from_mirror=True)))
    assert out == {"stored": False, "skipped": "forgotten"}
    assert len(client.forget_ledger) == 2
    assert _remember_logs(caplog) == []


# --- internal#715, D15: an explicit remember of a forgotten fact needs the user's confirmation


def test_715_restating_remember_asks_for_confirmation_and_stores_nothing(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    before = vault.active_texts()
    out = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    assert out["stored"] is False
    assert out["confirmation_required"] == "restates_forgotten"
    assert out["fact_id"] == "f2"
    assert out["confirm_token"] and "next_step" in out
    assert vault.active_texts() == before
    assert len(client.forget_ledger) == 1


def test_715_confirm_token_same_turn_is_refused(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    first = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    again = json.loads(run_sync(tools.remember({"text": VEG, "confirm_token": first["confirm_token"]}, state)))
    assert again["stored"] is False and again["confirmation_required"] == "restates_forgotten"
    assert again["confirm_token"] != first["confirm_token"]
    assert len(client.forget_ledger) == 1


def test_715_wrong_unknown_and_reused_tokens_are_refused(env):
    from totalreclaw.hermes import tools
    from totalreclaw.pii import note_turn_completed

    state, client, vault = env
    _forget(state, "f2")
    first = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    note_turn_completed()
    for token in ("deadbeefdeadbeef", "", None):
        args = {"text": VEG} if token is None else {"text": VEG, "confirm_token": token}
        out = json.loads(run_sync(tools.remember(args, state)))
        assert out["stored"] is False and "confirmation_required" in out
    # a token is bound to the text it was issued for
    other = json.loads(run_sync(tools.remember({"text": "User is vegetarian, no fish", "confirm_token": first["confirm_token"]}, state)))
    assert other["stored"] is False
    # the right token works once, then is spent
    ok = json.loads(run_sync(tools.remember({"text": VEG, "confirm_token": first["confirm_token"]}, state)))
    assert ok["stored"] is True
    assert len(client.forget_ledger) == 0
    _forget(state, "new-1")
    reuse = json.loads(run_sync(tools.remember({"text": VEG, "confirm_token": first["confirm_token"]}, state)))
    assert reuse["stored"] is False and "confirmation_required" in reuse


def test_715_token_expires(env, monkeypatch):
    from totalreclaw import pii
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    first = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    pii.note_turn_completed()
    real = pii.confirmations._clock
    monkeypatch.setattr(pii.confirmations, "_clock", lambda: real() + pii.CONFIRM_TOKEN_TTL_SECONDS + 1)
    out = json.loads(run_sync(tools.remember({"text": VEG, "confirm_token": first["confirm_token"]}, state)))
    assert out["stored"] is False and "confirmation_required" in out


def test_715_token_from_another_confirmation_kind_does_not_work(env):
    from totalreclaw.hermes import tools
    from totalreclaw.pii import confirmations, note_turn_completed

    state, client, vault = env
    _forget(state, "f2")
    wrong = confirmations.issue("api_key", VEG)
    note_turn_completed()
    out = json.loads(run_sync(tools.remember({"text": VEG, "confirm_token": wrong}, state)))
    assert out["stored"] is False and "confirmation_required" in out


def test_715_non_forgotten_remember_is_unchanged(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    out = json.loads(run_sync(tools.remember({"text": "User listens to jazz on Fridays"}, state)))
    assert out["stored"] is True and "confirmation_required" not in out
    assert len(client.forget_ledger) == 1


def test_715_no_forget_ledger_entries_means_no_prompt(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    out = json.loads(run_sync(tools.remember({"text": VEG}, state)))
    assert out["stored"] is True and "confirmation_required" not in out


def test_715_mirror_and_internal_writes_do_not_prompt(env):
    from totalreclaw.hermes import tools

    state, client, vault = env
    _forget(state, "f2")
    mirror = json.loads(run_sync(tools.remember({"text": VEG}, state, _from_mirror=True)))
    assert mirror == {"stored": False, "skipped": "forgotten"}
    internal = json.loads(run_sync(tools.remember({"text": VEG}, state, _skip_confirm=True)))
    assert internal["stored"] is True
