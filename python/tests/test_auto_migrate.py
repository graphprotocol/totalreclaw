"""Tests for ``hermes/auto_migrate.py`` (Option E Phase 2, P2-9, #581).

All fixtures supply a cached ``scope_address`` in the legacy credentials
dict so ``_resolve_smart_account`` never needs a network round-trip — the
load-bearing migration-preserves-readability test in particular must run
fully offline.

Interpretation note on step 5 ("keychain-unavailable" handling): the
implementation spec's step 5 says wrap-or-leave-in-file (a keychain-less
host is a supported headless configuration per derived-bundle-v1.md §4.3),
while the "failure handling" paragraph separately names a
"keychain-unavailable aborts with the file untouched" test. These two
read as being about DIFFERENT keychain touch-points: step 5 is about
wrapping the NEWLY DERIVED bundle (falls back to an in-file v2 write, so a
`TOTALRECLAW_NO_KEYCHAIN=1` headless host still gets migrated — the
population this phase primarily protects); the "aborts untouched" case is
about resolving the SOURCE mnemonic when the existing credential is
v1-keychain-wrapped and that keychain entry is gone/locked — there is
nothing to derive a bundle FROM in that case, so migration cannot proceed
at all and nothing is touched. Both scenarios are tested below.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

totalreclaw_core = pytest.importorskip("totalreclaw_core")

pytestmark = pytest.mark.skipif(
    not hasattr(totalreclaw_core, "derive_bundle_from_mnemonic"),
    reason="installed totalreclaw_core predates the derived-bundle-v1 bindings (#581)",
)

from totalreclaw import credentials_wrap as cw  # noqa: E402
from totalreclaw.credential_provider import ENV_CREDENTIALS_PATH  # noqa: E402
from totalreclaw.hermes import auto_migrate  # noqa: E402

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SMART_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    monkeypatch.delenv(ENV_CREDENTIALS_PATH, raising=False)
    monkeypatch.delenv(auto_migrate.ENV_NO_AUTO_MIGRATE, raising=False)
    return fake_home


@pytest.fixture
def fake_keychain(monkeypatch):
    monkeypatch.delenv(cw.ENV_NO_KEYCHAIN, raising=False)
    store: dict[str, str] = {}

    def _store(account: str, secret: str) -> None:
        store[account] = secret

    def _load(account: str) -> str:
        if account not in store:
            raise cw.KeychainEntryMissing(cw.MISSING_MESSAGE)
        return store[account]

    def _delete(account: str) -> None:
        store.pop(account, None)

    monkeypatch.setattr(cw, "detect_backend", lambda: "test-fake")
    monkeypatch.setattr(cw, "store_secret", _store)
    monkeypatch.setattr(cw, "load_secret", _load)
    monkeypatch.setattr(cw, "delete_secret", _delete)
    return store


def _creds_dir(home: Path) -> Path:
    d = home / ".totalreclaw"
    d.mkdir(exist_ok=True)
    return d


def _write_legacy_plaintext(home: Path) -> Path:
    path = _creds_dir(home) / "credentials.json"
    path.write_text(json.dumps({"mnemonic": TEST_MNEMONIC, "scope_address": TEST_SMART_ACCOUNT}))
    return path


def _write_legacy_v1_keychain(home: Path, fake_keychain) -> Path:
    wrapped = cw.wrap_credentials({"mnemonic": TEST_MNEMONIC})
    assert cw.is_marker(wrapped["mnemonic"])
    wrapped["scope_address"] = TEST_SMART_ACCOUNT
    path = _creds_dir(home) / "credentials.json"
    path.write_text(json.dumps(wrapped))
    return path


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_legacy_plaintext_migrates(isolated_home: Path, fake_keychain) -> None:
    creds_path = _write_legacy_plaintext(isolated_home)

    migrated = auto_migrate.maybe_migrate()

    assert migrated is True
    v2 = json.loads(creds_path.read_text())
    assert v2["version"] == 2
    assert v2["schema"] == "derived-bundle-v1"
    assert v2.get("keychain_wrapped") is True
    assert "vault" not in v2  # secret subtree lives in the keychain, not the file
    bak_path = creds_path.parent / "credentials.json.bak"
    assert bak_path.exists()
    assert json.loads(bak_path.read_text()) == {
        "mnemonic": TEST_MNEMONIC,
        "scope_address": TEST_SMART_ACCOUNT,
    }


def test_v1_keychain_wrapped_migrates(isolated_home: Path, fake_keychain) -> None:
    _write_legacy_v1_keychain(isolated_home, fake_keychain)

    migrated = auto_migrate.maybe_migrate()

    assert migrated is True
    creds_path = isolated_home / ".totalreclaw" / "credentials.json"
    v2 = json.loads(creds_path.read_text())
    assert v2["version"] == 2


def test_second_invocation_is_a_noop(isolated_home: Path, fake_keychain) -> None:
    _write_legacy_plaintext(isolated_home)
    first = auto_migrate.maybe_migrate()
    assert first is True

    second = auto_migrate.maybe_migrate()
    assert second is False  # already version:2 — nothing to do


def test_env_opt_out_skips(isolated_home: Path, monkeypatch: pytest.MonkeyPatch, fake_keychain) -> None:
    creds_path = _write_legacy_plaintext(isolated_home)
    original = creds_path.read_text()
    monkeypatch.setenv(auto_migrate.ENV_NO_AUTO_MIGRATE, "1")

    migrated = auto_migrate.maybe_migrate()

    assert migrated is False
    assert creds_path.read_text() == original


def test_v1_keychain_entry_is_retained_after_successful_migration(
    isolated_home: Path, fake_keychain
) -> None:
    """2026-08-03 fix (major-2): the v1 keychain entry must NEVER be
    deleted by auto_migrate. For a keychain-sourced install, .bak (the
    renamed old credentials.json) contains only the __keychain__:v1:<eoa>
    marker, never the phrase — deleting the v1 entry after migration
    destroyed the ONLY remaining copy of the root. The entry is retained
    on the same one-release-cycle policy as .bak, gated on the same #579
    sunset decision (not automated here)."""
    _write_legacy_v1_keychain(isolated_home, fake_keychain)
    v1_account = cw.account_for_mnemonic(TEST_MNEMONIC)
    assert v1_account in fake_keychain

    migrated = auto_migrate.maybe_migrate()

    assert migrated is True
    assert v1_account in fake_keychain, (
        "the v1 keychain entry must be RETAINED — deleting it destroys the "
        "only remaining copy of the phrase for a keychain-sourced install, "
        "since .bak never held the phrase to begin with"
    )
    # And the entry still resolves to the real phrase, proving it wasn't
    # just left present-but-corrupted.
    assert fake_keychain[v1_account] == TEST_MNEMONIC


def test_one_time_notice_printed_for_plaintext_source(isolated_home: Path, fake_keychain, capsys) -> None:
    _write_legacy_plaintext(isolated_home)

    auto_migrate.maybe_migrate()

    out = capsys.readouterr().out
    assert auto_migrate.ONE_TIME_NOTICE in out
    assert auto_migrate.ONE_TIME_NOTICE_KEYCHAIN_SOURCE not in out
    assert TEST_MNEMONIC not in out


def test_one_time_notice_printed_for_keychain_source_is_source_aware(
    isolated_home: Path, fake_keychain, capsys
) -> None:
    """2026-08-03 fix (major-2): a keychain-sourced migration must show
    the keychain-aware notice, not the plaintext-source one — the
    plaintext-source notice's claim that ".bak" holds the phrase is FALSE
    for this population (.bak here holds only the v1 marker)."""
    _write_legacy_v1_keychain(isolated_home, fake_keychain)

    auto_migrate.maybe_migrate()

    out = capsys.readouterr().out
    assert auto_migrate.ONE_TIME_NOTICE_KEYCHAIN_SOURCE in out
    assert auto_migrate.ONE_TIME_NOTICE not in out
    assert TEST_MNEMONIC not in out
    assert "keychain" in auto_migrate.ONE_TIME_NOTICE_KEYCHAIN_SOURCE.lower()


def test_read_back_verification_failure_reports_failure_not_success(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Minor fix (2026-08-03): a read-back verification failure must be
    reported as a FAILURE — no success notice, maybe_migrate() returns
    False — even though the v2 file was already written. Both .bak and
    (if applicable) the v1 keychain entry stay in place."""
    creds_path = _write_legacy_plaintext(isolated_home)

    from totalreclaw import bundle as bundle_module

    # parse_bundle_v1 is called twice in a clean run: once INTERNALLY by
    # derive_bundle_from_mnemonic (step 4 — must succeed, or migration
    # never reaches the write at all) and once explicitly for read-back
    # verification (step 8 — this is the call we want to fail). A
    # blanket patch breaks step 4 too (bundle.py's own
    # derive_bundle_from_mnemonic calls the module-level parse_bundle_v1
    # unqualified, so patching the module attribute affects it as well) —
    # only fail from the second call onward.
    real_parse_bundle_v1 = bundle_module.parse_bundle_v1
    call_count = {"n": 0}

    def _boom_after_first_call(json_str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_parse_bundle_v1(json_str)
        raise ValueError("simulated read-back corruption")

    monkeypatch.setattr(bundle_module, "parse_bundle_v1", _boom_after_first_call)

    migrated = auto_migrate.maybe_migrate()

    assert migrated is False
    out = capsys.readouterr().out
    assert auto_migrate.ONE_TIME_NOTICE not in out
    assert auto_migrate.ONE_TIME_NOTICE_KEYCHAIN_SOURCE not in out
    # The v2 file WAS written (step 7 succeeded before the read-back check
    # in step 8 failed) — but nothing claims success.
    v2 = json.loads(creds_path.read_text())
    assert v2.get("version") == 2
    bak_path = creds_path.parent / "credentials.json.bak"
    assert bak_path.exists()


def test_v2_file_created_with_0600_from_the_start_no_window(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor fix (2026-08-03): the tmp file (and therefore the final
    credentials.json, whose mode os.replace() inherits from it) must be
    created with mode 0600 via O_CREAT|O_WRONLY|O_TRUNC from the very
    first byte — no default-umask window before a later chmod."""
    import stat

    creds_path = _write_legacy_plaintext(isolated_home)

    real_open = os.open
    observed_modes: list[int] = []

    def _spy_open(path, flags, mode=0o777):
        if str(path).endswith(".tmp"):
            observed_modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _spy_open)

    migrated = auto_migrate.maybe_migrate()
    assert migrated is True

    assert observed_modes == [0o600], (
        f"tmp file must be os.open()'d with mode 0o600 from creation, got {observed_modes}"
    )
    actual_mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert actual_mode == 0o600


# ---------------------------------------------------------------------------
# Keychain-unavailable — see the module docstring's interpretation note
# ---------------------------------------------------------------------------


def test_source_keychain_unavailable_aborts_with_file_untouched(
    isolated_home: Path, fake_keychain
) -> None:
    """The EXISTING credential is v1-keychain-wrapped, but the entry is
    gone/locked — nothing to derive a bundle FROM. Must abort with the
    file completely untouched (no rename, no .bak, no v2 write)."""
    creds_path = _write_legacy_v1_keychain(isolated_home, fake_keychain)
    original = creds_path.read_text()
    fake_keychain.clear()  # simulate the entry being gone

    migrated = auto_migrate.maybe_migrate()

    assert migrated is False
    assert creds_path.read_text() == original
    assert not (creds_path.parent / "credentials.json.bak").exists()


def test_new_bundle_keychain_wrap_failure_falls_back_to_in_file_v2(
    isolated_home: Path,
) -> None:
    """No keychain backend at all for the NEW bundle's wrap step (headless
    host, kill-switch, or store error) — migration still proceeds; the v2
    file carries the full bundle plaintext instead of being
    keychain-wrapped. Matches derived-bundle-v1.md §4.3's documented
    headless storage mode and avoids permanently stranding
    TOTALRECLAW_NO_KEYCHAIN hosts on the legacy path."""
    creds_path = _write_legacy_plaintext(isolated_home)
    # No fake_keychain fixture here — TOTALRECLAW_NO_KEYCHAIN stays armed
    # (conftest's autouse _no_real_keychain fixture), so wrap_bundle
    # returns False for the real reason: kill-switch on / no backend.

    migrated = auto_migrate.maybe_migrate()

    assert migrated is True
    v2 = json.loads(creds_path.read_text())
    assert v2["version"] == 2
    assert v2.get("keychain_wrapped") is not True
    assert "vault" in v2
    assert "private_key" in v2["signing"]


# ---------------------------------------------------------------------------
# Atomic write failure between step 6 and 7 restores from .bak
# ---------------------------------------------------------------------------


def test_write_failure_between_backup_and_v2_write_restores_from_bak(
    isolated_home: Path, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds_path = _write_legacy_plaintext(isolated_home)
    original = creds_path.read_text()

    # The v2 file is now created via os.open(O_CREAT|O_WRONLY|O_TRUNC, 0o600)
    # (2026-08-03 fix — no 0644 window), not the builtins.open() the
    # previous revision of this module used — patch the actual mechanism.
    real_os_open = os.open

    def _boom_open(path, flags, mode=0o777):
        if str(path).endswith(".tmp"):
            raise OSError("simulated disk failure")
        return real_os_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _boom_open)

    migrated = auto_migrate.maybe_migrate()

    assert migrated is False
    # credentials.json must be restored — same content as before migration
    # started, not left as a renamed .bak with nothing in its place.
    assert creds_path.exists()
    assert creds_path.read_text() == original


# ---------------------------------------------------------------------------
# Lockfile handling
# ---------------------------------------------------------------------------


def test_lockfile_collision_is_handled(isolated_home: Path, fake_keychain) -> None:
    creds_path = _write_legacy_plaintext(isolated_home)
    original = creds_path.read_text()

    lock_path = isolated_home / ".totalreclaw" / ".migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("")  # fresh lock, held by "another process"

    migrated = auto_migrate.maybe_migrate()

    assert migrated is False
    assert creds_path.read_text() == original


def test_stale_lock_is_broken(isolated_home: Path, fake_keychain) -> None:
    _write_legacy_plaintext(isolated_home)

    lock_path = isolated_home / ".totalreclaw" / ".migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("")
    stale_time = time.time() - auto_migrate._STALE_LOCK_SECONDS - 60
    os.utime(lock_path, (stale_time, stale_time))

    migrated = auto_migrate.maybe_migrate()

    assert migrated is True  # the stale lock was broken and reacquired


def test_lock_released_after_migration(isolated_home: Path, fake_keychain) -> None:
    _write_legacy_plaintext(isolated_home)
    auto_migrate.maybe_migrate()

    lock_path = isolated_home / ".totalreclaw" / ".migration.lock"
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# The load-bearing test: a vault migrated from a phrase can still read
# facts written before migration (offline fixture, no network).
# ---------------------------------------------------------------------------


def test_migrated_vault_can_still_decrypt_pre_migration_facts(
    isolated_home: Path, fake_keychain
) -> None:
    from totalreclaw.bundle import parse_bundle_v1
    from totalreclaw.client import TotalReclaw
    from totalreclaw.crypto import decrypt, encrypt

    # A fact "written" (encrypted) before migration, under the
    # mnemonic-derived encryption key.
    pre_migration_client = TotalReclaw(mnemonic=TEST_MNEMONIC, relay_url="https://api-staging.totalreclaw.xyz")
    plaintext = "Pedro prefers dark mode and uses Vim keybindings."
    ciphertext = encrypt(plaintext, pre_migration_client._keys.encryption_key)

    creds_path = _write_legacy_plaintext(isolated_home)
    migrated = auto_migrate.maybe_migrate()
    assert migrated is True

    v2 = json.loads(creds_path.read_text())
    assert v2.get("keychain_wrapped") is True
    secret_json = cw.unwrap_bundle(v2["account"]["smart_account"])
    secret = json.loads(secret_json)
    full = {
        "version": v2["version"],
        "schema": v2["schema"],
        "vault": secret["vault"],
        "signing": secret["signing"],
        "account": v2["account"],
        "provisioned_at": v2["provisioned_at"],
        "provisioned_by": v2["provisioned_by"],
    }
    bundle = parse_bundle_v1(json.dumps(full))
    post_migration_client = TotalReclaw.from_bundle(bundle, server_url="https://api-staging.totalreclaw.xyz")

    decrypted = decrypt(ciphertext, post_migration_client._keys.encryption_key)
    assert decrypted == plaintext
