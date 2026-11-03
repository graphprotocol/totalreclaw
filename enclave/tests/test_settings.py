from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.support import prod_env, staging_env
from totalreclaw_enclave.settings import (
    DEV_PLACEHOLDER_HASH,
    GNOSIS_CHAIN_ID,
    PROD_DATA_EDGE,
    PROD_DERIVATION_ROOT,
    PROD_RELAY_URL,
    STAGING_DATA_EDGE,
    STAGING_RELAY_URL,
    Secret,
    Settings,
    SettingsError,
    assert_owner_eoa_permitted,
    load_settings,
)

STAGING_REQUIRED = (
    "ENCLAVE_PUBLIC_URL",
    "ENCLAVE_DB_PATH",
    "ENCLAVE_NEAR_CONTRACT",
    "ENCLAVE_MEASUREMENT_ID",
    "ENCLAVE_COMPOSE_HASH",
    "ENCLAVE_OS_IMAGE_HASH",
    "ENCLAVE_INFERENCE_POLICY_VERSION",
)


def test_enclave_env_is_required() -> None:
    with pytest.raises(SettingsError, match="ENCLAVE_ENV is required"):
        load_settings({})


def test_unknown_env_is_refused_without_echoing_it() -> None:
    with pytest.raises(SettingsError) as info:
        load_settings({"ENCLAVE_ENV": "production"})
    assert "production" not in str(info.value)


def test_dev_defaults() -> None:
    s = load_settings({"ENCLAVE_ENV": "dev"})
    assert s.env == "dev"
    assert s.relay_url == STAGING_RELAY_URL
    assert s.data_edge == STAGING_DATA_EDGE
    assert s.chain_id == GNOSIS_CHAIN_ID
    assert s.derivation_root == "totalreclaw-enclave-v1/dev"
    assert s.near_contract is None
    assert s.compose_hash == DEV_PLACEHOLDER_HASH
    assert s.os_image_hash == DEV_PLACEHOLDER_HASH
    assert s.measurement_id == "dev"
    assert s.inference_policy_version == "dev"
    assert s.public_url == "http://127.0.0.1:8080"
    assert s.bind_host == "127.0.0.1"
    assert s.bind_port == 8080
    assert s.db_path == Path(".enclave-dev/enclave.sqlite3")
    assert s.allow_owner_eoa is False
    assert s.transparency_url == "https://totalreclaw.xyz/enclave"


