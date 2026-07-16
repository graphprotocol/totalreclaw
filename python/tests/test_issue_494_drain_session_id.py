"""Regression tests for issue #494.

Split-out from #470 (Finding #7, umbrella #466). The mutation half —
``retype``/``set_scope``/``pin``/``unpin`` dropping ``metadata.session_id``
— was fixed in p-diogo/totalreclaw#526. This file covers the **drain half**.

The bug
-------
When ``hermes chat -q`` hits an interpreter-shutdown race, the unprocessed
buffer is persisted to ``~/.totalreclaw/.pending_extract.jsonl`` and
re-extracted at the next ``on_session_start`` (``hooks._drain_into_state``
→ ``lifecycle.auto_extract``). The server stamps ``metadata.session_id``
from the relay ``X-TotalReclaw-Session`` header (``relay._session_id``),
which at drain time carries the *draining* session, not the original. The
old queue record only persisted ``{owner, queued_at, messages}`` — the
source session id was never carried — so drained facts got the wrong
session_id (or null).

The fix
-------
* ``pending_drain.enqueue_messages`` persists the source ``session_id``.
* ``pending_drain.drain_pending(..., with_meta=True)`` returns it per batch
  (the default no-meta shape is unchanged for existing callers).
* ``lifecycle.auto_extract`` gains ``session_id_override`` — it temporarily
  points ``relay._session_id`` at the original session for the duration of
  the extraction and restores it afterwards. Its shutdown handler persists
  the (possibly-overridden) ``relay._session_id`` so a re-deferred drain
  keeps the original.
* ``hooks._drain_into_state`` extracts each batch separately, passing that
  batch's original session id as the override.

These tests pin: schema round-trip (persist + read), the write-side
capture at shutdown, the lifecycle override + restore, and the hooks
per-batch override.
"""
from __future__ import annotations

import json
from typing import Optional

import pytest

from totalreclaw.agent.loop_runner import InterpreterShutdownError
from totalreclaw.agent import pending_drain
from totalreclaw.agent.pending_drain import (
    drain_pending,
    enqueue_messages,
)


@pytest.fixture
def pending_path(tmp_path, monkeypatch):
    p = tmp_path / ".pending_extract.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: p)
    return p


# ---------------------------------------------------------------------------
# Fakes — a relay object carrying ``_session_id`` (the header the server
# stamps onto ``metadata.session_id``) is the piece the issue-148/169 fakes
# lacked.
# ---------------------------------------------------------------------------


class _FakeRelay:
    def __init__(self, session_id: Optional[str] = None):
        self._session_id = session_id


class _FakeClient:
    def __init__(self, *, eoa: str, sa: Optional[str] = None,
                 session_id: Optional[str] = None, raise_on: str = "never"):
        self._eoa_address = eoa
        self._sa_address = sa
        self._relay = _FakeRelay(session_id)
        self._raise_on = raise_on

    @property
    def eoa_address(self) -> str:
        return self._eoa_address

    @property
    def resolved_wallet_address(self) -> Optional[str]:
        return self._sa_address

    async def recall(self, *_a, **_kw):
        if self._raise_on == "recall":
            raise InterpreterShutdownError("simulated")
        return []

    async def remember_batch(self, *_a, **_kw):
        if self._raise_on == "remember_batch":
            raise InterpreterShutdownError("simulated")
        return []

    async def forget(self, *_a, **_kw):
        return None


class _FakeState:
    def __init__(self, client: _FakeClient, messages: list[dict]):
        self._client = client
        self._messages = list(messages)
        self._last_processed_idx = 0
        self.quota_warning: Optional[str] = None

    def is_configured(self) -> bool:
        return True

    def get_client(self):
        return self._client

    def get_unprocessed_messages(self):
        return self._messages[self._last_processed_idx:]

    def has_unprocessed_messages(self):
        return self._last_processed_idx < len(self._messages)

    def mark_messages_processed(self):
        self._last_processed_idx = len(self._messages)

    def get_max_facts_per_extraction(self):
        return 5

    def set_quota_warning(self, w: str):
        self.quota_warning = w

    def add_message(self, role: str, content: str):
        self._messages.append({"role": role, "content": content})

    def reset_turn_counter(self):
        pass

    def get_cached_billing(self):
        return None

    def start_session(self, external_id=None) -> str:
        return "fake-session-id"


# ---------------------------------------------------------------------------
# 1. Queue schema: enqueue persists session_id, drain(with_meta) returns it.
# ---------------------------------------------------------------------------


def test_enqueue_persists_session_id_and_drain_with_meta_returns_it(pending_path):
    owner = "0xabc0000000000000000000000000000000000001"
    msgs = [{"role": "user", "content": "Hi I'm Pedro from Porto."}]

    assert enqueue_messages(owner, msgs, session_id="sess-original-123") is True

    batches = drain_pending(owner, with_meta=True)
    assert len(batches) == 1
    assert batches[0]["messages"] == msgs
    assert batches[0]["session_id"] == "sess-original-123"


