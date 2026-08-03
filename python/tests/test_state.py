"""Credential-reader precedence tests for ``AgentState`` (Option E Phase 2,
P2-8, #581).

Covers the four-state precedence derived-bundle-v1.md §6 / the
client-consistency patch specify:

  1. ``TOTALRECLAW_RECOVERY_PHRASE`` env — highest precedence, deprecated.
  2/3. Credential-provider payload, ``version: 2`` -> derived-bundle-v1
     (keychain-unwrapped first if ``keychain_wrapped``).
  4. Legacy: plaintext ``{"mnemonic": ...}`` or the ``__keychain__:v1:<eoa>``
     marker.

Plus the loud-failure rule: an unrecognised ``version`` must raise, never
silently fall back to legacy handling.

Mirrors ``test_external_credentials_boot.py``'s fixture conventions
(isolated ``$HOME``, explicit env teardown) and
``test_credentials_wrap.py``'s ``fake_keychain`` pattern for the v2
keychain-wrapped scenarios.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

totalreclaw_core = pytest.importorskip("totalreclaw_core")

pytestmark = pytest.mark.skipif(
    not hasattr(totalreclaw_core, "derive_bundle_from_mnemonic"),
    reason="installed totalreclaw_core predates the derived-bundle-v1 bindings (#581)",
)

from totalreclaw import credentials_wrap as cw  # noqa: E402
from totalreclaw.agent.state import AgentState  # noqa: E402
from totalreclaw.bundle import derive_bundle_from_mnemonic  # noqa: E402
from totalreclaw.credential_provider import (  # noqa: E402
    ENV_CREDENTIALS_PATH,
    ENV_EXTERNAL_JSON,
    ENV_EXTERNAL_PATH,
    ENV_PROVIDER,
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SMART_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Reroute $HOME so no test contaminates the real ~/.totalreclaw."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    monkeypatch.delenv(ENV_CREDENTIALS_PATH, raising=False)
    monkeypatch.delenv(ENV_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)
    return fake_home


@pytest.fixture
def fake_keychain(monkeypatch):
    """In-memory keychain — mirrors test_credentials_wrap.py's fixture."""
    monkeypatch.delenv(cw.ENV_NO_KEYCHAIN, raising=False)
    store: dict[str, str] = {}

    def _store(account: str, secret: str) -> None:
        store[account] = secret

    def _load(account: str) -> str:
        if account not in store:
            raise cw.KeychainEntryMissing(cw.MISSING_MESSAGE)
        return store[account]

    monkeypatch.setattr(cw, "detect_backend", lambda: "test-fake")
    monkeypatch.setattr(cw, "store_secret", _store)
    monkeypatch.setattr(cw, "load_secret", _load)
    return store


def _write_creds(isolated_home: Path, creds: dict) -> Path:
    creds_dir = isolated_home / ".totalreclaw"
    creds_dir.mkdir(exist_ok=True)
    creds_file = creds_dir / "credentials.json"
    creds_file.write_text(json.dumps(creds))
    return creds_file


def _canonical_bundle():
    return derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT)


def _headless_v2_creds() -> dict:
    """The full, un-wrapped v2 shape (headless / external provider —
    derived-bundle-v1.md §4.3)."""
    bundle = _canonical_bundle()
    return {
        "version": 2,
        "schema": "derived-bundle-v1",
        "vault": {
            "encryption_key": bundle.vault.encryption_key.hex(),
            "dedup_key": bundle.vault.dedup_key.hex(),
            "auth_key": bundle.vault.auth_key.hex(),
            "lsh_seed": bundle.vault.lsh_seed.hex(),
        },
        "signing": {
            "kind": bundle.signing.kind,
            "private_key": bundle.signing.private_key.hex(),
            "address": bundle.signing.address,
        },
        "account": {
            "smart_account": bundle.account.smart_account,
            "chain_id": bundle.account.chain_id,
        },
        "provisioned_at": bundle.provisioned_at,
        "provisioned_by": bundle.provisioned_by,
    }


def _keychain_wrapped_v2_discovery_metadata() -> dict:
    """The non-secret discovery-metadata shape credentials.json carries
    when keychain_wrapped=true (derived-bundle-v1.md §4.3)."""
    bundle = _canonical_bundle()
    return {
        "version": 2,
        "schema": "derived-bundle-v1",
        "keychain_wrapped": True,
        "account": {
            "smart_account": bundle.account.smart_account,
            "chain_id": bundle.account.chain_id,
        },
        "signing": {"kind": bundle.signing.kind, "address": bundle.signing.address},
        "provisioned_at": bundle.provisioned_at,
        "provisioned_by": bundle.provisioned_by,
    }


# ---------------------------------------------------------------------------
# State 1 — env var, highest precedence
# ---------------------------------------------------------------------------


