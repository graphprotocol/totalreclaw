"""Unit tests for ``totalreclaw.credentials_wrap`` (cred-2 / internal#262).

Covers the marker scheme, the wrap/unwrap round-trip, plaintext fallback
on keychain failure, the ``TOTALRECLAW_NO_KEYCHAIN`` kill-switch, the
opportunistic upgrade of legacy plaintext, and the keychain-entry-missing
error path (asserting the message leaks neither the mnemonic nor the
marker payload). The "marker never passes BIP-39 validation" guarantee is
asserted three ways: the ``mnemonic`` package wordlist check,
``eth_account.Account.from_mnemonic`` (the validator used by
``cli.py``/``doctor``/``hermes._validate_mnemonic``), and the Rust
``totalreclaw_core.derive_keys_from_mnemonic`` (the deepest consumer on
the ``agent/state`` → ``configure`` path).

All keychain interactions go through an in-memory fake backend patched
onto the module — the real OS keychain and ``~/.totalreclaw`` are never
touched (also enforced repo-wide by the ``_no_real_keychain`` autouse
fixture in ``conftest.py``).
"""

from __future__ import annotations

import json

import pytest

from totalreclaw import credentials_wrap as cw
from totalreclaw.credentials_wrap import (
    MARKER_PREFIX,
    KeychainEntryMissing,
    KeychainUnavailable,
    account_for_mnemonic,
    is_marker,
    marker_for,
    resolve_mnemonic,
    wrap_credentials,
)

# Two distinct valid 12-word BIP-39 mnemonics (BIP-39 test vectors) so a
# round-trip can prove the *right* secret comes back, not a default.
VALID_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)
VALID_MNEMONIC_2 = (
    "legal winner thank year wave sausage worth useful legal winner thank yellow"
)


# ---------------------------------------------------------------------------
# Fixtures — an in-memory fake keychain backend patched onto the module.
# The autouse ``_no_real_keychain`` fixture in conftest.py forces the
# kill-switch ON for the whole suite; these tests opt back in by deleting
# that env var so the keychain code path is actually exercised.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_keychain(monkeypatch):
    """In-memory keychain. Returns the backing dict for inspection.

    Patches ``detect_backend`` → a non-None backend, ``store_secret`` and
    ``load_secret`` → the in-memory impl, and deletes the kill-switch env
    so the wrap/resolve code paths are live.
    """
    monkeypatch.delenv(cw.ENV_NO_KEYCHAIN, raising=False)
    store: dict[str, str] = {}

    def _store(account: str, secret: str) -> None:
        store[account] = secret

    def _load(account: str) -> str:
        if account not in store:
            raise KeychainEntryMissing(cw.MISSING_MESSAGE)
        return store[account]

    monkeypatch.setattr(cw, "detect_backend", lambda: "test-fake")
    monkeypatch.setattr(cw, "store_secret", _store)
    monkeypatch.setattr(cw, "load_secret", _load)
    return store


# ---------------------------------------------------------------------------
# Marker construction + detection
# ---------------------------------------------------------------------------


def test_marker_for_round_trips_account_through_prefix() -> None:
    acct = "0x" + "1" * 40
    m = marker_for(acct)
    assert m.startswith(MARKER_PREFIX)
    assert m[len(MARKER_PREFIX):] == acct


def test_is_marker_true_for_constructed_marker() -> None:
    assert is_marker(marker_for("0x" + "ab" * 20)) is True


def test_is_marker_false_for_plaintext_mnemonic_and_empty() -> None:
    assert is_marker(VALID_MNEMONIC) is False
    assert is_marker("") is False
    assert is_marker(None) is False  # type: ignore[arg-type]


def test_account_for_mnemonic_is_stable_and_has_no_whitespace() -> None:
    a1 = account_for_mnemonic(VALID_MNEMONIC)
    a2 = account_for_mnemonic(VALID_MNEMONIC)
    assert a1 == a2  # deterministic
    assert a1.startswith("0x")
    assert " " not in a1 and "\t" not in a1 and "\n" not in a1


def test_account_for_mnemonic_differs_per_mnemonic() -> None:
    assert account_for_mnemonic(VALID_MNEMONIC) != account_for_mnemonic(VALID_MNEMONIC_2)


