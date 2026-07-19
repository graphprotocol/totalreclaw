"""Tests for totalreclaw.keychain_wrap (cred-2).

Covers the stable low-level interface (``wrap_blob`` / ``unwrap_blob`` /
``delete_blob``) that the session-key auto-migration is written against, the
pure convenience layer over a credentials dict, and — crucially — the
documented headless / container plaintext fallback.

The tests are **hermetic**: they never touch a real OS keychain and do not
require the optional ``keyring`` dependency to be installed. They drive the
module through its single ``_load_keyring`` seam with an in-memory fake that
mimics keyring's ``get_keyring`` / ``set_password`` / ``get_password`` /
``delete_password`` surface.
"""
from __future__ import annotations

import pytest

from totalreclaw import keychain_wrap


# ---------------------------------------------------------------------------
# In-memory fake keyring (mimics the real keyring module surface we use)
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Stand-in for a real, usable backend (class name avoids fail/null)."""


class _FailBackend:
    """Stand-in for keyring's null / fail backend."""


class _SecretServiceBackend:
    """Stand-in for the Linux Secret Service backend (needs D-Bus)."""


class FakeKeyring:
    """Minimal in-memory keyring double."""

    def __init__(self, backend=None):
        self._store: dict[tuple[str, str], str] = {}
        self._backend = backend if backend is not None else _FakeBackend()

    def get_keyring(self):
        return self._backend

    def set_password(self, service: str, name: str, value: str) -> None:
        self._store[(service, name)] = value

    def get_password(self, service: str, name: str):
        return self._store.get((service, name))

    def delete_password(self, service: str, name: str) -> None:
        try:
            del self._store[(service, name)]
        except KeyError as exc:  # mimic keyring.errors.PasswordDeleteError
            raise RuntimeError("not found") from exc


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install an available in-memory keyring via the module seam."""
    kr = FakeKeyring()
    monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: kr)
    return kr


@pytest.fixture
def no_keyring(monkeypatch):
    """Simulate ``keyring`` not installed."""
    monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: None)


# A realistic-looking (but throwaway) 12-word test phrase.
SAMPLE_PHRASE = "abandon " * 11 + "about"


# ---------------------------------------------------------------------------
# keychain_available
# ---------------------------------------------------------------------------


class TestKeychainAvailable:
    def test_available_with_real_backend(self, fake_keyring):
        assert keychain_wrap.keychain_available() is True

    def test_unavailable_when_keyring_missing(self, no_keyring):
        assert keychain_wrap.keychain_available() is False

    def test_unavailable_with_fail_backend(self, monkeypatch):
        kr = FakeKeyring(backend=_FailBackend())
        monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: kr)
        assert keychain_wrap.keychain_available() is False

    def test_linux_secret_service_needs_dbus(self, monkeypatch):
        kr = FakeKeyring(backend=_SecretServiceBackend())
        monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: kr)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        assert keychain_wrap.keychain_available() is False
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
        assert keychain_wrap.keychain_available() is True

    def test_never_raises_when_get_keyring_explodes(self, monkeypatch):
        class Boom:
            def get_keyring(self):
                raise RuntimeError("dbus down")

        monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: Boom())
        assert keychain_wrap.keychain_available() is False


# ---------------------------------------------------------------------------
# wrap_blob / unwrap_blob / delete_blob
# ---------------------------------------------------------------------------


class TestBlobRoundTrip:
    def test_round_trip(self, fake_keyring):
        keychain_wrap.wrap_blob("entry-1", b"\x00\x01secret\xff")
        assert keychain_wrap.unwrap_blob("entry-1") == b"\x00\x01secret\xff"

    def test_stored_value_is_not_plaintext(self, fake_keyring):
        keychain_wrap.wrap_blob("entry-1", SAMPLE_PHRASE.encode("utf-8"))
        # Persisted form is base64, not the raw phrase.
        raw = fake_keyring.get_password(keychain_wrap.SERVICE_NAME, "entry-1")
        assert SAMPLE_PHRASE not in raw

    def test_overwrite(self, fake_keyring):
        keychain_wrap.wrap_blob("e", b"one")
        keychain_wrap.wrap_blob("e", b"two")
        assert keychain_wrap.unwrap_blob("e") == b"two"

    def test_wrap_rejects_non_bytes(self, fake_keyring):
        with pytest.raises(TypeError):
            keychain_wrap.wrap_blob("e", "a string")  # type: ignore[arg-type]

    def test_unwrap_missing_entry_raises_keyerror(self, fake_keyring):
        with pytest.raises(KeyError):
            keychain_wrap.unwrap_blob("nope")

    def test_wrap_unavailable_raises(self, no_keyring):
        with pytest.raises(keychain_wrap.KeychainUnavailable):
            keychain_wrap.wrap_blob("e", b"x")

    def test_unwrap_unavailable_raises(self, no_keyring):
        with pytest.raises(keychain_wrap.KeychainUnavailable):
            keychain_wrap.unwrap_blob("e")

    def test_delete(self, fake_keyring):
        keychain_wrap.wrap_blob("e", b"x")
        keychain_wrap.delete_blob("e")
        with pytest.raises(KeyError):
            keychain_wrap.unwrap_blob("e")

    def test_delete_absent_is_noop(self, fake_keyring):
        keychain_wrap.delete_blob("never-existed")  # must not raise

    def test_corrupt_entry_raises_keychain_error(self, fake_keyring):
        fake_keyring.set_password(keychain_wrap.SERVICE_NAME, "e", "!!!not-base64!!!")
        with pytest.raises(keychain_wrap.KeychainError):
            keychain_wrap.unwrap_blob("e")


# ---------------------------------------------------------------------------
# entry_name_for
# ---------------------------------------------------------------------------


class TestEntryName:
    def test_uses_userid(self):
        assert keychain_wrap.entry_name_for({"userId": "u-42"}) == "credential-secret:u-42"

    def test_uses_snake_case_user_id(self):
        assert keychain_wrap.entry_name_for({"user_id": "u-7"}) == "credential-secret:u-7"

    def test_falls_back_to_default(self):
        assert keychain_wrap.entry_name_for({}) == keychain_wrap.DEFAULT_ENTRY_NAME

    def test_blank_userid_falls_back(self):
        assert keychain_wrap.entry_name_for({"userId": "   "}) == keychain_wrap.DEFAULT_ENTRY_NAME


# ---------------------------------------------------------------------------
# wrap_credentials_mnemonic — the credentials-dict convenience layer
# ---------------------------------------------------------------------------


class TestWrapCredentials:
    def test_wraps_and_drops_secret(self, fake_keyring):
        creds = {"userId": "u-1", "salt": "s", "scope_address": "0xabc", "mnemonic": SAMPLE_PHRASE}
        out = keychain_wrap.wrap_credentials_mnemonic(creds)

        # secret removed from the on-disk shape
        assert "mnemonic" not in out
        assert "recovery_phrase" not in out
        assert out["keychain_wrapped"] is True
        # non-secret discovery metadata preserved
        assert out["userId"] == "u-1"
        assert out["salt"] == "s"
        assert out["scope_address"] == "0xabc"
        # and it round-trips back out of the keychain
        assert keychain_wrap.unwrap_credentials_mnemonic(out) == SAMPLE_PHRASE

    def test_does_not_mutate_input(self, fake_keyring):
        creds = {"userId": "u-1", "mnemonic": SAMPLE_PHRASE}
        keychain_wrap.wrap_credentials_mnemonic(creds)
        assert creds["mnemonic"] == SAMPLE_PHRASE  # original untouched
        assert "keychain_wrapped" not in creds

    def test_handles_recovery_phrase_key(self, fake_keyring):
        creds = {"userId": "u-2", "recovery_phrase": SAMPLE_PHRASE}
        out = keychain_wrap.wrap_credentials_mnemonic(creds)
        assert "recovery_phrase" not in out
        assert out["keychain_wrapped"] is True
        assert keychain_wrap.unwrap_credentials_mnemonic(out) == SAMPLE_PHRASE

    def test_no_secret_is_noop(self, fake_keyring):
        creds = {"userId": "u-3", "scope_address": "0xabc"}
        out = keychain_wrap.wrap_credentials_mnemonic(creds)
        assert out == creds

    def test_fallback_keeps_plaintext_and_marks_flag(self, no_keyring, tmp_path):
        cred_file = tmp_path / "credentials.json"
        cred_file.write_text("{}")
        cred_file.chmod(0o644)

        creds = {"userId": "u-4", "mnemonic": SAMPLE_PHRASE}
        out = keychain_wrap.wrap_credentials_mnemonic(creds, harden_path=cred_file)

        # secret stays in the file (documented lower protection level)
        assert out["mnemonic"] == SAMPLE_PHRASE
        assert out["keychain_wrapped"] is False
        # file hardened to 0o600
        assert (cred_file.stat().st_mode & 0o777) == 0o600

    def test_fallback_never_raises(self, no_keyring):
        creds = {"mnemonic": SAMPLE_PHRASE}
        # no keychain, no harden_path — must return cleanly
        out = keychain_wrap.wrap_credentials_mnemonic(creds)
        assert out["keychain_wrapped"] is False
        assert out["mnemonic"] == SAMPLE_PHRASE

    def test_custom_entry_name(self, fake_keyring):
        creds = {"mnemonic": SAMPLE_PHRASE}
        out = keychain_wrap.wrap_credentials_mnemonic(creds, entry_name="custom")
        assert keychain_wrap.unwrap_blob("custom") == SAMPLE_PHRASE.encode("utf-8")
        assert keychain_wrap.unwrap_credentials_mnemonic(out, entry_name="custom") == SAMPLE_PHRASE


# ---------------------------------------------------------------------------
# unwrap_credentials_mnemonic — reading either at-rest shape
# ---------------------------------------------------------------------------


class TestUnwrapCredentials:
    def test_reads_plaintext_legacy(self, fake_keyring):
        creds = {"mnemonic": SAMPLE_PHRASE}  # keychain_wrapped absent → legacy
        assert keychain_wrap.unwrap_credentials_mnemonic(creds) == SAMPLE_PHRASE

    def test_reads_plaintext_recovery_phrase(self, no_keyring):
        creds = {"recovery_phrase": SAMPLE_PHRASE, "keychain_wrapped": False}
        assert keychain_wrap.unwrap_credentials_mnemonic(creds) == SAMPLE_PHRASE

    def test_reads_wrapped(self, fake_keyring):
        creds = {"userId": "u-9", "mnemonic": SAMPLE_PHRASE}
        wrapped = keychain_wrap.wrap_credentials_mnemonic(creds)
        assert keychain_wrap.unwrap_credentials_mnemonic(wrapped) == SAMPLE_PHRASE

    def test_missing_everywhere_raises(self, fake_keyring):
        with pytest.raises(KeyError):
            keychain_wrap.unwrap_credentials_mnemonic({"userId": "u-x"})

    def test_wrapped_but_backend_gone_raises(self, monkeypatch):
        # Wrap with a working backend, then simulate the backend disappearing.
        kr = FakeKeyring()
        monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: kr)
        creds = {"userId": "u-10", "mnemonic": SAMPLE_PHRASE}
        wrapped = keychain_wrap.wrap_credentials_mnemonic(creds)

        monkeypatch.setattr(keychain_wrap, "_load_keyring", lambda: None)
        with pytest.raises(keychain_wrap.KeychainUnavailable):
            keychain_wrap.unwrap_credentials_mnemonic(wrapped)
