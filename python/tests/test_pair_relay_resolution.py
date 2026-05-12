"""F1 fix tests (2.3.6rc5) — pair relay URL cascade.

Before 2.3.6rc5, ``totalreclaw.pair.remote_client._resolve_relay_url``
returned the hardcoded staging default unless ``TOTALRECLAW_PAIR_RELAY_URL``
was set. So a real user who set ``TOTALRECLAW_SERVER_URL=https://api-staging...``
(the canonical server-URL env the rest of the Python client respects)
still got the pair flow against the hardcoded relay regardless. Auto-QA
on 2026-05-11 flagged this drift between the documented contract
("default = prod; staging is QA-only opt-in via env") and the package's
behaviour (silent default to staging; only one env honoured).

2.3.6rc5 ships:

1. ``DEFAULT_RELAY_URL`` flipped to ``wss://api.totalreclaw.xyz`` (prod)
2. ``_resolve_relay_url`` now reads a 4-tier env cascade:
   - ``TOTALRECLAW_PAIR_RELAY_URL`` (most specific)
   - ``TOTALRECLAW_SERVER_URL``
   - ``TOTALRECLAW_RELAY_URL``
   - ``DEFAULT_RELAY_URL``
3. Auto-converts ``https://`` → ``wss://`` and ``http://`` → ``ws://`` for
   the SERVER / RELAY envs (they're documented with HTTP prefix).

Why it matters: a real user installing stable from PyPI without any env
vars set would silently route their pair session through staging,
landing memories on Base Sepolia (testnet) instead of whichever mainnet
they intended. The guide promises prod-by-default; this fix aligns the
package with the guide.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from totalreclaw.pair import remote_client


_RELAY_ENV_NAMES = (
    "TOTALRECLAW_PAIR_RELAY_URL",
    "TOTALRECLAW_SERVER_URL",
    "TOTALRECLAW_RELAY_URL",
)


@pytest.fixture
def clear_relay_env(monkeypatch):
    """Strip all relay-URL env vars so each test starts from the same
    baseline (no env override → DEFAULT_RELAY_URL falls through)."""
    for name in _RELAY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# DEFAULT_RELAY_URL — flipped to prod in 2.3.6rc5
# ---------------------------------------------------------------------------


def test_default_relay_is_production() -> None:
    """Module-level DEFAULT must be the production relay, not staging."""
    assert remote_client.DEFAULT_RELAY_URL == "wss://api.totalreclaw.xyz", (
        "DEFAULT_RELAY_URL regressed back to staging or some other URL. "
        "F1 fix: must be the production relay wss://api.totalreclaw.xyz."
    )


def test_no_env_falls_back_to_production(clear_relay_env) -> None:
    """A user with no env vars set gets the production relay."""
    assert remote_client._resolve_relay_url() == "wss://api.totalreclaw.xyz"


# ---------------------------------------------------------------------------
# Cascade priority — TOTALRECLAW_PAIR_RELAY_URL wins
# ---------------------------------------------------------------------------


def test_pair_relay_env_takes_priority_over_server_and_relay(
    clear_relay_env, monkeypatch
) -> None:
    monkeypatch.setenv("TOTALRECLAW_PAIR_RELAY_URL", "wss://pair-override.example")
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://server-other.example")
    monkeypatch.setenv("TOTALRECLAW_RELAY_URL", "https://relay-other.example")
    assert remote_client._resolve_relay_url() == "wss://pair-override.example"


# ---------------------------------------------------------------------------
# Cascade priority — TOTALRECLAW_SERVER_URL is tier 2
# ---------------------------------------------------------------------------


def test_server_url_used_when_pair_relay_not_set(
    clear_relay_env, monkeypatch
) -> None:
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz")
    assert (
        remote_client._resolve_relay_url() == "wss://api-staging.totalreclaw.xyz"
    ), "TOTALRECLAW_SERVER_URL must be honoured + scheme-converted to wss://"


def test_server_url_https_converted_to_wss(clear_relay_env, monkeypatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://custom.example.com")
    assert remote_client._resolve_relay_url() == "wss://custom.example.com"


def test_server_url_http_converted_to_ws(clear_relay_env, monkeypatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "http://localhost:18789")
    assert remote_client._resolve_relay_url() == "ws://localhost:18789"


# ---------------------------------------------------------------------------
# Cascade priority — TOTALRECLAW_RELAY_URL is tier 3
# ---------------------------------------------------------------------------


def test_relay_url_used_when_higher_tiers_not_set(
    clear_relay_env, monkeypatch
) -> None:
    monkeypatch.setenv("TOTALRECLAW_RELAY_URL", "https://api-staging.totalreclaw.xyz")
    assert (
        remote_client._resolve_relay_url() == "wss://api-staging.totalreclaw.xyz"
    )


def test_server_url_wins_over_relay_url(clear_relay_env, monkeypatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://server.example")
    monkeypatch.setenv("TOTALRECLAW_RELAY_URL", "https://relay.example")
    assert remote_client._resolve_relay_url() == "wss://server.example", (
        "Cascade priority regressed — TOTALRECLAW_SERVER_URL should win "
        "over TOTALRECLAW_RELAY_URL."
    )


# ---------------------------------------------------------------------------
# Trailing-slash normalisation
# ---------------------------------------------------------------------------


def test_trailing_slash_stripped(clear_relay_env, monkeypatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://api.example.com/")
    assert remote_client._resolve_relay_url() == "wss://api.example.com"


# ---------------------------------------------------------------------------
# _http_to_ws — scheme-conversion helper
# ---------------------------------------------------------------------------


def test_http_to_ws_https_to_wss() -> None:
    assert remote_client._http_to_ws("https://x.example") == "wss://x.example"


def test_http_to_ws_http_to_ws() -> None:
    assert remote_client._http_to_ws("http://x.example") == "ws://x.example"


def test_http_to_ws_passthrough_for_already_ws() -> None:
    """Values that already start with wss:// or ws:// must pass through."""
    assert remote_client._http_to_ws("wss://x.example") == "wss://x.example"
    assert remote_client._http_to_ws("ws://x.example") == "ws://x.example"


def test_http_to_ws_passthrough_for_unprefixed() -> None:
    """Unprefixed bare host:port passes through (caller may rely on this
    for tests that don't care about scheme)."""
    assert remote_client._http_to_ws("api.example.com") == "api.example.com"
