"""Regression: confirm_indexed must not crash when the installed
totalreclaw_core wheel pre-dates the confirm_indexed PyO3 binding
(published totalreclaw-core==2.2.0 ships without it).

Issue: https://github.com/p-diogo/totalreclaw-internal/issues/149
"""
from __future__ import annotations

import json

import pytest

from totalreclaw import confirm_indexed as ci


@pytest.fixture
def core_without_confirm_indexed(monkeypatch):
    """Simulate the published totalreclaw-core==2.2.0 wheel — strip the
    four confirm_indexed exports added in the post-2.2.0 Rust source."""
    for attr in (
        "confirm_indexed_query",
        "confirm_indexed_parse",
        "confirm_indexed_default_poll_ms",
        "confirm_indexed_default_timeout_ms",
    ):
        monkeypatch.delattr(ci._core, attr, raising=False)


def test_query_string_falls_back_to_inline(core_without_confirm_indexed):
    q = ci._query_string()
    assert "ConfirmIndexed" in q
    assert "$id: ID!" in q
    assert "isActive" in q


def test_parse_response_handles_wrapped_active(core_without_confirm_indexed):
    payload = json.dumps({"data": {"fact": {"id": "abc", "isActive": True}}})
    assert ci._parse_response(payload) is True


def test_parse_response_handles_wrapped_inactive(core_without_confirm_indexed):
    payload = json.dumps({"data": {"fact": {"id": "abc", "isActive": False}}})
    assert ci._parse_response(payload) is False


def test_parse_response_handles_unwrapped_shape(core_without_confirm_indexed):
    payload = json.dumps({"fact": {"id": "abc", "isActive": True}})
    assert ci._parse_response(payload) is True


def test_parse_response_handles_null_fact(core_without_confirm_indexed):
    payload = json.dumps({"data": {"fact": None}})
    assert ci._parse_response(payload) is False


def test_default_poll_ms_falls_back(core_without_confirm_indexed):
    assert ci._default_poll_ms() == 1_000


def test_default_timeout_ms_falls_back(core_without_confirm_indexed):
    assert ci._default_timeout_ms() == 30_000


def test_real_core_path_when_present():
    """When the core wheel exposes the bindings, the helpers must delegate
    to them rather than the inline fallback. Skips cleanly on older wheels."""
    if not hasattr(ci._core, "confirm_indexed_query"):
        pytest.skip("installed totalreclaw_core lacks confirm_indexed_query")
    q = ci._query_string()
    assert "fact" in q.lower()
    payload = json.dumps({"data": {"fact": {"id": "x", "isActive": True}}})
    assert ci._parse_response(payload) is True
