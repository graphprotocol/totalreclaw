"""ChatGPT import — session fidelity (plan 2026-07-05, internal repo).

The ChatGPT export has first-class conversation boundaries (title,
timestamps, mapping tree with current_node). These tests pin the behavior
that preserves them end-to-end:

  G1  chunks carry conversation_id; the engine groups sessions by it and
      skips centroid-walk segmentation when explicit boundaries exist
  G2  mapping traversal follows the canonical current_node -> parent chain
      (edited/regenerated branches are NOT imported twice)
  G3  per-message create_time is preserved on chunk messages
  G4  content_type "code" (content.text) and voice audio transcriptions
      (multimodal dict parts) are extracted, image-only parts skipped
  G5  a real export is a zip / directory with conversations-NNN.json splits;
      zip, directory, and single-file inputs parse identically

Fixture: tests/fixtures/chatgpt-conversations.fixture.json — 7 real
(anonymized) conversations + 1 synthetic, selected by
totalreclaw-internal/e2e/chatgpt-import/make-fixture.py. Expected counts
below were derived from the fixture by that script's companion analysis;
see the manifest JSON for per-case metadata.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from totalreclaw.import_adapters.chatgpt_adapter import ChatGPTAdapter
from totalreclaw.import_engine import ImportEngine

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURE_DIR / "chatgpt-conversations.fixture.json"
MANIFEST = FIXTURE_DIR / "chatgpt-fixture-manifest.json"

# Derived from the fixture (see module docstring):
#   branched: 5 user/assistant text messages across all branches,
#             4 on the canonical current_node path
#   long:     69 across all branches, 61 canonical
CASE_IDS = {case: m["conversation_id"] for case, m in json.loads(MANIFEST.read_text()).items()}
CANONICAL_COUNTS = {"plain": 4, "singleton": 2, "branched": 4, "long": 61}
BFS_COUNTS = {"branched": 5, "long": 69}
CODE_STRING_PART_COUNT = 13  # string-part msgs in the code conv; content.text adds more


def _conversations():
    return json.loads(FIXTURE.read_text())


def _conv(case):
    cid = CASE_IDS[case]
    return next(
        c for c in _conversations()
        if (c.get("conversation_id") or c.get("id")) == cid
    )


def _parse(convs):
    adapter = ChatGPTAdapter()
    return adapter.parse(content=json.dumps(convs))


# ── G1: conversation identity on chunks ──────────────────────────────────


def test_every_chunk_carries_conversation_id():
    result = _parse(_conversations())
    assert result.chunks, result.errors
    for chunk in result.chunks:
        assert getattr(chunk, "conversation_id", None), (
            f"chunk {chunk.title!r} has no conversation_id"
        )


def test_multi_chunk_conversation_shares_one_id():
    result = _parse([_conv("long")])
    assert len(result.chunks) > 1  # 61 msgs / CHUNK_SIZE 20 -> 4 chunks
    ids = {chunk.conversation_id for chunk in result.chunks}
    assert ids == {CASE_IDS["long"]}


def test_conversation_ids_match_export():
    result = _parse([_conv("plain"), _conv("singleton")])
    got = {chunk.conversation_id for chunk in result.chunks}
    assert got == {CASE_IDS["plain"], CASE_IDS["singleton"]}


# ── G2: canonical branch traversal ───────────────────────────────────────


def test_branched_conversation_imports_canonical_path_only():
    result = _parse([_conv("branched")])
    assert result.total_messages == CANONICAL_COUNTS["branched"], (
        f"expected canonical path ({CANONICAL_COUNTS['branched']} msgs), got "
        f"{result.total_messages} — regenerated/edited branches must not be "
        f"imported (BFS over all branches yields {BFS_COUNTS['branched']})"
    )


def test_long_conversation_imports_canonical_path_only():
    result = _parse([_conv("long")])
    assert result.total_messages == CANONICAL_COUNTS["long"], (
        f"expected {CANONICAL_COUNTS['long']} canonical msgs, got "
        f"{result.total_messages} (all-branches BFS would give {BFS_COUNTS['long']})"
    )


def test_plain_conversation_message_count():
    result = _parse([_conv("plain")])
    assert result.total_messages == CANONICAL_COUNTS["plain"]


# ── G3: per-message timestamps ───────────────────────────────────────────


def test_messages_carry_individual_timestamps():
    result = _parse([_conv("plain")])
    msgs = [m for chunk in result.chunks for m in chunk.messages]
    stamped = [m for m in msgs if m.get("timestamp")]
    assert len(stamped) == len(msgs), "every message should carry its create_time"
    # ISO-8601 and non-decreasing along the conversation
    times = [m["timestamp"] for m in msgs]
    assert times == sorted(times)


def test_chunk_timestamp_is_first_message_time_not_conversation_time():
    result = _parse([_conv("long")])
    chunk_times = [c.timestamp for c in result.chunks if c.timestamp]
    assert len(set(chunk_times)) > 1, (
        "chunks of a long conversation must not all share the conversation-level "
        "create_time — per-message times are in the export"
    )


# ── G4: content-type coverage ────────────────────────────────────────────


def test_code_messages_extracted_via_content_text():
    result = _parse([_conv("code")])
    assert result.total_messages > CODE_STRING_PART_COUNT, (
        "content_type=code messages store text in content.text, not parts[] — "
        "they must be extracted"
    )


def test_voice_transcriptions_extracted():
    conv = _conv("voice")
    # pull an expected transcription snippet straight from the export
    snippet = None
    for node in conv["mapping"].values():
        msg = node.get("message")
        if not msg:
            continue
        for part in (msg.get("content") or {}).get("parts") or []:
            if isinstance(part, dict) and part.get("content_type") == "audio_transcription":
                text = (part.get("text") or "").strip()
                if len(text) > 10:
                    snippet = text[:40]
                    break
        if snippet:
            break
    assert snippet, "fixture must contain an audio_transcription part"

    result = _parse([conv])
    all_text = " ".join(m.get("text", "") for c in result.chunks for m in c.messages)
    assert snippet in all_text, "voice transcription text must be imported"


def test_image_only_conversation_skipped_with_warning():
    result = _parse([_conv("empty")])
    assert result.chunks == []
    assert result.warnings


# ── G5: zip / directory / single-file equivalence ────────────────────────


@pytest.fixture()
def export_layouts(tmp_path):
    """Materialize the fixture as single-file, split-directory, and zip."""
    convs = _conversations()
    single = tmp_path / "conversations.json"
    single.write_text(json.dumps(convs))

    split_dir = tmp_path / "export"
    split_dir.mkdir()
    half = len(convs) // 2
    (split_dir / "conversations-000.json").write_text(json.dumps(convs[:half]))
    (split_dir / "conversations-001.json").write_text(json.dumps(convs[half:]))

    zip_path = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(split_dir / "conversations-000.json", "conversations-000.json")
        zf.write(split_dir / "conversations-001.json", "conversations-001.json")
        zf.writestr("chat.html", "<html></html>")  # present in real exports, ignored

    return single, split_dir, zip_path


def test_zip_directory_and_single_file_parse_identically(export_layouts):
    single, split_dir, zip_path = export_layouts
    adapter = ChatGPTAdapter()

    results = {
        "single": adapter.parse(file_path=str(single)),
        "dir": adapter.parse(file_path=str(split_dir)),
        "zip": adapter.parse(file_path=str(zip_path)),
    }
    for name, res in results.items():
        assert not res.errors, f"{name}: {res.errors}"

    counts = {name: (len(r.chunks), r.total_messages) for name, r in results.items()}
    assert len(set(counts.values())) == 1, counts

    id_sets = {
        name: {c.conversation_id for c in r.chunks} for name, r in results.items()
    }
    assert id_sets["single"] == id_sets["dir"] == id_sets["zip"]


# ── G1 (engine): explicit boundaries beat centroid-walk ──────────────────


class _BatchClient:
    """Records remember_batch payloads; pretends to be on Gnosis (chain 100)."""

    def __init__(self):
        self.batches = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100

    async def remember_batch(self, payloads, source=None):
        self.batches.append({"payloads": list(payloads), "source": source})
        ids = [f"f{self._n + i}" for i in range(len(payloads))]
        self._n += len(payloads)
        return ids


def _payloads(client):
    return [p for b in client.batches for p in b["payloads"]]


def test_engine_groups_sessions_by_conversation_id(monkeypatch):
    """Chunks with explicit conversation ids must NOT go through
    embedding-based centroid-walk segmentation."""
    import totalreclaw.session_segmentation as seg

    def _boom(*a, **k):  # pragma: no cover - the point is it is never called
        raise AssertionError(
            "segment_sessions must not run for sources with explicit "
            "conversation boundaries"
        )

    monkeypatch.setattr(seg, "segment_sessions", _boom)

    parse = _parse([_conv("plain"), _conv("long"), _conv("singleton")])
    engine = ImportEngine(client=_BatchClient(), llm_extract=None)
    sessions = asyncio.run(engine._get_session_assignments(parse.chunks))

    # one session per conversation, each containing all of that conversation's chunks
    by_conv = {}
    for idx, chunk in enumerate(parse.chunks):
        by_conv.setdefault(chunk.conversation_id, []).append(idx)
    assert sorted(map(sorted, sessions)) == sorted(map(sorted, by_conv.values()))


def test_end_to_end_sessions_equal_conversations(monkeypatch):
    """Full process_batch over two conversations -> two sessions, facts of a
    conversation share one session_id, one Crystal per multi-turn conversation."""
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: [0.1, 0.2, 0.3])

    async def fake_extract(messages, timestamp):
        # engine contract: messages arrive as [{"role", "content"}]
        return [{"text": f"fact about {messages[0]['content'][:20]}", "type": "fact", "importance": 8}]

    async def fake_completion(prompt):
        return (
            '{"title": "A conversation", "summary": "s", '
            '"key_outcomes": [], "open_threads": [], "topics_discussed": []}'
        )

    client = _BatchClient()
    engine = ImportEngine(
        client=client,
        llm_extract=fake_extract,
        llm_completion=fake_completion,
        enable_smart_import=False,
    )
    result = asyncio.run(
        engine.process_batch(
            source="chatgpt",
            content=json.dumps([_conv("plain"), _conv("branched")]),
        )
    )
    assert result.is_complete, result.errors

    payloads = _payloads(client)
    assert payloads
    session_ids = {
        (p.get("extra_metadata") or {}).get("session_id")
        for p in payloads
        if (p.get("extra_metadata") or {}).get("session_id")
    }
    assert len(session_ids) == 2, (
        f"two conversations must map to exactly two sessions, got {len(session_ids)}"
    )
    crystals = [
        p for p in payloads
        if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"
    ]
    assert len(crystals) == 2
