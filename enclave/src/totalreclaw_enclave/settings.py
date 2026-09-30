"""Boot configuration: environment variables -> one frozen ``Settings``.

Rules (spec §3.1, §3.3, §8, §9; enclave brief):

* ``ENCLAVE_ENV`` is required and is one of ``dev``, ``staging``, ``prod``.
  There is no default: a service that silently fell back to ``dev`` (the
  deterministic test root) in production would be a key-custody failure.
* Relay URL, DataEdge, chain and CKD derivation root are fixed per
  environment. Only ``dev`` may point at a different relay (a local fake).
  No environment except ``prod`` may name the production relay or DataEdge,
  and ``prod`` may not name the staging ones.
* ``ENCLAVE_ALLOW_OWNER_EOA=1`` is refused when the environment is ``prod`` or
  the derivation root is the production root (spec §8 "Key handling"). The
  check runs on every ``Settings`` construction, so ``dataclasses.replace``
  cannot bypass it. ``dev`` and ``staging`` may set the flag; neither can
  reach the production relay, DataEdge or root.
* Error messages name the variable and the rule. They never echo the value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import urlsplit

from totalreclaw_enclave.errors import SafeMessageError

Env = Literal["dev", "staging", "prod"]
ENVS: Final[tuple[Env, ...]] = ("dev", "staging", "prod")

PROD_DERIVATION_ROOT: Final = "totalreclaw-enclave-v1/prod"
DERIVATION_ROOTS: Final[Mapping[Env, str]] = {
    "dev": "totalreclaw-enclave-v1/dev",
    "staging": "totalreclaw-enclave-v1/staging",
    "prod": PROD_DERIVATION_ROOT,
}

STAGING_RELAY_URL: Final = "https://api-staging.totalreclaw.xyz"
PROD_RELAY_URL: Final = "https://api.totalreclaw.xyz"
# Stored lowercase; compare lowercase.
STAGING_DATA_EDGE: Final = "0xe7a4d2677b686e13775ba9092631089e35f0bb91"
PROD_DATA_EDGE: Final = "0xc445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca"
GNOSIS_CHAIN_ID: Final = 100

DEFAULT_TRANSPARENCY_URL: Final = "https://totalreclaw.xyz/enclave"
DEV_PLACEHOLDER_HASH: Final = "0" * 64

_HEX64 = re.compile(r"[0-9a-f]{64}")
# NEAR account id rules (nomicon: 2-64 chars, lowercase alnum separated by - _ .).
_NEAR_ACCOUNT = re.compile(r"(?:[a-z\d]+[-_])*[a-z\d]+(?:\.(?:[a-z\d]+[-_])*[a-z\d]+)*")
_MEASUREMENT_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR")


class SettingsError(SafeMessageError):
    """Invalid boot configuration. The message never contains a value."""


class Secret:
    """Wrapper for secret configuration values (added by later leaves).

    ``repr``/``str`` never reveal the value; call ``reveal()`` at the single
    point of use. ENC-2 has no secret settings; ENC-9 (NEAR AI API key) and
    ENC-11 (NEAR function-call key) add theirs as ``Secret``, never ``str``.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


@dataclass(frozen=True, slots=True)
class Settings:
    env: Env
    public_url: str
    bind_host: str
    bind_port: int
    db_path: Path
    log_level: str
    allow_owner_eoa: bool
    derivation_root: str
    relay_url: str
    data_edge: str
    chain_id: int
    near_contract: str | None
    measurement_id: str
    compose_hash: str
    os_image_hash: str
    inference_policy_version: str
    transparency_url: str

    def __post_init__(self) -> None:
        # Every construction re-checks the refusal, including a
        # dataclasses.replace(settings, ...) after boot (ENC-11 swaps in
        # attested values that way).
        assert_owner_eoa_permitted(self, self.derivation_root)


