"""credential_provider — credential source abstraction (cred-3 stage 2).

Python mirror of `skill/plugin/credential-provider.ts` (cred-3 stage 1,
merged in p-diogo/totalreclaw#271). Same shape: a small abstraction over
where Hermes reads / writes `credentials.json` from, so the daemon can be
run against either:

  1. ``file`` — read/write ``~/.totalreclaw/credentials.json`` directly
     (legacy behavior; default; what every existing call site does today).

  2. ``external`` — read from a secret manager via one of two transports:

     - **Inline JSON** (``TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON``) — the raw
       credentials JSON injected as an env var at process start. Most
       ergonomic for managed platforms exposing secrets as env vars
       (Railway secrets, K8s ``envFrom``, Docker ``--env-file``).

     - **File mount** (``TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH``) — a path
       to a JSON file the secret manager mounts. Works with Docker Compose
       ``secrets:`` blocks, K8s secret ``volumeMount``\\s, and tmpfs paths
       populated by an ops wrapper that pulls the payload from a vault
       (AWS Secrets Manager, HashiCorp Vault, etc.) before Hermes start.

Stage 2 introduces the module + unit tests. Call sites in
``hermes/cli.py``, ``hermes/pair_tool_completion.py``, ``agent/state.py``,
``pair/http_server.py``, etc. are **not yet rewired** to go through this
abstraction — that's stage 3. The default ``file`` mode is byte-identical
to today's direct ``json.dumps`` / ``read_text`` calls.

Phrase-safety:
  - Credential payloads are never logged from this module.
  - File writes preserve mode ``0o600`` (best-effort on Windows / read-only
    filesystems, matching the existing ``hermes/cli.py`` pattern).
  - ``external`` mode is read-only by design — the secret manager owns
    the source of truth and writing back from Hermes would split it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

logger = logging.getLogger(__name__)

ProviderMode = Literal["file", "external"]


# ---------------------------------------------------------------------------
# CredentialsDict — the shape that lives in credentials.json
# ---------------------------------------------------------------------------

# We keep this as a plain ``dict[str, Any]`` (rather than ``TypedDict``)
# for two reasons:
#
# 1. The existing Python call sites in ``hermes/cli.py`` already do
#    ``json.dumps({"mnemonic": ..., "scope_address": ...})`` with a
#    free-form dict; introducing a strict TypedDict would force a
#    cascade of touch-ups across cli.py, pair_tool_completion.py,
#    onboarding.py, etc. That work belongs in cred-3 stage 3.
#
# 2. The v1 schema (legacy: ``{mnemonic}``) and v2 schema (post-spec:
#    ``{version, schema, session_signer, smart_account, ...}``) coexist
#    during the migration window. A plain dict accommodates both
#    without us reaching for a discriminated union.

CredentialsDict = dict[str, Any]


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class CredentialProvider(Protocol):
    """Three-method protocol covering the credential lifecycle.

    Mirrors the TS interface — see ``skill/plugin/credential-provider.ts``
    in cred-3 stage 1 for the canonical contract.
    """

    @property
    def mode(self) -> ProviderMode:
        """``"file"`` or ``"external"`` — exposed so callers can branch
        on UI / warning behaviour (e.g. skip the first-run nudge in
        ``external`` mode because the mnemonic was authored elsewhere).
        """
        ...

    def load(self) -> Optional[CredentialsDict]:
        """Return parsed credentials, or ``None`` when the source has
        none usable.

        ``None`` covers every "no credentials available" path —
        file-not-found, empty payload, malformed JSON, env var unset.
        Never raises; the caller decides how to surface a missing
        credential (typically: log + fall through to a fresh-generate
        bootstrap, same shape as today's first-run detection).
        """
        ...

    def save(self, creds: CredentialsDict) -> bool:
        """Persist freshly-generated or updated credentials.

        Returns ``True`` on success, ``False`` on failure (the caller
        decides whether to retry or escalate). Read-only providers
        (``external``) always return ``False`` — the caller should log a
        warning that the write didn't propagate to the secret manager.
        """
        ...

    def clear(self) -> bool:
        """Delete the credentials (used by re-pairing flows).

        Returns ``True`` on success, ``False`` for read-only providers
        or filesystem errors.
        """
        ...


# ---------------------------------------------------------------------------
# FileCredentialProvider — default; reads / writes credentials.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileCredentialProvider:
    """Default provider — reads + writes ``credentials.json`` on disk.

    Behaviour matches the existing inline ``path.read_text()`` /
    ``path.write_text(json.dumps(...))`` pattern in ``hermes/cli.py``
    et al., so wiring a call site through this provider in ``file`` mode
    produces zero behaviour delta.
    """

    credentials_path: Path

    @property
    def mode(self) -> ProviderMode:
        return "file"

    def load(self) -> Optional[CredentialsDict]:
        try:
            if not self.credentials_path.exists():
                return None
        except OSError:
            return None

        try:
            raw = self.credentials_path.read_text()
        except OSError:
            return None

        if not raw.strip():
            return None

        try:
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None

        if not isinstance(parsed, dict):
            return None

        return parsed

    def save(self, creds: CredentialsDict) -> bool:
        try:
            self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
            self.credentials_path.write_text(json.dumps(creds, indent=2))
        except OSError:
            logger.warning(
                "FileCredentialProvider.save: failed to write %s",
                self.credentials_path,
                exc_info=True,
            )
            return False

        # Best-effort mode 0o600 — matches the inline pattern in
        # hermes/cli.py (Windows / read-only FS may reject; we accept that
        # rather than block save). Never log the payload.
        try:
            self.credentials_path.chmod(0o600)
        except OSError:
            logger.debug(
                "FileCredentialProvider.save: chmod 0600 failed on %s",
                self.credentials_path,
                exc_info=True,
            )

        return True

    def clear(self) -> bool:
        try:
            self.credentials_path.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning(
                "FileCredentialProvider.clear: failed to unlink %s",
                self.credentials_path,
                exc_info=True,
            )
            return False


# ---------------------------------------------------------------------------
# ExternalCredentialProvider — read-only secret-manager wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalCredentialProvider:
    """Read-only secret-manager wrapper.

    Two transports — env-injected JSON (``inline_json``) wins if both are
    set. Both are read-only: ``save()`` and ``clear()`` always return
    ``False``. The secret manager owns the canonical state; writing back
    would create two competing sources of truth.
    """

    inline_json: Optional[str]
    file_path: Optional[Path]

    @property
    def mode(self) -> ProviderMode:
        return "external"

    def load(self) -> Optional[CredentialsDict]:
        raw = self._read_raw()
        if raw is None:
            return None

        try:
            parsed = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            # Parse error — return None so the bootstrap path can decide
            # how to surface (typically: log + fall through to fresh
            # bootstrap, same as a corrupt credentials.json on disk).
            # Deliberately do not echo the payload here on parse failure.
            logger.warning(
                "ExternalCredentialProvider.load: payload did not parse as JSON"
            )
            return None

        if not isinstance(parsed, dict):
            logger.warning(
                "ExternalCredentialProvider.load: payload was not a JSON object "
                "(type=%s)",
                type(parsed).__name__,
            )
            return None

        return parsed

    def save(self, _creds: CredentialsDict) -> bool:
        # Read-only.
        return False

    def clear(self) -> bool:
        # Read-only.
        return False

    def _read_raw(self) -> Optional[str]:
        if self.inline_json is not None:
            return self.inline_json

        if self.file_path is not None:
            try:
                if not self.file_path.exists():
                    return None
                return self.file_path.read_text()
            except OSError:
                return None

        return None


# ---------------------------------------------------------------------------
# Config + factory
# ---------------------------------------------------------------------------

# Env-var names — kept identical to the TS side (cred-3 stage 1) so
# operators configure the same way regardless of which client (plugin
# or Hermes) consumes the secret.

ENV_PROVIDER = "TOTALRECLAW_CREDENTIALS_PROVIDER"
ENV_EXTERNAL_JSON = "TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON"
ENV_EXTERNAL_PATH = "TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH"
ENV_CREDENTIALS_PATH = "TOTALRECLAW_CREDENTIALS_PATH"


def _resolve_credentials_path() -> Path:
    """Default credentials path — matches ``onboarding.CANONICAL_CREDENTIALS_PATH``.

    Honours ``TOTALRECLAW_CREDENTIALS_PATH`` env override (used by the
    pair-tool integration tests + by operators who relocate the dotfile).
    """
    override = os.environ.get(ENV_CREDENTIALS_PATH)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".totalreclaw" / "credentials.json"


def _resolve_provider_mode() -> ProviderMode:
    raw = os.environ.get(ENV_PROVIDER, "file").strip().lower()
    if raw == "external":
        return "external"
    # Anything else (unset, empty, "file", typo) → file mode. Symmetric
    # with the TS side which also defaults to file. We do not silently
    # honour unknown modes — that would mask a deploy-time mistake.
    return "file"


def get_credential_provider(
    *,
    credentials_path: Optional[Path] = None,
    provider_mode: Optional[ProviderMode] = None,
    inline_json: Optional[str] = None,
    external_file_path: Optional[Path] = None,
) -> CredentialProvider:
    """Factory — pick the configured provider.

    All parameters are optional; unset values fall back to env vars (so
    production callers can call with zero args and tests can override by
    passing explicit values).

    In ``external`` mode with neither transport set, we still return an
    ``ExternalCredentialProvider`` so the caller observes the configured
    mode + sees ``None`` from ``load()``. Mirroring the TS stage-1
    behaviour: we do NOT silently fall back to ``file`` mode (that would
    mask a deploy-time mistake by reading a stale credentials.json left
    on disk).
    """
    mode: ProviderMode = (
        provider_mode if provider_mode is not None else _resolve_provider_mode()
    )

    if mode == "external":
        resolved_inline = (
            inline_json if inline_json is not None else os.environ.get(ENV_EXTERNAL_JSON)
        )
        resolved_path: Optional[Path]
        if external_file_path is not None:
            resolved_path = external_file_path
        else:
            env_path = os.environ.get(ENV_EXTERNAL_PATH)
            resolved_path = Path(env_path).expanduser() if env_path else None

        return ExternalCredentialProvider(
            inline_json=resolved_inline,
            file_path=resolved_path,
        )

    resolved_credentials_path = (
        credentials_path if credentials_path is not None else _resolve_credentials_path()
    )
    return FileCredentialProvider(credentials_path=resolved_credentials_path)