# ---------------------------------------------------------------------------
# wrap / unwrap round-trip (mocked backend)
# ---------------------------------------------------------------------------


def test_wrap_then_resolve_round_trips_the_real_mnemonic(fake_keychain) -> None:
    wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
    # The on-disk mnemonic field is now a marker, not the phrase.
    assert is_marker(wrapped["mnemonic"])
    assert wrapped.get("keychain_wrapped") is True
    assert VALID_MNEMONIC not in wrapped["mnemonic"]

    # resolve_mnemonic pulls the real phrase back from the keychain.
    assert resolve_mnemonic(wrapped) == VALID_MNEMONIC


def test_wrap_uses_eoa_as_keychain_account(fake_keychain) -> None:
    wrap_credentials({"mnemonic": VALID_MNEMONIC})
    eoa = account_for_mnemonic(VALID_MNEMONIC)
    assert VALID_MNEMONIC in fake_keychain.values()
    assert fake_keychain[eoa] == VALID_MNEMONIC


def test_wrap_preserves_non_secret_fields(fake_keychain) -> None:
    wrapped = wrap_credentials(
        {"mnemonic": VALID_MNEMONIC, "scope_address": "0xdeadbeef", "user_id": "u-1"}
    )
    assert wrapped["scope_address"] == "0xdeadbeef"
    assert wrapped["user_id"] == "u-1"
    assert is_marker(wrapped["mnemonic"])


def test_wrap_replaces_legacy_recovery_phrase_key(fake_keychain) -> None:
    # A legacy-shape file keyed under ``recovery_phrase`` gets its value
    # replaced by the marker (same field), and resolves back correctly.
    wrapped = wrap_credentials({"recovery_phrase": VALID_MNEMONIC})
    assert is_marker(wrapped["recovery_phrase"])
    assert "mnemonic" not in wrapped
    assert resolve_mnemonic(wrapped) == VALID_MNEMONIC


# ---------------------------------------------------------------------------
# Plaintext fallback on keychain failure
# ---------------------------------------------------------------------------