def test_env_var_wins_over_a_present_v2_file(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_creds(isolated_home, _headless_v2_creds())
    monkeypatch.setenv("TOTALRECLAW_RECOVERY_PHRASE", TEST_MNEMONIC)

    state = AgentState()

    assert state.is_configured()
    assert state.get_client()._mnemonic == TEST_MNEMONIC


def test_env_var_emits_a_deprecation_nudge(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("TOTALRECLAW_RECOVERY_PHRASE", TEST_MNEMONIC)
    import logging

    with caplog.at_level(logging.INFO, logger="totalreclaw.agent.state"):
        AgentState()

    assert any("deprecated" in rec.message for rec in caplog.records)
    # Phrase-safety: the nudge must never contain the phrase itself.
    assert not any(TEST_MNEMONIC in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# States 2/3 — version:2 -> derived-bundle-v1
# ---------------------------------------------------------------------------


def test_v2_headless_credentials_configure_from_bundle(isolated_home: Path) -> None:
    """Headless / external-provider shape — no keychain unwrap needed."""
    _write_creds(isolated_home, _headless_v2_creds())

    state = AgentState()

    assert state.is_configured()
    client = state.get_client()
    assert client._mnemonic is None
    assert client.wallet_address == TEST_SMART_ACCOUNT.lower()


def test_v2_keychain_wrapped_credentials_unwrap_and_configure(
    isolated_home: Path, fake_keychain
) -> None:
    bundle = _canonical_bundle()
    secret_subtree = json.dumps(
        {
            "vault": {
                "encryption_key": bundle.vault.encryption_key.hex(),
                "dedup_key": bundle.vault.dedup_key.hex(),
                "auth_key": bundle.vault.auth_key.hex(),
                "lsh_seed": bundle.vault.lsh_seed.hex(),
            },
            "signing": {
                "kind": bundle.signing.kind,
                "private_key": bundle.signing.private_key.hex(),
                "address": bundle.signing.address,
            },
        }
    )
    ok = cw.wrap_bundle(bundle.account.smart_account, secret_subtree)
    assert ok is True

    _write_creds(isolated_home, _keychain_wrapped_v2_discovery_metadata())

    state = AgentState()

    assert state.is_configured()
    client = state.get_client()
    assert client._mnemonic is None
    assert client.wallet_address == bundle.account.smart_account.lower()
    assert client._keys.encryption_key == bundle.vault.encryption_key


def test_v2_keychain_wrapped_missing_entry_stays_unconfigured(isolated_home: Path, fake_keychain) -> None:
    """Entry never wrapped (or gone/locked) — same graceful-skip shape as
    the legacy v1 KeychainEntryMissing path, not a raise."""
    _write_creds(isolated_home, _keychain_wrapped_v2_discovery_metadata())

    state = AgentState()

    assert not state.is_configured()


def test_v2_keychain_wrapped_missing_smart_account_field_raises(isolated_home: Path, fake_keychain) -> None:
    bad = _keychain_wrapped_v2_discovery_metadata()
    del bad["account"]["smart_account"]
    _write_creds(isolated_home, bad)

    with pytest.raises(ValueError, match="smart_account"):
        AgentState()


def test_v2_malformed_bundle_raises_not_silently_skipped(isolated_home: Path) -> None:
    """A corrupted v2 file (bad hex) must propagate loudly — distinct from
    the keychain-entry-missing case, which is an operational condition,
    not evidence of tampering."""
    bad = _headless_v2_creds()
    bad["vault"]["encryption_key"] = "not-valid-hex"
    _write_creds(isolated_home, bad)

    with pytest.raises(ValueError):
        AgentState()


def test_external_mode_v2_bundle_boots_via_inline_json(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.setenv(ENV_EXTERNAL_JSON, json.dumps(_headless_v2_creds()))

    # Critically: no credentials.json on disk.
    assert not (isolated_home / ".totalreclaw" / "credentials.json").exists()

    state = AgentState()

    assert state.is_configured()
    assert state.get_client()._mnemonic is None


# ---------------------------------------------------------------------------
# State 4 — legacy plaintext / v1 keychain marker
# ---------------------------------------------------------------------------


def test_legacy_plaintext_mnemonic_still_configures(isolated_home: Path) -> None:
    _write_creds(isolated_home, {"mnemonic": TEST_MNEMONIC})

    state = AgentState()

    assert state.is_configured()
    assert state.get_client()._mnemonic == TEST_MNEMONIC


def test_legacy_v1_keychain_marker_still_configures(isolated_home: Path, fake_keychain) -> None:
    wrapped = cw.wrap_credentials({"mnemonic": TEST_MNEMONIC})
    assert cw.is_marker(wrapped["mnemonic"])
    _write_creds(isolated_home, wrapped)

    state = AgentState()

    assert state.is_configured()
    assert state.get_client()._mnemonic == TEST_MNEMONIC


# ---------------------------------------------------------------------------
# Unknown version — loud error, never a silent downgrade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_version", [3, 99, "two", 1])
def test_unknown_version_raises(isolated_home: Path, bad_version) -> None:
    creds = _headless_v2_creds()
    creds["version"] = bad_version
    _write_creds(isolated_home, creds)

    with pytest.raises(ValueError, match="version"):
        AgentState()


def test_absent_version_key_is_legacy_not_an_error(isolated_home: Path) -> None:
    """No version key at all (every pre-Phase-2 credentials.json) must NOT
    be treated as an unknown version — it's the legacy shape, state 4."""
    _write_creds(isolated_home, {"mnemonic": TEST_MNEMONIC})

    state = AgentState()  # must not raise

    assert state.is_configured()
