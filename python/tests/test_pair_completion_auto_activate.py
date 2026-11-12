"""§5.4 (#351) — pairing auto-activates TotalReclaw as the Hermes memory provider.

``complete_pairing`` calls ``activate_after_pairing`` so TotalReclaw is the
active provider the moment the user pairs (non-exclusive since 2.5.3rc4:
Hermes' built-in memory stays on beside it, and another active provider is
left for the user to decide) — best-effort, never failing an
otherwise-successful pairing, and never touching key/phrase/crypto code.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from totalreclaw.hermes.pair_tool_completion import complete_pairing


def _session() -> MagicMock:
    s = MagicMock()
    s.sid = "sess1234"
    return s


def _state_configured() -> MagicMock:
    state = MagicMock()
    client = MagicMock()
    client.eoa_address = "0xEOA"
    state.get_client.return_value = client
    return state


def test_pairing_auto_activates_provider():
    state = _state_configured()
    with patch(
        "totalreclaw.hermes.install_memory_provider.activate_after_pairing",
        return_value={"activated": True, "sidecar_path": "/h/plugins/totalreclaw/__init__.py"},
    ) as act:
        res = complete_pairing("fake phrase", _session(), state)

    state.configure.assert_called_once()
    act.assert_called_once_with()  # provider activated on pair
    assert res.state == "active"
    assert res.account_id == "0xEOA"


def test_activation_failure_does_not_break_pairing():
    state = _state_configured()
    with patch(
        "totalreclaw.hermes.install_memory_provider.activate_after_pairing",
        side_effect=RuntimeError("disk full"),
    ):
        res = complete_pairing("fake phrase", _session(), state)

    # Pairing still succeeds even though activation raised.
    assert res.state == "active"
    assert res.account_id == "0xEOA"


def test_no_activation_when_configure_fails():
    state = MagicMock()
    state.configure.side_effect = RuntimeError("bad phrase")
    with patch(
        "totalreclaw.hermes.install_memory_provider.activate_after_pairing"
    ) as act:
        res = complete_pairing("fake phrase", _session(), state)

    act.assert_not_called()  # no activation if credentials never configured
    assert res.state == "error"
