"""Credential-safety fixes: atomic write + safe replace rotation (#674),
in-process reload after a replace (#618), doctor on bundle creds (#608).

All paths live under the per-test temp HOME (tests/conftest.py). Dummy
mnemonics are the public BIP-39 test vectors.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from totalreclaw import credential_provider as cp
from totalreclaw.hermes import auto_migrate as am

M1 = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)
M2 = (
    "legal winner thank year wave sausage worth useful legal winner thank yellow"
)


@pytest.fixture()
def creds_path(tmp_path, monkeypatch):
    d = tmp_path / "tr"
    d.mkdir()
    p = d / "credentials.json"
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(p))
    monkeypatch.delenv("TOTALRECLAW_CREDENTIALS_PROVIDER", raising=False)
    return p


# --- #674: atomic write ---------------------------------------------------


def test_save_is_atomic_mode_0600_and_leaves_no_temp(creds_path):
    provider = cp.get_credential_provider()
    assert provider.save({"mnemonic": "old"})
    assert provider.save({"mnemonic": "new"})
    assert json.loads(creds_path.read_text()) == {"mnemonic": "new"}
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
    assert [f.name for f in creds_path.parent.iterdir()] == ["credentials.json"]


@pytest.mark.parametrize("fail_at", ["fsync", "replace"])
def test_simulated_crash_leaves_valid_old_file(creds_path, monkeypatch, fail_at):
    provider = cp.get_credential_provider()
    provider.save({"mnemonic": "old"})

    def boom(*a, **k):
        raise OSError("simulated crash")

    monkeypatch.setattr(cp.os, fail_at, boom)
    assert provider.save({"mnemonic": "new"}) is False
    monkeypatch.undo()
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds_path))
    # Old file intact and parseable; temp cleaned up.
    assert json.loads(creds_path.read_text()) == {"mnemonic": "old"}
    assert [f.name for f in creds_path.parent.iterdir()] == ["credentials.json"]


def test_save_replaces_symlink_instead_of_writing_through(creds_path, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"keep": true}')
    creds_path.symlink_to(target)
    assert cp.get_credential_provider().save({"mnemonic": "new"})
    assert not creds_path.is_symlink()
    assert json.loads(target.read_text()) == {"keep": True}


# --- #674: replace rotation ----------------------------------------------


def test_preserve_only_copies_and_never_moves_bak(creds_path):
    creds_path.write_text('{"mnemonic": "old"}')
    bak = creds_path.parent / "credentials.json.bak"
    bak.write_text('{"mnemonic": "older"}')

    kept = am.preserve_credentials_before_replace()

    assert kept is not None and kept.copy_path.read_text() == '{"mnemonic": "old"}'
    assert stat.S_IMODE(kept.copy_path.stat().st_mode) == 0o600
    assert bak.exists(), ".bak must stay put until the new write succeeded"
    assert creds_path.read_text() == '{"mnemonic": "old"}'


def test_finish_rotates_bak_after_success(creds_path):
    creds_path.write_text('{"mnemonic": "old"}')
    bak = creds_path.parent / "credentials.json.bak"
    bak.write_text('{"mnemonic": "older"}')
    kept = am.preserve_credentials_before_replace()
    am.finish_credentials_replace(kept)
    assert not bak.exists()
    rotated = list(creds_path.parent.glob("credentials.json.bak.replaced-*"))
    assert len(rotated) == 1 and rotated[0].read_text() == '{"mnemonic": "older"}'


def test_abort_removes_copy_only_when_file_unchanged(creds_path):
    creds_path.write_text('{"mnemonic": "old"}')
    kept = am.preserve_credentials_before_replace()
    am.abort_credentials_replace(kept)
    assert not kept.copy_path.exists()

    kept = am.preserve_credentials_before_replace()
    creds_path.write_text('{"mnemonic": "changed"}')
    am.abort_credentials_replace(kept)
    assert kept.copy_path.exists(), "copy is the only record of the old file"


def test_two_preserves_never_overwrite_each_other(creds_path):
    creds_path.write_text('{"mnemonic": "old"}')
    a = am.preserve_credentials_before_replace()
    b = am.preserve_credentials_before_replace()
    assert a.copy_path != b.copy_path and a.copy_path.exists() and b.copy_path.exists()


def test_failed_configure_leaves_everything_in_place(creds_path, monkeypatch):
    """The Hermes replace sequence with a crash inside the write: the old
    file and .bak are untouched and the stray copy is removed."""
    from totalreclaw.agent.state import AgentState

    creds_path.write_text(json.dumps({"mnemonic": M2}))
    bak = creds_path.parent / "credentials.json.bak"
    bak.write_text('{"mnemonic": "older"}')
    monkeypatch.setattr(cp.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))

    kept = am.preserve_credentials_before_replace()
    state = AgentState()
    state.configure(M1)  # provider.save fails softly (returns False)
    monkeypatch.undo()
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds_path))
    am.abort_credentials_replace(kept)

    assert json.loads(creds_path.read_text()) == {"mnemonic": M2}
    assert bak.exists()
    assert list(creds_path.parent.glob("*.replaced-*")) == []


# --- #618: reload after replace ------------------------------------------


def test_running_state_reloads_after_credentials_replaced_on_disk(creds_path):
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M1)
    first = state.get_client().eoa_address

    # Another process (the pair sidecar) replaces the account.
    cp.get_credential_provider().save({"mnemonic": M2})

    second = state.get_client().eoa_address
    assert second != first


def test_no_reload_when_nothing_changed(creds_path):
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M1)
    c = state.get_client()
    assert state.get_client() is c


def test_reload_failure_keeps_previous_client(creds_path):
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M1)
    c = state.get_client()
    creds_path.write_text('{"version": 99}')  # unknown version -> loud error
    assert state.get_client() is c


# --- #608: doctor on bundle credentials ----------------------------------


def test_doctor_accepts_bundle_credentials(tmp_path, capsys):
    from totalreclaw import cli as tr_cli

    p = tmp_path / "credentials.json"
    sa = "0x" + "e" * 40
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "schema": "derived-bundle-v1",
                "account": {"smart_account": sa, "chain_id": 100},
                "keychain_wrapped": True,
                "signing": {"kind": "eoa", "address": "0x" + "d" * 40},
            }
        )
    )
    tr_cli.run_doctor(credentials_path=p, relay_url="https://invalid.invalid")
    out = capsys.readouterr().out
    assert "no mnemonic field" not in out
    assert "derived bundle" in out
    assert sa in out


def test_doctor_still_fails_on_missing_mnemonic_in_legacy_file(tmp_path, capsys):
    from totalreclaw import cli as tr_cli

    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"scope_address": "0x" + "f" * 40}))
    tr_cli.run_doctor(credentials_path=p, relay_url="https://invalid.invalid")
    assert "no mnemonic field" in capsys.readouterr().out


def test_doctor_warns_when_bundle_has_no_smart_account(tmp_path, capsys):
    from totalreclaw import cli as tr_cli

    p = tmp_path / "credentials.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "schema": "derived-bundle-v1",
                "account": {"chain_id": 100},
                "keychain_wrapped": True,
                "signing": {"kind": "eoa", "address": "0x" + "d" * 40},
            }
        )
    )
    tr_cli.run_doctor(credentials_path=p, relay_url="https://invalid.invalid")
    out = capsys.readouterr().out
    assert "no account.smart_account" in out


# --- review fixes: failed save stops the switch, mode, read-only reload ----


def _break_replace(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cp.os, "replace", boom)


def test_replace_with_failed_save_does_not_switch_account(creds_path, monkeypatch):
    """The replace sequence used by the pair tool and the sidecar: a failed
    credentials.json write raises, the in-memory client keeps the old
    account, the old file and .bak stay, and the stray copy is removed. The
    raise makes the handler return state=error, which nacks the relay."""
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M2)
    old_eoa = state.get_client().eoa_address
    bak = creds_path.parent / "credentials.json.bak"
    bak.write_text('{"mnemonic": "older"}')
    before = creds_path.read_text()

    kept = am.preserve_credentials_before_replace()
    _break_replace(monkeypatch)
    with pytest.raises(RuntimeError):
        state.configure(M1, require_save=True)
    monkeypatch.undo()
    monkeypatch.setenv("TOTALRECLAW_CREDENTIALS_PATH", str(creds_path))
    am.abort_credentials_replace(kept)

    assert state.get_client().eoa_address == old_eoa
    assert creds_path.read_text() == before
    assert bak.exists()
    assert list(creds_path.parent.glob("*.replaced-*")) == []


def test_replace_with_good_save_switches_account(creds_path):
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M2)
    old_eoa = state.get_client().eoa_address
    state.configure(M1, require_save=True)
    assert state.get_client().eoa_address != old_eoa
    assert cp.get_credential_provider().load()


@pytest.mark.parametrize("mask", [0o077, 0o000, 0o277])
def test_credentials_file_mode_is_0600_under_any_umask(creds_path, mask):
    old = os.umask(mask)
    try:
        assert cp.get_credential_provider().save({"mnemonic": "x"})
        assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
        creds_path.write_text('{"mnemonic": "old"}')
        kept = am.preserve_credentials_before_replace()
        assert stat.S_IMODE(kept.copy_path.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_reload_does_not_write_credentials(creds_path):
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    state.configure(M1)
    first = state.get_client().eoa_address
    # Another process replaces the file in a plain (unwrapped) legacy shape.
    cp.get_credential_provider().save({"recovery_phrase": M2})
    st = creds_path.stat()
    before = (st.st_ino, st.st_mtime_ns, creds_path.read_text())

    assert state.get_client().eoa_address != first  # reloaded
    st = creds_path.stat()
    assert (st.st_ino, st.st_mtime_ns, creds_path.read_text()) == before
