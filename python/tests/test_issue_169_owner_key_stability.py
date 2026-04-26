"""Regression tests for issue #169 — pending-queue owner-key stability.

The drain queue at ``~/.totalreclaw/.pending_extract.jsonl`` keys each
batch by the owner address returned by ``_owner_address(state)`` at write
time. That helper prefers a Smart Account address when ``_sa_address`` /
``smart_account_address`` is set on the client, falling back to the EOA
address otherwise. Across the ``hermes chat -q`` per-turn-process
boundary, the SA-vs-EOA branch can flip between the writing turn (during
interpreter shutdown) and the next turn's ``on_session_start`` drain
(early in a fresh interpreter, before any recall has resolved SA), so
the drain may query the wrong side of the split and silently miss
queued batches. See umbrella #163 finding F6 for the full analysis.

The fix widens the read API: ``has_pending`` and ``drain_pending`` now
accept either a single owner string OR an iterable of owner strings, and
``hooks.on_session_start`` passes both EOA and SA via the new
``_owner_addresses(state)`` helper. This pins:

1. ``has_pending`` / ``drain_pending`` accept an iterable and match on
   any address in the set (case-insensitive).
2. The single-string API still works (backwards compat).
3. The end-to-end drain scenario where the queue was written under EOA
   but ``_sa_address`` is already set when the next session starts —
   without the fix, drain misses; with the fix, it succeeds.
"""
from __future__ import annotations

from typing import Optional

import pytest

from totalreclaw.agent import pending_drain
from totalreclaw.agent.lifecycle import _owner_addresses
from totalreclaw.agent.pending_drain import (
    drain_pending,
    enqueue_messages,
    has_pending,
)


@pytest.fixture
def pending_path(tmp_path, monkeypatch):
    p = tmp_path / ".pending_extract.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: p)
    return p


# ---------------------------------------------------------------------------
# 1. has_pending / drain_pending accept an iterable and match any address
# ---------------------------------------------------------------------------


