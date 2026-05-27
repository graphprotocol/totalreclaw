"""Unit tests for ``totalreclaw.credential_provider`` (cred-3 stage 2).

Mirrors the test surface of ``skill/plugin/credential-provider.test.ts``
(cred-3 stage 1, merged in p-diogo/totalreclaw#271) so the Python and TS
sides have matching coverage. Tests cover:

  - file-mode round-trip + 0o600 preservation
  - external-mode boot load via inline JSON
  - external-mode boot load via mounted JSON file
  - inline JSON wins when both transports set
  - read-only enforcement on external mode
  - corrupt / missing / unset → ``None``
  - factory mode switch
  - factory honours explicit args over env vars

The "external secret provider loads credentials at boot" done criterion
from cred-3 #263 is the load-mode tests below.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from totalreclaw.credential_provider import (
    ENV_CREDENTIALS_PATH,
    ENV_EXTERNAL_JSON,
    ENV_EXTERNAL_PATH,
    ENV_PROVIDER,
    ExternalCredentialProvider,
    FileCredentialProvider,
    get_credential_provider,
)


# ---------------------------------------------------------------------------
# FileCredentialProvider — round trip + edge cases
# ---------------------------------------------------------------------------


def test_file_provider_save_then_load_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "credentials.json"
    provider = FileCredentialProvider(credentials_path=p)

    creds = {"mnemonic": "abandon " * 11 + "about", "scope_address": "0xabc"}
    assert provider.save(creds) is True

    loaded = provider.load()
    assert loaded == creds


def test_file_provider_save_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / "credentials.json"
    provider = FileCredentialProvider(credentials_path=p)

    assert provider.save({"mnemonic": "x"}) is True
    assert p.exists()


def test_file_provider_save_preserves_0o600_mode(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod 0o600 is best-effort on Windows")

    p = tmp_path / "credentials.json"
    provider = FileCredentialProvider(credentials_path=p)
    assert provider.save({"mnemonic": "x"}) is True

    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_file_provider_load_missing_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "nope.json"
    provider = FileCredentialProvider(credentials_path=p)
    assert provider.load() is None


def test_file_provider_load_empty_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "credentials.json"
    p.write_text("")
    provider = FileCredentialProvider(credentials_path=p)
    assert provider.load() is None


def test_file_provider_load_malformed_json_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "credentials.json"
    p.write_text("{not valid json")
    provider = FileCredentialProvider(credentials_path=p)
    assert provider.load() is None


def test_file_provider_load_non_object_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps(["a", "b"]))
    provider = FileCredentialProvider(credentials_path=p)
    assert provider.load() is None


def test_file_provider_mode_is_file(tmp_path: Path) -> None:
    provider = FileCredentialProvider(credentials_path=tmp_path / "x.json")
    assert provider.mode == "file"


def test_file_provider_clear_removes_file(tmp_path: Path) -> None:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"mnemonic": "x"}))
    provider = FileCredentialProvider(credentials_path=p)

    assert provider.clear() is True
    assert not p.exists()


def test_file_provider_clear_missing_file_succeeds(tmp_path: Path) -> None:
    p = tmp_path / "never-existed.json"
    provider = FileCredentialProvider(credentials_path=p)
    # Path.unlink(missing_ok=True) keeps this a no-op success.
    assert provider.clear() is True


# ---------------------------------------------------------------------------
# ExternalCredentialProvider — read paths
# ---------------------------------------------------------------------------


def test_external_provider_load_via_inline_json() -> None:
    creds = {"mnemonic": "x" * 12, "smart_account": "0xabc"}
    provider = ExternalCredentialProvider(
        inline_json=json.dumps(creds), file_path=None
    )

    loaded = provider.load()
    assert loaded == creds


def test_external_provider_load_via_file_mount(tmp_path: Path) -> None:
    creds = {"mnemonic": "y" * 12, "schema": "session-key-v1"}
    p = tmp_path / "mounted-secret.json"
    p.write_text(json.dumps(creds))

    provider = ExternalCredentialProvider(inline_json=None, file_path=p)

    loaded = provider.load()
    assert loaded == creds


def test_external_provider_inline_json_wins_over_file_path(tmp_path: Path) -> None:
    inline = {"source": "inline"}
    on_disk = {"source": "file"}
    p = tmp_path / "mounted-secret.json"
    p.write_text(json.dumps(on_disk))

    provider = ExternalCredentialProvider(
        inline_json=json.dumps(inline), file_path=p
    )

    loaded = provider.load()
    assert loaded == inline


def test_external_provider_neither_transport_set_returns_none() -> None:
    provider = ExternalCredentialProvider(inline_json=None, file_path=None)
    assert provider.load() is None


def test_external_provider_missing_file_path_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "does-not-exist.json"
    provider = ExternalCredentialProvider(inline_json=None, file_path=p)
    assert provider.load() is None


def test_external_provider_malformed_inline_json_returns_none() -> None:
    provider = ExternalCredentialProvider(
        inline_json="{not valid", file_path=None
    )
    assert provider.load() is None


def test_external_provider_non_object_inline_json_returns_none() -> None:
    provider = ExternalCredentialProvider(
        inline_json=json.dumps([1, 2, 3]), file_path=None
    )
    assert provider.load() is None


# ---------------------------------------------------------------------------
# ExternalCredentialProvider — read-only enforcement
# ---------------------------------------------------------------------------


def test_external_provider_save_returns_false() -> None:
    provider = ExternalCredentialProvider(
        inline_json=json.dumps({"mnemonic": "x"}), file_path=None
    )
    assert provider.save({"mnemonic": "y"}) is False


def test_external_provider_clear_returns_false() -> None:
    provider = ExternalCredentialProvider(
        inline_json=json.dumps({"mnemonic": "x"}), file_path=None
    )
    assert provider.clear() is False


def test_external_provider_mode_is_external() -> None:
    provider = ExternalCredentialProvider(inline_json=None, file_path=None)
    assert provider.mode == "external"


# ---------------------------------------------------------------------------
# get_credential_provider factory
# ---------------------------------------------------------------------------


def test_factory_default_returns_file_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_PROVIDER, raising=False)
    monkeypatch.setenv(ENV_CREDENTIALS_PATH, str(tmp_path / "credentials.json"))

    provider = get_credential_provider()
    assert isinstance(provider, FileCredentialProvider)
    assert provider.credentials_path == tmp_path / "credentials.json"


def test_factory_unknown_mode_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "made-up-mode")

    provider = get_credential_provider()
    assert isinstance(provider, FileCredentialProvider)


def test_factory_external_mode_with_inline_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = {"mnemonic": "x" * 12}
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.setenv(ENV_EXTERNAL_JSON, json.dumps(creds))
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    provider = get_credential_provider()
    assert isinstance(provider, ExternalCredentialProvider)
    assert provider.load() == creds


def test_factory_external_mode_with_file_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creds = {"mnemonic": "y" * 12}
    p = tmp_path / "vault-mount.json"
    p.write_text(json.dumps(creds))

    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.setenv(ENV_EXTERNAL_PATH, str(p))

    provider = get_credential_provider()
    assert isinstance(provider, ExternalCredentialProvider)
    assert provider.load() == creds


def test_factory_external_mode_neither_transport_returns_provider_with_none_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.delenv(ENV_EXTERNAL_JSON, raising=False)
    monkeypatch.delenv(ENV_EXTERNAL_PATH, raising=False)

    provider = get_credential_provider()
    # We DO NOT silently fall back to file mode — caller must observe
    # the misconfiguration (matches TS stage-1 behaviour).
    assert isinstance(provider, ExternalCredentialProvider)
    assert provider.load() is None


def test_factory_explicit_args_override_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "external")
    monkeypatch.setenv(ENV_EXTERNAL_JSON, json.dumps({"source": "env"}))

    explicit_creds = {"source": "explicit-arg"}
    provider = get_credential_provider(
        provider_mode="external",
        inline_json=json.dumps(explicit_creds),
    )

    assert isinstance(provider, ExternalCredentialProvider)
    assert provider.load() == explicit_creds


def test_factory_explicit_file_path_override(tmp_path: Path) -> None:
    explicit_path = tmp_path / "custom-credentials.json"
    explicit_path.write_text(json.dumps({"mnemonic": "z"}))

    provider = get_credential_provider(
        provider_mode="file", credentials_path=explicit_path
    )

    assert isinstance(provider, FileCredentialProvider)
    assert provider.load() == {"mnemonic": "z"}


def test_factory_file_mode_default_path_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv(ENV_CREDENTIALS_PATH, raising=False)
    monkeypatch.delenv(ENV_PROVIDER, raising=False)

    provider = get_credential_provider()
    assert isinstance(provider, FileCredentialProvider)
    assert provider.credentials_path == fake_home / ".totalreclaw" / "credentials.json"