def test_settings_are_frozen() -> None:
    s = load_settings({"ENCLAVE_ENV": "dev"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.env = "prod"  # type: ignore[misc]


def test_staging_full_config(tmp_path: Path) -> None:
    s = load_settings(staging_env(tmp_path, ENCLAVE_COMPOSE_HASH="C" * 64))
    assert s.env == "staging"
    assert s.public_url == "https://enclave-staging.totalreclaw.xyz"
    assert s.relay_url == STAGING_RELAY_URL
    assert s.data_edge == STAGING_DATA_EDGE
    assert s.derivation_root == "totalreclaw-enclave-v1/staging"
    assert s.near_contract == "enclave-staging.totalreclaw.testnet"
    assert s.compose_hash == "c" * 64  # lowercased


@pytest.mark.parametrize("missing", STAGING_REQUIRED)
def test_staging_requires_each_variable(tmp_path: Path, missing: str) -> None:
    env = staging_env(tmp_path)
    del env[missing]
    with pytest.raises(SettingsError, match=missing):
        load_settings(env)


def test_prod_uses_prod_relay_and_data_edge(tmp_path: Path) -> None:
    s = load_settings(prod_env(tmp_path))
    assert s.relay_url == PROD_RELAY_URL
    assert s.data_edge == PROD_DATA_EDGE
    assert s.derivation_root == PROD_DERIVATION_ROOT


@pytest.mark.parametrize("flag", ["1", " 1 ", "1\n"])
def test_prod_refuses_owner_eoa_flag(tmp_path: Path, flag: str) -> None:
    with pytest.raises(SettingsError, match="ENCLAVE_ALLOW_OWNER_EOA is refused when ENCLAVE_ENV=prod"):
        load_settings(prod_env(tmp_path, ENCLAVE_ALLOW_OWNER_EOA=flag))


def test_dev_and_staging_allow_owner_eoa_flag(tmp_path: Path) -> None:
    assert load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_ALLOW_OWNER_EOA": "1"}).allow_owner_eoa is True
    assert load_settings(staging_env(tmp_path, ENCLAVE_ALLOW_OWNER_EOA="1")).allow_owner_eoa is True


def test_replace_cannot_bypass_the_owner_eoa_refusal(tmp_path: Path) -> None:
    # ENC-11 swaps attested values in with dataclasses.replace() after boot;
    # every construction re-runs the refusal.
    dev = load_settings({"ENCLAVE_ENV": "dev"})
    with pytest.raises(SettingsError, match="refused when ENCLAVE_ENV=prod"):
        dataclasses.replace(dev, env="prod", allow_owner_eoa=True)
    with pytest.raises(SettingsError, match="refused when ENCLAVE_ENV=prod"):
        dataclasses.replace(load_settings(prod_env(tmp_path)), allow_owner_eoa=True)
    staging = load_settings(staging_env(tmp_path, ENCLAVE_ALLOW_OWNER_EOA="1"))
    with pytest.raises(SettingsError, match="production derivation root"):
        dataclasses.replace(staging, derivation_root=PROD_DERIVATION_ROOT)
    assert dataclasses.replace(staging, measurement_id="m-2").allow_owner_eoa is True


def test_replace_cannot_sneak_in_an_unknown_env(dev_settings: Settings) -> None:
    # load_settings refuses a bad ENCLAVE_ENV, but the owner-EOA refusal only
    # compares env == "prod": replace(s, env="PROD") would sidestep it.
    with pytest.raises(SettingsError, match="must be one of dev, staging, prod"):
        dataclasses.replace(dev_settings, env="PROD")


@pytest.mark.parametrize(
    ("root", "error"),
    [
        ("totalreclaw-enclave-v1/staging", "ENCLAVE_DERIVATION_ROOT does not match"),
        ("totalreclaw-enclave-v1/dev", "ENCLAVE_DERIVATION_ROOT does not match"),
        (" totalreclaw-enclave-v1/prod ", "ENCLAVE_ALLOW_OWNER_EOA is refused when ENCLAVE_ENV=prod"),
    ],
)
def test_prod_root_override_cannot_enable_owner_eoa(tmp_path: Path, root: str, error: str) -> None:
    with pytest.raises(SettingsError, match=error):
        load_settings(prod_env(tmp_path, ENCLAVE_DERIVATION_ROOT=root, ENCLAVE_ALLOW_OWNER_EOA="1"))


def test_load_settings_reads_os_environ_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in prod_env(tmp_path, ENCLAVE_ALLOW_OWNER_EOA="1").items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match="refused when ENCLAVE_ENV=prod"):
        load_settings()
    monkeypatch.setenv("ENCLAVE_ALLOW_OWNER_EOA", "0")
    s = load_settings()
    assert (s.env, s.allow_owner_eoa, s.derivation_root) == ("prod", False, PROD_DERIVATION_ROOT)


def test_owner_eoa_refused_on_prod_root_read_from_the_gate(tmp_path: Path) -> None:
    # ENC-11 re-checks with the derivation_root read from the NEAR gate contract.
    s = load_settings(staging_env(tmp_path, ENCLAVE_ALLOW_OWNER_EOA="1"))
    with pytest.raises(SettingsError, match="production derivation root"):
        assert_owner_eoa_permitted(s, PROD_DERIVATION_ROOT)
    assert_owner_eoa_permitted(s, "totalreclaw-enclave-v1/staging")


@pytest.mark.parametrize("value", ["true", "yes", "2", "on"])
def test_owner_eoa_flag_must_be_0_or_1(value: str) -> None:
    with pytest.raises(SettingsError, match="must be 0 or 1"):
        load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_ALLOW_OWNER_EOA": value})


def test_derivation_root_override_must_match_env(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="ENCLAVE_DERIVATION_ROOT"):
        load_settings(staging_env(tmp_path, ENCLAVE_DERIVATION_ROOT=PROD_DERIVATION_ROOT))
    s = load_settings(staging_env(tmp_path, ENCLAVE_DERIVATION_ROOT="totalreclaw-enclave-v1/staging"))
    assert s.derivation_root == "totalreclaw-enclave-v1/staging"


def test_relay_override_only_in_dev(tmp_path: Path) -> None:
    s = load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_RELAY_URL": "http://127.0.0.1:9999/"})
    assert s.relay_url == "http://127.0.0.1:9999"
    with pytest.raises(SettingsError, match="only be overridden when ENCLAVE_ENV=dev"):
        load_settings(staging_env(tmp_path, ENCLAVE_RELAY_URL="http://127.0.0.1:9999"))


@pytest.mark.parametrize("url", [PROD_RELAY_URL, PROD_RELAY_URL + "/", "https://api.totalreclaw.xyz/v1"])
def test_dev_refuses_the_production_relay(url: str) -> None:
    with pytest.raises(SettingsError, match="production relay is refused"):
        load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_RELAY_URL": url})


def test_near_contract_network_matches_env(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match=".testnet"):
        load_settings(staging_env(tmp_path, ENCLAVE_NEAR_CONTRACT="enclave.totalreclaw.near"))
    with pytest.raises(SettingsError, match=".near account"):
        load_settings(prod_env(tmp_path, ENCLAVE_NEAR_CONTRACT="enclave-staging.totalreclaw.testnet"))
    with pytest.raises(SettingsError, match="NEAR account id"):
        load_settings(staging_env(tmp_path, ENCLAVE_NEAR_CONTRACT="Bad Account.testnet"))


def test_dev_refuses_a_near_contract() -> None:
    with pytest.raises(SettingsError, match="must be unset when ENCLAVE_ENV=dev"):
        load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_NEAR_CONTRACT": "enclave-staging.totalreclaw.testnet"})


def test_prod_refuses_debug_logging(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="DEBUG is refused"):
        load_settings(prod_env(tmp_path, ENCLAVE_LOG_LEVEL="debug"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"ENCLAVE_PUBLIC_URL": "http://enclave-staging.totalreclaw.xyz"},
        {"ENCLAVE_PUBLIC_URL": "https://enclave-staging.totalreclaw.xyz/mcp"},
        {"ENCLAVE_COMPOSE_HASH": "abc"},
        {"ENCLAVE_OS_IMAGE_HASH": "z" * 64},
        {"ENCLAVE_MEASUREMENT_ID": "Has Spaces"},
        {"ENCLAVE_PORT": "70000"},
        {"ENCLAVE_TRANSPARENCY_URL": "http://totalreclaw.xyz/enclave"},
    ],
)
def test_malformed_values_are_refused(tmp_path: Path, overrides: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        load_settings(staging_env(tmp_path, **overrides))


@pytest.mark.parametrize(
    "url",
    [
        "https://enclave@enclave-staging.totalreclaw.xyz",
        "https://enclave:secret@enclave-staging.totalreclaw.xyz",
    ],
)
def test_public_url_userinfo_is_refused(tmp_path: Path, url: str) -> None:
    # https://host@evil.example must not become the OAuth issuer (ENC-5).
    with pytest.raises(SettingsError, match="username or password"):
        load_settings(staging_env(tmp_path, ENCLAVE_PUBLIC_URL=url))


@pytest.mark.parametrize("url", ["https://", "https://:8443", "enclave-staging.totalreclaw.xyz"])
def test_public_url_without_a_host_is_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(SettingsError, match="hostname"):
        load_settings(staging_env(tmp_path, ENCLAVE_PUBLIC_URL=url))


def test_public_url_host_is_normalised_to_lowercase(tmp_path: Path) -> None:
    s = load_settings(staging_env(tmp_path, ENCLAVE_PUBLIC_URL="https://ENCLAVE-Staging.TotalReclaw.XYZ"))
    assert s.public_url == "https://enclave-staging.totalreclaw.xyz"
    s = load_settings(staging_env(tmp_path, ENCLAVE_PUBLIC_URL="https://Enclave-Staging.TotalClaw.XYZ:8443"))
    assert s.public_url == "https://enclave-staging.totalclaw.xyz:8443"


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_dev_public_url_accepts_http_on_loopback_hosts(url: str) -> None:
    assert load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_PUBLIC_URL": url}).public_url == url


@pytest.mark.parametrize("url", ["http://enclave-dev.totalreclaw.xyz", "http://192.168.1.10:8080"])
def test_dev_public_url_refuses_http_off_loopback(url: str) -> None:
    with pytest.raises(SettingsError, match="must be an https origin"):
        load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_PUBLIC_URL": url})


@pytest.mark.parametrize(
    "url",
    ["https://user@totalreclaw.xyz/enclave", "https://user:pw@totalreclaw.xyz/enclave"],
)
def test_transparency_url_userinfo_is_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(SettingsError, match="username or password"):
        load_settings(staging_env(tmp_path, ENCLAVE_TRANSPARENCY_URL=url))


def test_transparency_url_without_a_host_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="hostname"):
        load_settings(staging_env(tmp_path, ENCLAVE_TRANSPARENCY_URL="https:///enclave"))