def test_drain_with_meta_legacy_record_yields_none_session(pending_path):
    """A record written before #494 has no ``session_id`` field — the reader
    must degrade to ``None`` (no override), not KeyError."""
    p = pending_drain._pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "owner": "0xabc0000000000000000000000000000000000002",
        "queued_at": "2026-07-15T00:00:00Z",
        "messages": [{"role": "user", "content": "legacy"}],
    }
    p.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    batches = drain_pending(legacy["owner"], with_meta=True)
    assert len(batches) == 1
    assert batches[0]["messages"] == legacy["messages"]
    assert batches[0]["session_id"] is None


def test_drain_without_meta_preserves_message_list_shape(pending_path):
    """The default (no ``with_meta``) return contract is unchanged so the
    existing #148 / #169 callers and tests keep working."""
    owner = "0xabc0000000000000000000000000000000000003"
    msgs = [{"role": "user", "content": "hi"}]
    enqueue_messages(owner, msgs, session_id="s1")

    drained = drain_pending(owner)
    assert drained == [msgs]


# ---------------------------------------------------------------------------
# 2. Write side: the shutdown handler persists the source session id.
# ---------------------------------------------------------------------------


def test_shutdown_enqueue_persists_source_session_id(pending_path):
    from totalreclaw.agent.lifecycle import auto_extract

    eoa = "0xdeadbeef00000000000000000000000000000001"
    state = _FakeState(
        _FakeClient(eoa=eoa, session_id="live-session-xyz", raise_on="recall"),
        [{"role": "user", "content": "Hi I'm Pedro."}],
    )

    assert auto_extract(state, mode="turn") == []

    batches = drain_pending(eoa, with_meta=True)
    assert len(batches) == 1
    # Pre-fix this was absent/empty; the drained facts would then get the
    # NEXT session's id.
    assert batches[0]["session_id"] == "live-session-xyz"


# ---------------------------------------------------------------------------
# 3. Lifecycle override: relay session tag is swapped during extraction and
#    restored afterwards.
# ---------------------------------------------------------------------------


def test_auto_extract_overrides_relay_session_then_restores(pending_path, monkeypatch):
    from totalreclaw.agent import lifecycle

    seen = {}

    def fake_inner(state, mode, llm_config, messages, max_facts, client, stored_texts):
        seen["session_at_extract"] = client._relay._session_id
        state.mark_messages_processed()
        return []

    monkeypatch.setattr(lifecycle, "_auto_extract_inner", fake_inner)

    client = _FakeClient(eoa="0xabc0000000000000000000000000000000000004",
                         session_id="draining-session")
    state = _FakeState(client, [{"role": "user", "content": "hello world"}])

    lifecycle.auto_extract(state, mode="full", session_id_override="original-session")

    # During extraction the relay carried the ORIGINAL session.
    assert seen["session_at_extract"] == "original-session"
    # After extraction the draining session's tag is restored.
    assert client._relay._session_id == "draining-session"


def test_auto_extract_without_override_leaves_relay_session_untouched(pending_path, monkeypatch):
    from totalreclaw.agent import lifecycle

    seen = {}

    def fake_inner(state, mode, llm_config, messages, max_facts, client, stored_texts):
        seen["session_at_extract"] = client._relay._session_id
        state.mark_messages_processed()
        return []

    monkeypatch.setattr(lifecycle, "_auto_extract_inner", fake_inner)

    client = _FakeClient(eoa="0xabc0000000000000000000000000000000000005",
                         session_id="draining-session")
    state = _FakeState(client, [{"role": "user", "content": "hi"}])

    lifecycle.auto_extract(state, mode="full")  # no override

    assert seen["session_at_extract"] == "draining-session"
    assert client._relay._session_id == "draining-session"


# ---------------------------------------------------------------------------
# 4. Hooks: each drained batch is extracted with its OWN original session id.
# ---------------------------------------------------------------------------


def test_drain_into_state_applies_per_batch_session_override(pending_path, monkeypatch):
    from totalreclaw.hermes import hooks

    eoa = "0xdeadbeef00000000000000000000000000000002"
    enqueue_messages(eoa, [{"role": "user", "content": "from-session-A"}],
                     session_id="sess-A")
    enqueue_messages(eoa, [{"role": "user", "content": "from-session-B"}],
                     session_id="sess-B")

    calls: list = []

    def fake_auto_extract(state, mode="turn", llm_config=None, session_id_override=None):
        calls.append({
            "content": state._messages[-1]["content"],
            "override": session_id_override,
        })
        return []

    monkeypatch.setattr(hooks, "_auto_extract", fake_auto_extract)
    monkeypatch.setattr(hooks, "_get_hermes_llm_config", lambda: None)

    state = _FakeState(_FakeClient(eoa=eoa), [])
    hooks.on_session_start(state, session_id="new-draining-session")

    # Both batches drained, each stamped with its own ORIGINAL session id —
    # NOT the new draining session.
    by_content = {c["content"]: c["override"] for c in calls}
    assert by_content.get("from-session-A") == "sess-A"
    assert by_content.get("from-session-B") == "sess-B"
    assert "new-draining-session" not in by_content.values()
