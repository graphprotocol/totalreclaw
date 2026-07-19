"""keychain_wrap — OS keychain wrap for credential secrets at rest (cred-2).

Python-only Hermes desktop keychain wrap, per the 2026-05-27 re-scope of the
cred-2 roadmap leaf (``plans/2026-05-06-credentials-at-rest-roadmap.md``,
Phase 2) and the session-key-delegation spec §5. This module provides the
stable interface the session-key auto-migration (cred-5 / cred-10,
``hermes/auto_migrate.py``) is written against::

    wrap_blob(name: str, blob: bytes) -> None
    unwrap_blob(name: str) -> bytes

… backed by the OS-native secret store on each platform:

* **macOS**   — Keychain
* **Linux**   — Secret Service (libsecret / GNOME Keyring / KWallet) over D-Bus
* **Windows** — Credential Manager

via the cross-platform :mod:`keyring` library. ``keyring`` is the Python
equivalent of the ``keytar``-style dependency the original roadmap called for:
it is pure-Python glue that talks to the platform's *own* secret store, so it
ships **no bundled native bindings** and sidesteps the OpenClaw native-bindings
scanner question that was deferred to the post-hermes parity queue (Pedro,
2026-05-27).

Graceful fallback (headless / container)
----------------------------------------
On a headless host or inside a stock container there is no unlocked OS keychain
(no D-Bus session bus on Linux, no logged-in Keychain on macOS). In that case
:func:`keychain_available` returns ``False`` and the higher-level
:func:`wrap_credentials_mnemonic` leaves the secret in plaintext
``credentials.json`` (chmod ``0o600``) and records ``keychain_wrapped: false``
— the documented status-quo protection level from the roadmap ("Container UX:
detection fails → fall back to status-quo plaintext-at-rest + chmod 600").
Nothing crashes; callers just stay on the old at-rest posture.

``keyring`` itself is an **optional** dependency (extra ``[keychain]``). If it
is not installed the module behaves exactly like the headless-fallback case —
:func:`keychain_available` is ``False`` and no secret is ever moved.

Scope boundary (important)
--------------------------
This module is ONLY the wrap helper plus a pure-function convenience layer over
a credentials dict. It deliberately does **not** rewire the live credential I/O
in ``agent/state.py`` / ``hermes/cli.py``. The actual auto-migration (dropping
the on-disk mnemonic, ``.bak`` handling, idempotency, failure-recovery) is the
separately-tracked ``hermes/auto_migrate.py`` deliverable (session-key spec §5,
cred-5 / cred-10). Keeping them separate avoids colliding with the parallel
cred-5 credentials-write-path work and keeps this change behaviour-neutral
until a caller opts in. The future ``file`` / ``keychain`` / ``external``
source selection belongs in :mod:`totalreclaw.credential_provider` (cred-3),
which can grow a ``keychain`` backend on top of the primitives here.

Phrase-safety
-------------
The wrapped secret is handled only as opaque ``bytes`` / a passed-in string. It
is **never** logged, printed, or returned through any user-facing / agent sink.
Log lines carry only the non-secret entry name. (Enforced by the AST
terminology sweep in ``python/tests/test_onboarding.py``.)

Added: cred-2 (2026-07-20).
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: keyring "service" namespace under which every TotalReclaw secret is stored.
SERVICE_NAME = "totalreclaw"

#: Fallback keychain entry name when the creds dict carries no ``userId`` to
#: key on. Multiple installs on one host with no userId will share this entry;
#: :func:`entry_name_for` prefers a per-user name whenever possible.
DEFAULT_ENTRY_NAME = "credential-secret"

#: Non-secret flag written into ``credentials.json`` signalling the new shape.
#: ``true``  → the secret lives in the OS keychain (this module owns it).
#: ``false`` → the secret is still plaintext in the file (fallback level).
KEYCHAIN_WRAPPED_KEY = "keychain_wrapped"

#: Credential key names that may hold the plaintext secret in a legacy /
#: fallback ``credentials.json``. Mirrors
#: ``totalreclaw.onboarding._CREDENTIAL_KEYS`` +
#: ``agent/state.py::_extract_mnemonic_from_creds``.
_CREDENTIAL_KEYS: Tuple[str, ...] = ("mnemonic", "recovery_phrase")

#: Shape that lives in credentials.json. Kept as a plain dict for parity with
#: :mod:`totalreclaw.credential_provider` (``CredentialsDict``) — see that
#: module for why it is not a strict ``TypedDict`` during the migration window.
CredentialsDict = dict


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KeychainError(Exception):
    """Base error for keychain wrap operations."""


class KeychainUnavailable(KeychainError):
    """Raised when no usable OS keychain backend is present or reachable.

    Higher-level helpers (:func:`wrap_credentials_mnemonic`) catch this and
    fall back to plaintext-at-rest; the low-level :func:`wrap_blob` /
    :func:`unwrap_blob` surface it so a migration caller can restore from a
    ``.bak`` and abort cleanly (session-key spec §9 R3).
    """


# ---------------------------------------------------------------------------
# Backend seam + availability
# ---------------------------------------------------------------------------


def _load_keyring():
    """Import :mod:`keyring` lazily. Return the module, or ``None`` if it is
    not installed / fails to import.

    This is the single seam the whole module (and its tests) route through, so
    keychain access can be exercised without a real OS keychain and without
    ``keyring`` installed in the environment.
    """
    try:
        import keyring  # noqa: PLC0415 — deliberately lazy / optional
        return keyring
    except Exception:  # pragma: no cover - defensive: any import failure
        return None


def keychain_available() -> bool:
    """Return ``True`` iff a usable OS keychain backend is present + reachable.

    Returns ``False`` (never raises) when:

    * :mod:`keyring` is not installed;
    * the active backend is the null / fail backend (keyring's signal for "no
      real secret store on this host");
    * on Linux, the Secret Service backend is active but there is no D-Bus
      session bus (``DBUS_SESSION_BUS_ADDRESS`` unset) — the typical stock
      container / headless case. Detecting this here avoids letting the first
      wrap call raise mid-migration.
    """
    kr = _load_keyring()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
    except Exception:
        return False
    if backend is None:
        return False

    name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    if "fail" in name or "null" in name:
        return False

    # Linux Secret Service needs a D-Bus session bus to reach the daemon.
    if "secretservice" in name or "libsecret" in name:
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return False

    return True


# ---------------------------------------------------------------------------
# Stable low-level interface (session-key spec §5 assumes this exactly)
# ---------------------------------------------------------------------------


def wrap_blob(name: str, blob: bytes) -> None:
    """Store ``blob`` in the OS keychain under ``(SERVICE_NAME, name)``.

    The bytes are base64-encoded for storage because keyring's cross-platform
    contract is string-valued. Overwrites any existing entry with the same
    name.

    Raises
    ------
    TypeError
        If ``blob`` is not ``bytes`` / ``bytearray``.
    KeychainUnavailable
        If no usable OS keychain backend is present.
    KeychainError
        If the backend is present but the write fails.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("blob must be bytes")

    kr = _load_keyring()
    if kr is None or not keychain_available():
        raise KeychainUnavailable("no usable OS keychain backend")

    encoded = base64.b64encode(bytes(blob)).decode("ascii")
    try:
        kr.set_password(SERVICE_NAME, name, encoded)
    except Exception as exc:  # keyring.errors.PasswordSetError + backend errors
        raise KeychainError(f"keychain write failed for entry {name!r}") from exc
    logger.debug("wrapped credential secret into keychain entry %r", name)


def unwrap_blob(name: str) -> bytes:
    """Return the bytes previously stored under ``(SERVICE_NAME, name)``.

    Raises
    ------
    KeychainUnavailable
        If no usable OS keychain backend is present.
    KeyError
        If there is no entry under ``name``.
    KeychainError
        If the stored value is corrupt or the read fails.
    """
    kr = _load_keyring()
    if kr is None or not keychain_available():
        raise KeychainUnavailable("no usable OS keychain backend")

    try:
        stored = kr.get_password(SERVICE_NAME, name)
    except Exception as exc:
        raise KeychainError(f"keychain read failed for entry {name!r}") from exc

    if stored is None:
        raise KeyError(name)

    try:
        return base64.b64decode(stored.encode("ascii"), validate=True)
    except Exception as exc:
        raise KeychainError(f"corrupt keychain entry {name!r}") from exc


def delete_blob(name: str) -> None:
    """Remove the keychain entry under ``name``. Best-effort — a no-op if the
    entry is already absent. Used by revoke flows + test cleanup.

    Raises
    ------
    KeychainUnavailable
        If no usable OS keychain backend is present.
    """
    kr = _load_keyring()
    if kr is None or not keychain_available():
        raise KeychainUnavailable("no usable OS keychain backend")
    try:
        kr.delete_password(SERVICE_NAME, name)
    except Exception:
        # keyring.errors.PasswordDeleteError (entry absent) or a transient
        # backend hiccup — deletion is best-effort cleanup, so swallow.
        logger.debug("keychain entry %r absent or already removed", name)


# ---------------------------------------------------------------------------
# Convenience layer over a credentials dict (pure — never mutates input)
# ---------------------------------------------------------------------------


def _extract_secret(creds: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(secret, key_name)`` for the first non-empty credential key in
    ``creds``, or ``(None, None)`` if none is present."""
    for key in _CREDENTIAL_KEYS:
        value = creds.get(key)
        if isinstance(value, str) and value.strip():
            return value, key
    return None, None


def entry_name_for(creds: Mapping[str, Any]) -> str:
    """Derive a stable, **non-secret** keychain entry name from ``creds``.

    Keyed on the non-secret ``userId`` (falling back to ``user_id``) so several
    installs / users on one host don't collide. Falls back to
    :data:`DEFAULT_ENTRY_NAME` when no user id is present.
    """
    uid = creds.get("userId") or creds.get("user_id")
    if isinstance(uid, str) and uid.strip():
        return f"{DEFAULT_ENTRY_NAME}:{uid.strip()}"
    return DEFAULT_ENTRY_NAME


def harden_file_permissions(path: Union[str, Path]) -> None:
    """Best-effort ``chmod 0o600`` on the credentials file (matches the
    existing ``hermes/cli.py`` pattern). No-op / swallow on Windows and
    read-only filesystems."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def wrap_credentials_mnemonic(
    creds: Mapping[str, Any],
    *,
    entry_name: Optional[str] = None,
    harden_path: Optional[Union[str, Path]] = None,
) -> CredentialsDict:
    """Move the plaintext secret out of ``creds`` into the OS keychain.

    Returns a **new** dict; the input is never mutated. Behaviour:

    * **Keychain available + secret present** → store the secret blob in the
      keychain, drop the plaintext credential key(s) from the returned dict,
      set ``keychain_wrapped: true``.
    * **Keychain unavailable + secret present** (headless / container, or
      ``keyring`` not installed) → leave the secret in place, set
      ``keychain_wrapped: false`` (documented plaintext fallback), and — if
      ``harden_path`` is given — chmod it ``0o600``.
    * **No secret present** → return a copy unchanged (idempotent: the file is
      either already wrapped or empty; the existing ``keychain_wrapped`` flag,
      if any, is preserved).

    Never raises on the fallback path. A genuine keychain *write* failure when a
    backend IS present propagates as :class:`KeychainError` so a migration
    caller can restore from ``.bak`` and abort (session-key spec §9).
    """
    out: CredentialsDict = dict(creds)
    secret, _key = _extract_secret(creds)

    if secret is None:
        # Nothing to move — leave the dict (and any existing flag) as-is.
        return out

    if not keychain_available():
        # Documented fallback: keep plaintext, mark the lower protection level.
        out[KEYCHAIN_WRAPPED_KEY] = False
        if harden_path is not None:
            harden_file_permissions(harden_path)
        return out

    name = entry_name or entry_name_for(creds)
    wrap_blob(name, secret.encode("utf-8"))
    for key in _CREDENTIAL_KEYS:
        out.pop(key, None)
    out[KEYCHAIN_WRAPPED_KEY] = True
    return out


def unwrap_credentials_mnemonic(
    creds: Mapping[str, Any],
    *,
    entry_name: Optional[str] = None,
) -> str:
    """Read the credential secret regardless of at-rest shape (migration read).

    * If ``creds[keychain_wrapped]`` is truthy → unwrap from the OS keychain.
    * Otherwise → read the plaintext ``mnemonic`` / ``recovery_phrase`` from
      ``creds`` (backward-compat for legacy + fallback installs).

    Raises
    ------
    KeyError
        If the secret cannot be found in either place.
    KeychainUnavailable / KeychainError
        Propagated from :func:`unwrap_blob` when a wrapped entry is expected
        but the backend is missing / the read fails.
    """
    if creds.get(KEYCHAIN_WRAPPED_KEY):
        name = entry_name or entry_name_for(creds)
        return unwrap_blob(name).decode("utf-8")

    secret, _key = _extract_secret(creds)
    if secret is None:
        raise KeyError("no credential secret found (neither plaintext nor keychain)")
    return secret


__all__ = [
    "SERVICE_NAME",
    "DEFAULT_ENTRY_NAME",
    "KEYCHAIN_WRAPPED_KEY",
    "CredentialsDict",
    "KeychainError",
    "KeychainUnavailable",
    "keychain_available",
    "wrap_blob",
    "unwrap_blob",
    "delete_blob",
    "entry_name_for",
    "harden_file_permissions",
    "wrap_credentials_mnemonic",
    "unwrap_credentials_mnemonic",
]