def assert_owner_eoa_permitted(settings: Settings, derivation_root: str) -> None:
    """Refuse ``ENCLAVE_ALLOW_OWNER_EOA`` on production.

    Called by ``Settings.__post_init__`` with the settings' own root (so
    ``load_settings`` and every ``dataclasses.replace`` run it), and again by
    ENC-11 with the ``derivation_root`` read from the NEAR gate contract at boot.
    """
    if not settings.allow_owner_eoa:
        return
    if settings.env == "prod":
        raise SettingsError("ENCLAVE_ALLOW_OWNER_EOA is refused when ENCLAVE_ENV=prod")
    if derivation_root == PROD_DERIVATION_ROOT:
        raise SettingsError("ENCLAVE_ALLOW_OWNER_EOA is refused on the production derivation root")


def _get(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require(environ: Mapping[str, str], name: str, env: Env) -> str:
    value = _get(environ, name)
    if value is None:
        raise SettingsError(f"{name} is required when ENCLAVE_ENV={env}")
    return value


def _https_origin(name: str, value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise SettingsError(f"{name} must be an https origin with no path, query or fragment")
    return f"https://{parts.netloc}"


def _bool_flag(environ: Mapping[str, str], name: str) -> bool:
    value = _get(environ, name)
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise SettingsError(f"{name} must be 0 or 1")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Parse and validate the environment. Raises ``SettingsError``."""
    if environ is None:
        import os

        environ = os.environ

    raw_env = _get(environ, "ENCLAVE_ENV")
    if raw_env is None:
        raise SettingsError("ENCLAVE_ENV is required (dev, staging or prod)")
    if raw_env not in ENVS:
        raise SettingsError("ENCLAVE_ENV must be one of dev, staging, prod")
    env = cast(Env, raw_env)

    derivation_root = DERIVATION_ROOTS[env]
    override_root = _get(environ, "ENCLAVE_DERIVATION_ROOT")
    if override_root is not None and override_root != derivation_root:
        raise SettingsError("ENCLAVE_DERIVATION_ROOT does not match the root fixed for ENCLAVE_ENV")

    # Relay + DataEdge: fixed per environment; dev may override the relay only.
    if env == "prod":
        relay_url, data_edge = PROD_RELAY_URL, PROD_DATA_EDGE
    else:
        relay_url, data_edge = STAGING_RELAY_URL, STAGING_DATA_EDGE
    relay_override = _get(environ, "ENCLAVE_RELAY_URL")
    if relay_override is not None:
        relay_override = relay_override.rstrip("/")
        if env != "dev" and relay_override != relay_url:
            raise SettingsError("ENCLAVE_RELAY_URL may only be overridden when ENCLAVE_ENV=dev")
        relay_url = relay_override
    if env != "prod" and urlsplit(relay_url).netloc == urlsplit(PROD_RELAY_URL).netloc:
        raise SettingsError("the production relay is refused unless ENCLAVE_ENV=prod")
    if env == "prod" and urlsplit(relay_url).netloc == urlsplit(STAGING_RELAY_URL).netloc:
        raise SettingsError("the staging relay is refused when ENCLAVE_ENV=prod")

    log_level = (_get(environ, "ENCLAVE_LOG_LEVEL") or "INFO").upper()
    if log_level not in _LOG_LEVELS:
        raise SettingsError("ENCLAVE_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    if env == "prod" and log_level == "DEBUG":
        raise SettingsError("ENCLAVE_LOG_LEVEL=DEBUG is refused when ENCLAVE_ENV=prod")

    port_raw = _get(environ, "ENCLAVE_PORT") or "8080"
    if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
        raise SettingsError("ENCLAVE_PORT must be an integer in 1..65535")

    if env == "dev":
        public_url = (_get(environ, "ENCLAVE_PUBLIC_URL") or "http://127.0.0.1:8080").rstrip("/")
        db_path = Path(_get(environ, "ENCLAVE_DB_PATH") or ".enclave-dev/enclave.sqlite3")
        if _get(environ, "ENCLAVE_NEAR_CONTRACT") is not None:
            raise SettingsError("ENCLAVE_NEAR_CONTRACT must be unset when ENCLAVE_ENV=dev (no NEAR network)")
        near_contract: str | None = None
        measurement_id = _get(environ, "ENCLAVE_MEASUREMENT_ID") or "dev"
        compose_hash = _get(environ, "ENCLAVE_COMPOSE_HASH") or DEV_PLACEHOLDER_HASH
        os_image_hash = _get(environ, "ENCLAVE_OS_IMAGE_HASH") or DEV_PLACEHOLDER_HASH
        inference_policy_version = _get(environ, "ENCLAVE_INFERENCE_POLICY_VERSION") or "dev"
    else:
        public_url = _https_origin("ENCLAVE_PUBLIC_URL", _require(environ, "ENCLAVE_PUBLIC_URL", env))
        db_path = Path(_require(environ, "ENCLAVE_DB_PATH", env))
        near_contract = _require(environ, "ENCLAVE_NEAR_CONTRACT", env)
        if not (2 <= len(near_contract) <= 64 and _NEAR_ACCOUNT.fullmatch(near_contract)):
            raise SettingsError("ENCLAVE_NEAR_CONTRACT must be a NEAR account id")
        # Spec §11 Q3: staging = testnet gate contract, prod = mainnet.
        if env == "staging" and not near_contract.endswith(".testnet"):
            raise SettingsError("ENCLAVE_NEAR_CONTRACT must be a .testnet account when ENCLAVE_ENV=staging")
        if env == "prod" and not near_contract.endswith(".near"):
            raise SettingsError("ENCLAVE_NEAR_CONTRACT must be a .near account when ENCLAVE_ENV=prod")
        measurement_id = _require(environ, "ENCLAVE_MEASUREMENT_ID", env)
        compose_hash = _require(environ, "ENCLAVE_COMPOSE_HASH", env).lower()
        os_image_hash = _require(environ, "ENCLAVE_OS_IMAGE_HASH", env).lower()
        inference_policy_version = _require(environ, "ENCLAVE_INFERENCE_POLICY_VERSION", env)

    if not _MEASUREMENT_ID.fullmatch(measurement_id):
        raise SettingsError("ENCLAVE_MEASUREMENT_ID must match [a-z0-9][a-z0-9_.:-]{0,63}")
    for name, value in (("ENCLAVE_COMPOSE_HASH", compose_hash), ("ENCLAVE_OS_IMAGE_HASH", os_image_hash)):
        if not _HEX64.fullmatch(value):
            raise SettingsError(f"{name} must be 64 lowercase hex characters")
    if not _MEASUREMENT_ID.fullmatch(inference_policy_version):
        raise SettingsError("ENCLAVE_INFERENCE_POLICY_VERSION must match [a-z0-9][a-z0-9_.:-]{0,63}")

    transparency_url = _get(environ, "ENCLAVE_TRANSPARENCY_URL") or DEFAULT_TRANSPARENCY_URL
    if urlsplit(transparency_url).scheme != "https":
        raise SettingsError("ENCLAVE_TRANSPARENCY_URL must be an https URL")

    return Settings(
        env=env,
        public_url=public_url,
        bind_host=_get(environ, "ENCLAVE_BIND_HOST") or "127.0.0.1",
        bind_port=int(port_raw),
        db_path=db_path,
        log_level=log_level,
        allow_owner_eoa=_bool_flag(environ, "ENCLAVE_ALLOW_OWNER_EOA"),
        derivation_root=derivation_root,
        relay_url=relay_url,
        data_edge=data_edge,
        chain_id=GNOSIS_CHAIN_ID,
        near_contract=near_contract,
        measurement_id=measurement_id,
        compose_hash=compose_hash,
        os_image_hash=os_image_hash,
        inference_policy_version=inference_policy_version,
        transparency_url=transparency_url,
    )