def test_has_pending_accepts_owner_set(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"

    enqueue_messages(eoa, [{"role": "user", "content": "from-eoa"}])

    # Single-string API: SA only — must miss (legacy behavior).
    assert has_pending(sa) is False
    # Set API: any of {EOA, SA} — must hit.
    assert has_pending([eoa, sa]) is True
    assert has_pending((sa, eoa)) is True


def test_drain_pending_accepts_owner_set_returns_eoa_batch(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"
    msgs = [
        {"role": "user", "content": "Hi I'm Pedro from Porto."},
        {"role": "assistant", "content": "Got it."},
    ]
    enqueue_messages(eoa, msgs)

    drained = drain_pending([eoa, sa])
    assert len(drained) == 1
    assert drained[0] == msgs
    # Queue is consumed.
    assert not pending_path.exists()


def test_drain_pending_accepts_owner_set_returns_sa_batch(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"
    msgs = [{"role": "user", "content": "queued under SA"}]
    enqueue_messages(sa, msgs)

    drained = drain_pending([eoa, sa])
    assert len(drained) == 1
    assert drained[0] == msgs


def test_drain_pending_set_drains_both_sides(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"
    enqueue_messages(eoa, [{"role": "user", "content": "from-eoa"}])
    enqueue_messages(sa, [{"role": "user", "content": "from-sa"}])

    drained = drain_pending([eoa, sa])
    assert len(drained) == 2
    contents = sorted(b[0]["content"] for b in drained)
    assert contents == ["from-eoa", "from-sa"]


def test_drain_pending_set_excludes_other_users(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"
    other = "0x9999999900000000000000000000000000000000"
    enqueue_messages(eoa, [{"role": "user", "content": "us-eoa"}])
    enqueue_messages(other, [{"role": "user", "content": "them"}])

    drained = drain_pending([eoa, sa])
    assert len(drained) == 1
    assert drained[0][0]["content"] == "us-eoa"
    # Other user's batch survives.
    assert has_pending(other) is True


def test_normalize_owners_lowercases_inputs(pending_path):
    """Mixed-case input from callers (e.g. checksummed addresses) must
    match the lower-case keys written by the queue. Defensive: the
    primary write-path lower-cases, but a future caller might pass
    checksum-cased addresses."""
    eoa_lc = "0xc8d4183100000000000000000000000000000000"
    eoa_ck = "0xC8D4183100000000000000000000000000000000"
    enqueue_messages(eoa_lc, [{"role": "user", "content": "msg"}])

    assert has_pending([eoa_ck]) is True
    drained = drain_pending([eoa_ck])
    assert len(drained) == 1


# ---------------------------------------------------------------------------
# 2. Backwards compatibility — single-string API still works
# ---------------------------------------------------------------------------


def test_single_string_api_still_works(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    msgs = [{"role": "user", "content": "msg"}]
    enqueue_messages(eoa, msgs)

    assert has_pending(eoa) is True
    drained = drain_pending(eoa)
    assert drained == [msgs]


def test_empty_owner_set_is_safe(pending_path):
    eoa = "0xc8d4183100000000000000000000000000000000"
    enqueue_messages(eoa, [{"role": "user", "content": "msg"}])
    # Empty iterable / empty string both treated as "no owner" — never matches.
    assert has_pending([]) is False
    assert drain_pending([]) == []
    assert has_pending("") is False
    assert drain_pending("") == []


# ---------------------------------------------------------------------------
# 3. End-to-end on_session_start scenario — F6 latent failure
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *, eoa: str, sa: Optional[str] = None):
        self._eoa_address = eoa
        # The latent F6 scenario: ``_sa_address`` is set BEFORE
        # on_session_start runs (e.g. eager-resolution in state.configure).
        self._sa_address = sa

    async def recall(self, *_a, **_kw):
        return []


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


def test_owner_addresses_returns_both_when_sa_set():
    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"
    state = _FakeState(_FakeClient(eoa=eoa, sa=sa), [])
    addrs = _owner_addresses(state)
    assert addrs == [eoa, sa]  # EOA first, deduped, lower-cased


def test_owner_addresses_returns_eoa_only_when_sa_unset():
    eoa = "0xc8d4183100000000000000000000000000000000"
    state = _FakeState(_FakeClient(eoa=eoa, sa=None), [])
    addrs = _owner_addresses(state)
    assert addrs == [eoa]


def test_on_session_start_drains_eoa_queue_when_sa_eagerly_resolved(
    pending_path, monkeypatch
):
    """The latent F6 scenario.

    Setup mirrors the real lifecycle race the umbrella's analysis warns
    about: a prior turn enqueued under EOA (because at shutdown
    ``_sa_address`` was None), and the NEXT session's
    ``on_session_start`` runs with ``_sa_address`` already set (e.g. a
    future change adds eager SA-resolution to ``state.configure``).

    Pre-fix: ``on_session_start`` calls ``_owner_address(state)`` which
    returns SA, ``has_pending(SA)`` returns False, drain skips. The
    queued batch stays on disk forever and the messages never get
    auto-extracted.

    Post-fix: ``on_session_start`` calls ``_owner_addresses(state)``
    which returns ``[EOA, SA]``, ``has_pending([EOA, SA])`` returns
    True, the EOA-keyed batch drains successfully.
    """
    from totalreclaw.hermes import hooks

    eoa = "0xc8d4183100000000000000000000000000000000"
    sa = "0x22636a7300000000000000000000000000000000"

    # Prior session enqueued under EOA (at shutdown, SA was None).
    enqueue_messages(
        eoa,
        [
            {"role": "user", "content": "queued from prior session"},
            {"role": "assistant", "content": "queued reply"},
        ],
    )

    # New session: ``_sa_address`` is already set when on_session_start fires.
    state = _FakeState(_FakeClient(eoa=eoa, sa=sa), [])

    drained_calls: list = []

    def fake_auto_extract(state, mode="turn", llm_config=None):
        drained_calls.append({"mode": mode, "msg_count": len(state._messages)})
        return []

    monkeypatch.setattr(hooks, "_auto_extract", fake_auto_extract)
    monkeypatch.setattr(hooks, "_get_hermes_llm_config", lambda: None)

    hooks.on_session_start(state, session_id="test-session")

    # Drain ran, _auto_extract called with the recovered messages.
    assert len(drained_calls) == 1, "drain did not run — F6 still latent"
    assert drained_calls[0]["mode"] == "full"
    assert drained_calls[0]["msg_count"] == 2
    # Queue file was consumed.
    assert not pending_path.exists()
    # User-visible quota warning was set.
    assert state.quota_warning and "caught up" in state.quota_warning
