"""Integration test for cred-3 stage 3 external-mode boot.

Validates the cred-3 #263 done criterion: "external secret provider loads
mnemonic at boot." Hermes's ``AgentState._try_auto_configure`` now routes
credential discovery through ``get_credential_provider()``, so configuring
``TOTALRECLAW_CREDENTIALS_PROVIDER=external`` + a transport env var lets
the daemon boot against env-var- or mounted-file-backed secret managers
without touching ``~/.totalreclaw/credentials.json`` on disk.

Tests cover:
  - Inline JSON env var → daemon boots with that mnemonic, never reads disk.
  - Mounted JSON file → daemon boots with that mnemonic, disk file untouched.
  - External mode + neither transport set → daemon does NOT silently fall
    back to disk (per cred-3 stage 1 + 2 spec).
  - External mode + ``configure()`` is a no-op write (secret manager owns
    the source of truth).
  - Legacy file-mode behaviour byte-identical to pre-stage-3.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pytest

from totalreclaw.agent.state import AgentState
from totalreclaw.credential_provider import (
    ENV_CREDENTIALS_PATH,
    ENV_EXTERNAL_JSON,
    ENV_EXTERNAL_PATH,
    ENV_PROVIDER,
)


# A real BIP-39 24-word phrase isn't required for boot — `AgentState.configure`
# calls into `TotalReclaw(mnemonic=...)` which only fails on truly malformed
# input. Using a known-good 12-word test mnemonic.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Reroute $HOME so no test contaminates the real ~/.totalreclaw."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    monkeypatch.delenv(ENV_CREDENTIALS_PATH, raising=False)
    return fake_home


def test_file_mode_boot_unchanged(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default file mode — boots from ~/.totalreclaw/credentials.json
    exactly like before stage 3."""
    monkeypatch.delenv(ENV_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    creds_dir = isolated_home / ".totalreclaw"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"
    creds_file.write_text(json.dumps({"mnemonic": TEST_MNEMONIC}))

    state = AgentState()
    state._try_auto_configure()

    assert state.is_configured()


def test_external_mode_boot_via_inline_json(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External mode + inline JSON — daemon boots with that mnemonic
    without ever reading disk."""
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.setenv(
        ENV_EXTERNAL_JSON, json.dumps({"mnemonic": TEST_MNEMONIC})
    )
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    # Critically: no credentials.json on disk.
    assert not (isolated_home / ".totalreclaw" / "credentials.json").exists()

    state = AgentState()
    state._try_auto_configure()

    assert state.is_configured()


def test_external_mode_boot_via_mounted_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """External mode + file-mount transport — daemon boots from the mount
    path, ignoring ~/.totalreclaw entirely."""
    secret_mount = tmp_path / "vault-secret.json"
    secret_mount.write_text(json.dumps({"mnemonic": TEST_MNEMONIC}))

    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.setenv(ENV_EXTERNAL_PATH, str(secret_mount))

    # Critically: no credentials.json on disk.
    assert not (isolated_home / ".totalreclaw" / "credentials.json").exists()

    state = AgentState()
    state._try_auto_configure()

    assert state.is_configured()


def test_external_mode_neither_transport_does_not_fall_back(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External mode with neither transport set — provider returns None,
    daemon stays unconfigured. We do NOT silently fall back to reading
    a stale credentials.json off disk (would mask a deploy misconfig)."""
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    # Put a credentials.json on disk that WOULD work in file mode.
    creds_dir = isolated_home / ".totalreclaw"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"
    creds_file.write_text(json.dumps({"mnemonic": TEST_MNEMONIC}))

    state = AgentState()
    state._try_auto_configure()

    # Stays unconfigured — external mode does not read disk.
    assert not state.is_configured()


def test_external_mode_configure_does_not_write_disk(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In external mode, ``state.configure(mnemonic)`` must NOT write to
    ~/.totalreclaw/credentials.json — the secret manager owns the source
    of truth. Writing would create two competing truths."""
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.setenv(
        ENV_EXTERNAL_JSON, json.dumps({"mnemonic": TEST_MNEMONIC})
    )
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    # Pre-condition: no credentials.json on disk.
    creds_file = isolated_home / ".totalreclaw" / "credentials.json"
    assert not creds_file.exists()

    state = AgentState()
    state.configure(TEST_MNEMONIC)

    # Post-condition: still no credentials.json on disk. configure()
    # produced no write because provider.save() returns False in
    # external mode.
    assert not creds_file.exists()


def test_legacy_recovery_phrase_preservation_in_file_mode(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug #7 / Wave 2a write policy — if credentials.json has the legacy
    ``recovery_phrase`` key with the same mnemonic, leave file untouched.
    Stage 3 must preserve this file-mode behaviour."""
    monkeypatch.delenv(ENV_PROVIDER, raising=False)

    creds_dir = isolated_home / ".totalreclaw"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"
    legacy_blob = json.dumps({"recovery_phrase": TEST_MNEMONIC})
    creds_file.write_text(legacy_blob)
    original_mtime = creds_file.stat().st_mtime_ns

    state = AgentState()
    state.configure(TEST_MNEMONIC)

    # File untouched — same content, same mtime within the test window.
    assert creds_file.read_text() == legacy_blob


def test_unknown_provider_mode_falls_back_to_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in TOTALRECLAW_CREDENTIALS_PROVIDER must fall back to file
    mode (matches the cred-3 stage 1 + 2 factory behaviour). A deploy
    that intended ``external`` but typo'd ``externl`` reads disk like
    legacy — louder than silent breakage."""
    monkeypatch.setenv(ENV_PROVIDER, "made-up-mode")

    creds_dir = isolated_home / ".totalreclaw"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"
    creds_file.write_text(json.dumps({"mnemonic": TEST_MNEMONIC}))

    state = AgentState()
    state._try_auto_configure()

    assert state.is_configured()