def test_wrap_falls_back_to_plaintext_when_store_raises(fake_keychain, monkeypatch):
    def _boom(account, secret):
        raise KeychainUnavailable(cw.UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(cw, "store_secret", _boom)
    wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
    # Plaintext preserved unchanged — no marker, no keychain_wrapped flag.
    assert wrapped == {"mnemonic": VALID_MNEMONIC}
    assert "keychain_wrapped" not in wrapped


def test_wrap_falls_back_to_plaintext_when_no_backend(monkeypatch):
    monkeypatch.delenv(cw.ENV_NO_KEYCHAIN, raising=False)
    monkeypatch.setattr(cw, "detect_backend", lambda: None)
    wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
    assert wrapped == {"mnemonic": VALID_MNEMONIC}
    assert "keychain_wrapped" not in wrapped


def test_wrap_never_raises_even_on_backend_error(fake_keychain, monkeypatch):
    def _boom(account, secret):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(cw, "store_secret", _boom)
    # Must not raise — fallback is silent + plaintext.
    wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
    assert wrapped["mnemonic"] == VALID_MNEMONIC


def test_wrap_on_empty_creds_is_a_noop(fake_keychain) -> None:
    assert wrap_credentials({}) == {}
    assert wrap_credentials({"scope_address": "0x"}) == {"scope_address": "0x"}


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_forces_plaintext_wrap(monkeypatch, fake_keychain):
    # fake_keychain deleted the env; re-arm the kill-switch.
    monkeypatch.setenv(cw.ENV_NO_KEYCHAIN, "1")
    wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
    assert wrapped == {"mnemonic": VALID_MNEMONIC}
    assert fake_keychain == {}  # nothing stored


def test_kill_switch_values_one_true_and_yes(monkeypatch, fake_keychain):
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv(cw.ENV_NO_KEYCHAIN, val)
        wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
        assert wrapped["mnemonic"] == VALID_MNEMONIC, val


def test_kill_switch_empty_or_zero_is_off(monkeypatch, fake_keychain):
    for val in ("", "0", "false", "no"):
        monkeypatch.setenv(cw.ENV_NO_KEYCHAIN, val)
        wrapped = wrap_credentials({"mnemonic": VALID_MNEMONIC})
        assert is_marker(wrapped["mnemonic"]), val


# ---------------------------------------------------------------------------
# Read of legacy plaintext + opportunistic upgrade
# ---------------------------------------------------------------------------


def test_resolve_mnemonic_reads_plaintext_as_today(fake_keychain) -> None:
    # No marker → returns the plaintext, no keychain touch.
    assert resolve_mnemonic({"mnemonic": VALID_MNEMONIC}) == VALID_MNEMONIC
    assert resolve_mnemonic({"recovery_phrase": VALID_MNEMONIC}) == VALID_MNEMONIC


def test_resolve_mnemonic_empty_when_no_credential(fake_keychain) -> None:
    assert resolve_mnemonic({}) == ""
    assert resolve_mnemonic({"mnemonic": ""}) == ""
    assert resolve_mnemonic({"scope_address": "0x"}) == ""


def test_opportunistic_upgrade_wraps_plaintext_on_rewrap(fake_keychain) -> None:
    """Reading legacy plaintext + re-saving through wrap upgrades it to a
    marker (the auto-configure → configure → save path does exactly this).
    """
    legacy = {"mnemonic": VALID_MNEMONIC, "scope_address": "0xabc"}
    # First read: plaintext, untouched.
    assert resolve_mnemonic(legacy) == VALID_MNEMONIC
    # Re-save through wrap → upgraded to marker, phrase now in keychain.
    upgraded = wrap_credentials(legacy)
    assert is_marker(upgraded["mnemonic"])
    assert resolve_mnemonic(upgraded) == VALID_MNEMONIC
    eoa = account_for_mnemonic(VALID_MNEMONIC)
    assert fake_keychain[eoa] == VALID_MNEMONIC


# ---------------------------------------------------------------------------
# Keychain-entry-missing error path — NO sensitive output
# ---------------------------------------------------------------------------


def test_resolve_marker_with_missing_entry_raises_clean_error(fake_keychain) -> None:
    # Marker present but the keychain entry was deleted (e.g. wiped keychain).
    creds = {"mnemonic": marker_for("0x" + "9" * 40)}
    with pytest.raises(KeychainEntryMissing) as ei:
        resolve_mnemonic(creds)
    msg = str(ei.value)
    assert VALID_MNEMONIC not in msg
    assert VALID_MNEMONIC_2 not in msg
    assert MARKER_PREFIX not in msg
    assert "__keychain__" not in msg
    # The marker payload (the embedded account) must not leak either.
    assert "0x" + "9" * 40 not in msg


def test_missing_entry_message_guides_user_to_restore(fake_keychain) -> None:
    creds = {"mnemonic": marker_for(account_for_mnemonic(VALID_MNEMONIC))}
    with pytest.raises(KeychainEntryMissing) as ei:
        resolve_mnemonic(creds)
    msg = str(ei.value).lower()
    assert "restore" in msg or "setup" in msg


def test_resolve_marker_when_kill_switch_armed_raises_clean(monkeypatch, fake_keychain):
    # File is keychain-wrapped but the operator armed the kill-switch
    # (e.g. moved the file to a headless box). Cannot resolve → clean error.
    monkeypatch.setenv(cw.ENV_NO_KEYCHAIN, "1")
    creds = {"mnemonic": marker_for(account_for_mnemonic(VALID_MNEMONIC))}
    with pytest.raises(KeychainEntryMissing) as ei:
        resolve_mnemonic(creds)
    assert VALID_MNEMONIC not in str(ei.value)
    assert MARKER_PREFIX not in str(ei.value)


# ---------------------------------------------------------------------------
# The marker NEVER passes BIP-39 validation — the fail-loud guarantee
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_marker() -> str:
    return marker_for(account_for_mnemonic(VALID_MNEMONIC))


def test_marker_fails_bip39_wordlist_check(sample_marker) -> None:
    """The ``mnemonic`` package (a dev dep) is the reference BIP-39 impl."""
    from mnemonic import Mnemonic

    m = Mnemonic("english")
    assert m.check(sample_marker) is False


def test_marker_fails_eth_account_validation(sample_marker) -> None:
    """``eth_account.Account.from_mnemonic`` is the validator used by
    ``cli.py`` doctor, ``hermes._validate_mnemonic`` and ``client``."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    with pytest.raises(Exception):
        Account.from_mnemonic(sample_marker, account_path="m/44'/60'/0'/0/0")


def test_marker_fails_rust_core_derivation(sample_marker) -> None:
    """The deepest consumer — ``totalreclaw_core.derive_keys_from_mnemonic``
    (Rust) — also rejects the marker. This is the path
    ``agent/state.configure`` → ``client`` → ``crypto`` reaches, so even a
    consumer that does NOT pre-validate cannot silently derive a wrong
    wallet from the marker."""
    from totalreclaw.crypto import derive_keys_from_mnemonic

    with pytest.raises(Exception):
        derive_keys_from_mnemonic(sample_marker)


def test_marker_is_single_token_so_word_count_gate_rejects(sample_marker) -> None:
    """``hermes._validate_mnemonic`` rejects on ``len(words) != 12`` before
    even calling ``from_mnemonic`` — the marker has no spaces (the EOA
    account carries none), so it is a single token."""
    assert len(sample_marker.split()) == 1


# ===========================================================================
# Wiring tests — the keychain path exercised through the REAL entry points
# (hermes setup, totalreclaw doctor, AgentState auto-configure, onboarding).
# The kill-switch is armed for the whole suite by the ``_no_real_keychain``
# autouse fixture; these tests opt back in via ``fake_keychain``.
# ===========================================================================

from io import StringIO  # noqa: E402

from totalreclaw.hermes import cli as hermes_cli  # noqa: E402


def _make_io(stdin_text: str):
    stdin = StringIO(stdin_text)
    io = hermes_cli._IO(stdin=stdin, stdout=StringIO(), stderr=StringIO())
    io.is_tty = True
    return io


def test_setup_writes_marker_when_keychain_available(fake_keychain, tmp_path) -> None:
    """``hermes setup`` restore flow stores the phrase in the keychain and
    writes a marker to credentials.json — never the plaintext phrase."""
    creds = tmp_path / "credentials.json"
    io = _make_io(f"restore\n{VALID_MNEMONIC}\n")

    rc = hermes_cli.run_setup(credentials_path=creds, io=io, allow_non_tty=True)

    assert rc == 0, io.stderr.getvalue()
    saved = json.loads(creds.read_text())
    assert is_marker(saved["mnemonic"])
    assert saved.get("keychain_wrapped") is True
    # The plaintext phrase is NOT on disk anywhere in the file.
    assert VALID_MNEMONIC not in creds.read_text()
    # ...but it IS in the keychain, keyed by the EOA.
    assert fake_keychain[account_for_mnemonic(VALID_MNEMONIC)] == VALID_MNEMONIC


def test_setup_writes_plaintext_when_kill_switch_armed(
    fake_keychain, monkeypatch, tmp_path
) -> None:
    """Kill-switch armed (headless/container) → setup writes plaintext,
    keychain untouched. Same surface the pre-cred-2 path produced."""
    monkeypatch.setenv(cw.ENV_NO_KEYCHAIN, "1")
    creds = tmp_path / "credentials.json"
    io = _make_io(f"restore\n{VALID_MNEMONIC}\n")

    rc = hermes_cli.run_setup(credentials_path=creds, io=io, allow_non_tty=True)

    assert rc == 0, io.stderr.getvalue()
    saved = json.loads(creds.read_text())
    assert saved["mnemonic"] == VALID_MNEMONIC
    assert "keychain_wrapped" not in saved
    assert fake_keychain == {}


def test_doctor_resolves_marker_and_validates(fake_keychain, tmp_path, capsys) -> None:
    """``totalreclaw doctor`` resolves a marker to the real phrase and
    reports a valid BIP-39 phrase (not a validation failure)."""
    from totalreclaw import cli as tr_cli

    creds = tmp_path / "credentials.json"
    eoa = account_for_mnemonic(VALID_MNEMONIC)
    fake_keychain[eoa] = VALID_MNEMONIC
    creds.write_text(
        json.dumps(
            {"mnemonic": marker_for(eoa), "scope_address": "0x" + "f" * 40}
        )
    )

    rc = tr_cli.run_doctor(credentials_path=creds, relay_url="https://invalid.invalid")

    out = capsys.readouterr().out
    assert "valid BIP-39" in out
    # The plaintext phrase must not appear in doctor's output.
    assert VALID_MNEMONIC not in out
    assert rc in (0, 1)  # other checks may warn; the mnemonic check passed


def test_doctor_clean_fail_when_keychain_entry_missing(
    fake_keychain, monkeypatch, tmp_path, capsys
) -> None:
    """Wrapped file + keychain entry gone → doctor prints a clean FAIL that
    leaks neither the phrase nor the marker."""
    from totalreclaw import cli as tr_cli

    creds = tmp_path / "credentials.json"
    # Marker present but the fake keychain is EMPTY (entry was wiped).
    creds.write_text(json.dumps({"mnemonic": marker_for("0x" + "a" * 40)}))

    tr_cli.run_doctor(credentials_path=creds, relay_url="https://invalid.invalid")

    out = capsys.readouterr().out
    assert VALID_MNEMONIC not in out
    assert MARKER_PREFIX not in out
    assert "__keychain__" not in out
    # The non-sensitive guidance is surfaced.
    assert "restore" in out.lower() or "setup" in out.lower()


def test_auto_configure_resolves_marker_to_real_mnemonic(
    fake_keychain, monkeypatch, tmp_path
) -> None:
    """``AgentState`` auto-configures from a marker file by resolving the
    keychain entry — the REAL phrase reaches the client, not the marker."""
    from totalreclaw.agent import AgentState

    creds = tmp_path / "credentials.json"
    eoa = account_for_mnemonic(VALID_MNEMONIC)
    fake_keychain[eoa] = VALID_MNEMONIC
    creds.write_text(json.dumps({"mnemonic": marker_for(eoa)}))
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    state = AgentState()

    assert state.is_configured()
    # The client holds the REAL phrase, proving the marker was resolved —
    # never passed through to derive_keys_from_mnemonic.
    assert state.get_client()._mnemonic == VALID_MNEMONIC


def test_auto_configure_skips_gracefully_when_keychain_entry_missing(
    fake_keychain, monkeypatch, tmp_path
) -> None:
    """Marker file + entry gone → auto-configure logs a clean warning and
    leaves the agent unconfigured (no crash, no sensitive output)."""
    from totalreclaw.agent import AgentState

    creds = tmp_path / "credentials.json"
    # Marker present, keychain empty.
    creds.write_text(json.dumps({"mnemonic": marker_for("0x" + "b" * 40)}))
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    state = AgentState()

    assert not state.is_configured()


def test_auto_configure_upgrades_legacy_plaintext_on_boot(
    fake_keychain, monkeypatch, tmp_path
) -> None:
    """A legacy PLAINTEXT file is read as today, then re-saved through
    ``configure → wrap`` so the next boot finds a marker (opportunistic
    upgrade, best-effort)."""
    from totalreclaw.agent import AgentState

    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"mnemonic": VALID_MNEMONIC}))
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    state = AgentState()
    assert state.is_configured()

    # The file was upgraded to a marker on boot.
    saved = json.loads(creds.read_text())
    assert is_marker(saved["mnemonic"])
    assert saved.get("keychain_wrapped") is True
    assert VALID_MNEMONIC not in creds.read_text()


def test_onboarding_detects_marker_file_as_onboarded(fake_keychain, tmp_path) -> None:
    """A keychain-wrapped user is still 'already onboarded' — first-run
    detection must NOT send them back through the wizard."""
    from totalreclaw.onboarding import detect_first_run

    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"mnemonic": marker_for("0x" + "c" * 40)}))
    assert detect_first_run(creds) is False


def test_end_to_end_setup_then_auto_configure_round_trip(
    fake_keychain, monkeypatch, tmp_path
) -> None:
    """Marker written by ``setup`` is read back by ``AgentState``
    auto-configure — the full write→read loop through the keychain."""
    from totalreclaw.agent import AgentState

    creds = tmp_path / "credentials.json"
    io = _make_io(f"restore\n{VALID_MNEMONIC}\n")
    rc = hermes_cli.run_setup(credentials_path=creds, io=io, allow_non_tty=True)
    assert rc == 0

    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    state = AgentState()
    assert state.is_configured()
    assert state.get_client()._mnemonic == VALID_MNEMONIC

