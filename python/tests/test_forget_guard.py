"""Forget guard: matcher parity + encrypted ledger + client wiring (internal#605, #640).

The fixture ``tests/parity/fixtures/forget-guard-v1.json`` is shared with the
Rust core (``forget_guard.rs::tests::parity_fixture_vectors``) and the WASM
test (``tests/parity/forget-guard-parity.test.ts``). The Python port runs it
always; the core binding runs it when the installed ``totalreclaw_core``
exports ``match_forgotten`` (core >= 2.7.0, or a wheel built from this tree).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import totalreclaw_core

from totalreclaw import forget_guard as fg
from totalreclaw.crypto import encrypt, encrypt_embedding

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "parity" / "fixtures" / "forget-guard-v1.json").read_text(
        encoding="utf-8"
    )
)
HAS_CORE = hasattr(totalreclaw_core, "match_forgotten")
HAS_CORE_RELEASE = hasattr(totalreclaw_core, "forget_release_ids")
KEY = bytes(range(32))


# ---------------------------------------------------------------------------
# Parity: Python port (always) and core binding (when present)
# ---------------------------------------------------------------------------


def test_thresholds_match_fixture():
    assert FIXTURE["subset_min_cosine"] == fg.SUBSET_MIN_COSINE
    assert FIXTURE["superset_min_cosine"] == fg.SUPERSET_MIN_COSINE
    assert FIXTURE["anchor_min_cosine"] == fg.ANCHOR_MIN_COSINE
    assert FIXTURE["clause_min_cosine"] == fg.CLAUSE_MIN_COSINE
    assert FIXTURE["release_min_cosine"] == fg.RELEASE_MIN_COSINE
    assert FIXTURE["paraphrase_min_cosine"] == fg.PARAPHRASE_MIN_COSINE


def test_stopwords_match_core_source():
    src = (Path(__file__).resolve().parents[2] / "rust" / "totalreclaw-core" / "src" / "forget_guard.rs").read_text(
        encoding="utf-8"
    )
    block = src.split("pub const STOPWORDS: &[&str] = &[", 1)[1].split("];", 1)[0]
    rust_words = [w.strip().strip('"') for w in block.replace("\n", " ").split(",") if w.strip()]
    assert rust_words == sorted(rust_words)
    assert set(rust_words) == fg.STOPWORDS


def test_relation_words_match_core_source():
    src = (Path(__file__).resolve().parents[2] / "rust" / "totalreclaw-core" / "src" / "forget_guard.rs").read_text(
        encoding="utf-8"
    )
    block = src.split("pub const RELATION_WORDS: &[&str] = &[", 1)[1].split("];", 1)[0]
    rust_words = [w.strip().strip('"') for w in block.replace("\n", " ").split(",") if w.strip()]
    assert rust_words == sorted(rust_words)
    assert set(rust_words) == fg.RELATION_WORDS


@pytest.mark.parametrize("case", FIXTURE["match_cases"], ids=[c["name"] for c in FIXTURE["match_cases"]])
def test_python_port_match_cases(case):
    got = fg._py_match_forgotten(case["text"], case["embedding"], case["entries"])
    want = case["expected"]
    if want is None:
        assert got is None
    else:
        assert got is not None and got["id"] == want["id"] and got["reason"] == want["reason"]


@pytest.mark.skipif(not HAS_CORE, reason="installed totalreclaw_core has no match_forgotten")
@pytest.mark.parametrize("case", FIXTURE["match_cases"], ids=[c["name"] for c in FIXTURE["match_cases"]])
def test_core_binding_match_cases(case):
    out = json.loads(
        totalreclaw_core.match_forgotten(case["text"], json.dumps(case["embedding"]), json.dumps(case["entries"]))
    )
    want = case["expected"]
    if want is None:
        assert out is None
    else:
        assert out["id"] == want["id"] and out["reason"] == want["reason"]


RELEASE_CASES = FIXTURE["release_cases"]


@pytest.mark.parametrize("case", RELEASE_CASES, ids=[c["name"] for c in RELEASE_CASES])
def test_python_port_release_cases(case):
    assert fg._py_forget_release_ids(case["text"], case["embedding"], case["entries"]) == case["expected"]


@pytest.mark.skipif(not HAS_CORE_RELEASE, reason="installed totalreclaw_core has no forget_release_ids")
@pytest.mark.parametrize("case", RELEASE_CASES, ids=[c["name"] for c in RELEASE_CASES])
def test_core_binding_release_cases(case):
    out = json.loads(
        totalreclaw_core.forget_release_ids(case["text"], json.dumps(case["embedding"]), json.dumps(case["entries"]))
    )
    assert out == case["expected"]
    assert fg.forget_release_ids(case["text"], case["embedding"], case["entries"]) == case["expected"]


@pytest.mark.parametrize("case", RELEASE_CASES, ids=[c["name"] for c in RELEASE_CASES])
def test_public_release_uses_python_port_without_core(monkeypatch, case):
    monkeypatch.setattr(fg, "_core", lambda: None)
    assert fg.forget_release_ids(case["text"], case["embedding"], case["entries"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["request_cases"], ids=[c["text"][:40] or "empty" for c in FIXTURE["request_cases"]])
def test_is_forget_request_cases(case):
    folded = fg._fold(case["text"])
    py = bool(case["text"].strip()) and any(r.search(folded) for r in fg._FORGET_REQUEST_RES)
    assert py is case["expected"]
    assert fg.is_forget_request(case["text"]) is case["expected"]


@pytest.mark.parametrize("case", FIXTURE["token_cases"], ids=[c["text"][:30] or "empty" for c in FIXTURE["token_cases"]])
def test_token_cases(case):
    assert fg.guard_tokens(case["text"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["anchor_cases"], ids=[c["text"][:30] for c in FIXTURE["anchor_cases"]])
def test_anchor_cases(case):
    assert fg.guard_anchors(case["text"]) == case["expected"]


def test_public_match_uses_python_port_without_core(monkeypatch):
    monkeypatch.setattr(fg, "_core", lambda: None)
    case = next(c for c in FIXTURE["match_cases"] if c["name"] == "anchor_reworded_restatement_605")
    assert fg.match_forgotten(case["text"], case["embedding"], case["entries"])["reason"] == "anchor"
    assert fg.is_forget_request("User asked to forget his address") is True


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_ledger_round_trip_is_encrypted(tmp_path):
    led = fg.ForgetLedger(tmp_path / "forgotten-x.enc", KEY)
    led.record("f1", "User is vegetarian", [1.0, 0.0])
    raw = (tmp_path / "forgotten-x.enc").read_text()
    assert "vegetarian" not in raw  # ciphertext only
    assert oct(os.stat(tmp_path / "forgotten-x.enc").st_mode & 0o777) == "0o600"
    again = fg.ForgetLedger(tmp_path / "forgotten-x.enc", KEY)
    assert [e["id"] for e in again.entries()] == ["f1"]
    assert again.match("user is vegetarian")["reason"] == "exact"
    assert again.match("User likes jazz", [0.0, 1.0]) is None


def test_ledger_wrong_key_reads_empty(tmp_path):
    fg.ForgetLedger(tmp_path / "l.enc", KEY).record("f1", "User is vegetarian")
    assert fg.ForgetLedger(tmp_path / "l.enc", bytes(32)).entries() == []


def test_ledger_release_and_dedupe(tmp_path):
    led = fg.ForgetLedger(tmp_path / "l.enc", KEY)
    led.record("f1", "User is vegetarian", [1.0, 0.0])
    led.record("f1", "User is vegetarian", [1.0, 0.0])  # same id → one entry
    led.record("f2", "User lives in Lisbon", [0.0, 1.0])
    assert len(led) == 2
    assert led.release("User is vegetarian", [1.0, 0.0]) == 1
    assert [e["id"] for e in led.entries()] == ["f2"]


H13_REMEMBER = (
    "User is vegetarian again (does not eat meat), resuming as of November 18, 2026 — "
    "they had lapsed from vegetarianism at some point before this and have now returned to it."
)
VEG_ID = "45649345-c4d0-42e0-94bb-74027213bbfa"
GLM_ID = "a3c1f7ca-0000-4000-8000-000000000000"


def _at(c):
    import math

    return [c, math.sqrt(1 - c * c)]


def _h13_ledger(tmp_path):
    led = fg.ForgetLedger(tmp_path / "l.enc", KEY)
    led.record(VEG_ID, "User is vegetarian.", [1.0, 0.0])
    led.record(GLM_ID, "User decided to use glm-4.5-flash for memory extraction from now on.", [0.0, 1.0])
    return led


def test_ledger_release_padded_remember_h13(tmp_path):
    led = _h13_ledger(tmp_path)
    assert led.release_counts(H13_REMEMBER, _at(0.69)) == (1, 2)
    assert [e["id"] for e in led.entries()] == [GLM_ID]
    # With the entry gone the short extractor form is no longer suppressed.
    assert led.match("User is vegetarian again.", _at(0.9)) is None


def test_ledger_release_keeps_unrelated_entry(tmp_path):
    led = _h13_ledger(tmp_path)
    # Restating the glm entry releases it; the vegetarian entry shares no
    # content word and stays.
    got = led.release_counts("User decided to use glm-5.3-flash for memory extraction from now on.", _at(0.1))
    assert got == (1, 2)
    assert [e["id"] for e in led.entries()] == [VEG_ID]


def test_ledger_release_counts_empty_ledger(tmp_path):
    assert fg.ForgetLedger(tmp_path / "none.enc", KEY).release_counts("User is vegetarian") == (0, 0)


def test_ledger_caps_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fg.ForgetLedger, "MAX_ENTRIES", 3)
    led = fg.ForgetLedger(tmp_path / "l.enc", KEY)
    for i in range(5):
        led.record(f"f{i}", f"fact {i}")
    assert [e["id"] for e in led.entries()] == ["f2", "f3", "f4"]


def test_ledger_ignores_empty_text(tmp_path):
    led = fg.ForgetLedger(tmp_path / "l.enc", KEY)
    led.record("f1", "  ")
    assert len(led) == 0 and not (tmp_path / "l.enc").exists()


def test_for_owner_uses_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    led = fg.ForgetLedger.for_owner("0xAbC123", KEY)
    assert led.path == tmp_path / "forgotten-0xabc123.enc"


# ---------------------------------------------------------------------------
# fetch_fact_plaintext + client.forget wiring
# ---------------------------------------------------------------------------

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
SA = "0x2c0cf74b2b76110708ca431796367779e3738250"


def _subgraph_fact(fact_id, text, keys, owner=SA, embedding=None):
    blob = json.dumps({"text": text, "type": "claim", "source": "user", "schema_version": "1.0"})
    enc_hex = base64.b64decode(encrypt(blob, keys.encryption_key)).hex()
    fact = {"id": fact_id, "owner": owner, "encryptedBlob": "0x" + enc_hex, "isActive": True}
    if embedding is not None:
        fact["encryptedEmbedding"] = encrypt_embedding(embedding, keys.encryption_key)
    return fact


def _client():
    from totalreclaw.client import TotalReclaw

    c = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=SA)
    c._registered = True
    c._chain_id_resolved = True
    c._chain_id = 100
    return c


@pytest.mark.asyncio
async def test_fetch_fact_plaintext_decrypts(tmp_path):
    from totalreclaw.operations import fetch_fact_plaintext

    c = _client()
    emb = [0.0] * 640
    emb[0] = 1.0
    fact = _subgraph_fact("f1", "User is vegetarian", c.keys, embedding=emb)
    c._relay.query_subgraph = AsyncMock(return_value={"data": {"fact": fact}})
    got = await fetch_fact_plaintext("f1", SA, c._relay, c.keys)
    assert got["text"] == "User is vegetarian"
    assert got["embedding"][0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_fetch_fact_plaintext_rejects_other_owner():
    from totalreclaw.operations import fetch_fact_plaintext

    c = _client()
    fact = _subgraph_fact("f1", "User is vegetarian", c.keys, owner="0x" + "11" * 20)
    c._relay.query_subgraph = AsyncMock(return_value={"data": {"fact": fact}})
    assert await fetch_fact_plaintext("f1", SA, c._relay, c.keys) is None


@pytest.mark.asyncio
async def test_client_forget_records_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    c = _client()
    fact = _subgraph_fact("f1", "User is vegetarian", c.keys)
    c._relay.query_subgraph = AsyncMock(return_value={"data": {"fact": fact}})
    with patch("totalreclaw.client.forget_fact", new=AsyncMock(return_value=True)):
        assert (await c.forget("f1"))["success"] is True  # internal tombstone
        assert len(c.forget_ledger) == 0
        c._relay.query_subgraph.assert_not_awaited()
        assert (await c.forget("f1", suppress_reextraction=True))["success"] is True
    assert [e["id"] for e in c.forget_ledger.entries()] == ["f1"]


@pytest.mark.asyncio
async def test_client_forget_failure_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    c = _client()
    fact = _subgraph_fact("f1", "User is vegetarian", c.keys)
    c._relay.query_subgraph = AsyncMock(return_value={"data": {"fact": fact}})
    with patch("totalreclaw.client.forget_fact", new=AsyncMock(return_value=False)):
        assert (await c.forget("f1", suppress_reextraction=True))["success"] is False
    assert len(c.forget_ledger) == 0