def test_errors_never_echo_values(tmp_path: Path) -> None:
    sentinel = "SENTINELVALUE"
    cases = [
        {"ENCLAVE_ENV": sentinel},
        staging_env(tmp_path, ENCLAVE_PUBLIC_URL=f"http://{sentinel}.example"),
        staging_env(tmp_path, ENCLAVE_COMPOSE_HASH=sentinel),
        staging_env(tmp_path, ENCLAVE_NEAR_CONTRACT=f"{sentinel}.near"),
        {"ENCLAVE_ENV": "dev", "ENCLAVE_ALLOW_OWNER_EOA": sentinel},
    ]
    for env in cases:
        with pytest.raises(SettingsError) as info:
            load_settings(env)
        assert sentinel not in str(info.value)
        assert sentinel.lower() not in str(info.value)


def test_secret_never_reveals_itself_in_repr() -> None:
    secret = Secret("s3cr3t-value")
    assert "s3cr3t" not in repr(secret)
    assert "s3cr3t" not in str(secret)
    assert "s3cr3t" not in f"{secret}"
    assert secret.reveal() == "s3cr3t-value"


@pytest.mark.parametrize("value", ["PROD", "Prod", "development", "stage"])
def test_env_value_is_exact_lowercase(value: str) -> None:
    with pytest.raises(SettingsError, match="must be one of dev, staging, prod"):
        load_settings({"ENCLAVE_ENV": value})


def test_surrounding_whitespace_is_ignored() -> None:
    assert load_settings({"ENCLAVE_ENV": " dev \n"}).env == "dev"
