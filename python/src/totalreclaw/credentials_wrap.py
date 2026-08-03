"""credentials_wrap — OS keychain wrap of the mnemonic at rest (cred-2 / internal#262).

Goal
----
The 12-word recovery phrase (the only root key for a TotalReclaw wallet)
should never sit in plaintext in ``credentials.json`` when the host has a
usable OS keychain. Instead the phrase is stored in the OS keychain under
a per-wallet *account*, and ``credentials.json`` carries a non-secret
**marker** in place of the mnemonic field.

    {
      "mnemonic": "__keychain__:v1:0x<eoa-address>",
      "keychain_wrapped": true,
      "scope_address": "0x<smart-account>",
      ...
    }

Backend selection (first match wins)
------------------------------------
1. ``keyring`` (lazy import) — native OS API via the Security framework /
   Secret Service / Credential Manager. **No argv exposure** of the
   secret. **Mandatory on macOS** (see "macOS keyring is a hard
   dependency" below) — installed automatically by ``pip install
   totalreclaw`` on darwin; optional elsewhere via the ``[keychain]``
   extra.
2. Linux Secret Service via ``secretstorage`` (lazy import).
3. None of the above, or the kill-switch armed → **plaintext fallback**
   (the exact pre-cred-2 behaviour).

Phrase-safety rails (hard)
--------------------------
* The mnemonic is handled only as an opaque ``str`` inside the backend.
  It is never logged, printed, or embedded in any exception message.
* On ANY keychain failure (no backend, locked keychain, store error) the
  wrap silently falls back to plaintext and records nothing sensitive —
  a single ``logger.debug`` line with no payload.
* No network. No change to the pair/restore UX.

Fail-loud guarantee (backward + forward compat)
-----------------------------------------------
An OLD client that does not understand the marker would read it and try
to use it as a mnemonic. The marker is a single token (the embedded EOA
address carries no whitespace) and therefore **fails BIP-39 validation
at every consumer** — verified empirically against:

* ``eth_account.Account.from_mnemonic`` (the validator used by
  ``cli.py`` doctor, ``hermes._validate_mnemonic`` and ``client``);
* the ``mnemonic`` package wordlist ``check`` (the reference impl);
* the Rust ``totalreclaw_core.derive_keys_from_mnemonic`` (the deepest
  consumer on the ``agent/state.configure`` → ``client`` → ``crypto``
  path — it raises ``invalid word count: 1``).

So no consumer — including one that skips pre-validation — can silently
derive a *different* wallet from the marker. See ``test_credentials_wrap``.

macOS keyring is a hard dependency (fast-follow #558)
------------------------------------------------------
Earlier versions of this module fell back to the ``security
add-generic-password -w <secret>`` subprocess on macOS when ``keyring``
wasn't installed. That command passes the secret as an argument, so it
was briefly visible in the local process list during the one-time wrap
(the login keychain is per-user and the same user can already read any
of their own keychain items at will via ``security find-generic-password``,
so the incremental risk was small and local-attacker-only — but it was
cheap to eliminate). ``keyring>=24`` is now a **mandatory** dependency on
darwin (``sys_platform == 'darwin'`` marker in ``pyproject.toml``), so
``detect_backend()`` always resolves to ``"keyring"`` there and the
subprocess WRITE path (``_store_macos``) has been removed entirely — see
``test_credentials_wrap.py``.

The READ fallback (``_load_macos``, which uses ``-w`` as a *stdout* flag
— no argv exposure) is kept, but its recovery scope is narrower than it
looks: it can only successfully read entries that were themselves
written via the now-retired ``_store_macos`` subprocess path (i.e. a
legacy, pre-#558 install that resolved to the ``"macos"`` backend at
wrap time). Entries written via the Python ``keyring`` package's macOS
backend go through different Security-framework attribute plumbing and
are not guaranteed to be findable by ``security find-generic-password
-s <service> -a <account>`` with the same lookup. So ``_load_macos`` is
a **legacy-compat read path**, not a general recovery net for "the
mandatory ``keyring`` dependency somehow failed to import right now" —
if that degenerate case is hit on an install that originally wrapped
*through* ``keyring``, the read can still miss. It is otherwise
unreachable in normal operation, since ``detect_backend()`` checks
``keyring`` first.

v2 — bundle wrap (Option E Phase 2 / #581)
-------------------------------------------
The v1 scheme above wraps a *mnemonic string* keyed on the **EOA**
address. Phase 2 adds a second, independent scheme that wraps a
*derived-bundle-v1 secret subtree* (``{vault, signing}`` — see
``totalreclaw.bundle``) keyed on the **Smart Account** address::

    {
      "version": 2,
      "schema": "derived-bundle-v1",
      "keychain_wrapped": true,
      "account": {"smart_account": "0x<smart-account>", "chain_id": 100},
      "signing": {"kind": "owner-eoa", "address": "0x<owner-address>"},
      "provisioned_at": "...",
      "provisioned_by": "local-migration"
    }

The secret-bearing ``{vault, signing}`` subtree (private keys included)
lives ONLY in the keychain, under service ``totalreclaw``, account
``__keychain__:v2:<smart_account>`` — see :func:`wrap_bundle` /
:func:`unwrap_bundle`. The ``v2`` account namespace is deliberately
distinct from ``v1``'s (bare EOA address as account, prefixed marker
only as the on-disk *value*): a v1 entry and a v2 entry for the same
user's vault key on different strings (EOA vs ``__keychain__:v2:``-
prefixed Smart Account) and never collide, so both can coexist on one
machine during migration. Same phrase-safety rails, same kill-switch
(``TOTALRECLAW_NO_KEYCHAIN``), same backend chain, same
never-raise-on-wrap / fail-loud-on-load contract as v1.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "totalreclaw"
ENV_NO_KEYCHAIN = "TOTALRECLAW_NO_KEYCHAIN"

#: Marker prefix placed in the mnemonic-bearing field when the phrase is
#: keychain-wrapped. ``marker_for(account)`` yields ``PREFIX + account``.
#: The ``v1`` lets a future format bump coexist with old markers.
MARKER_PREFIX = "__keychain__:v1:"

#: v2 (Option E Phase 2 / #581) — keychain *account* prefix for a wrapped
#: derived-bundle-v1 secret subtree. Unlike v1 (bare EOA as account,
#: prefixed marker only as the credentials.json field VALUE), the v2
#: account identifier IS the full prefixed string
#: ``MARKER_PREFIX_V2 + smart_account`` — there is no v2 "marker embedded
#: in a field" convention, because credentials.json's v2 discovery metadata
#: has no secret-shaped field to replace (``account.smart_account`` is
#: already plain, non-secret data; ``keychain_wrapped: true`` is the flag).
#: See :func:`marker_for_v2` / :func:`wrap_bundle` / :func:`unwrap_bundle`.
MARKER_PREFIX_V2 = "__keychain__:v2:"

# Non-sensitive, static guidance strings. NEVER include the mnemonic,
# the marker payload, or the account in these — tests assert that.
MISSING_MESSAGE = (
    "Your recovery phrase is stored in the OS keychain but could not be "
    "retrieved (the keychain entry is missing or the keychain is locked). "
    "Re-run setup to restore your account from your recovery phrase."
)
UNAVAILABLE_MESSAGE = "OS keychain backend is unavailable."


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KeychainUnavailable(Exception):
    """No keychain backend, or the backend rejected the operation.

    Carries only :data:`UNAVAILABLE_MESSAGE` — never the secret.
    """


class KeychainEntryMissing(Exception):
    """A marker is present on disk but the keychain entry is gone / locked.

    Carries only :data:`MISSING_MESSAGE` — never the mnemonic, the marker
    payload, or the account.
    """


# ---------------------------------------------------------------------------
# Kill-switch + backend detection
# ---------------------------------------------------------------------------

_KILL_ON = frozenset({"1", "true", "yes", "on"})


def is_kill_switch_on() -> bool:
    """True when ``TOTALRECLAW_NO_KEYCHAIN`` is set to a truthy value.

    Forces plaintext behaviour (documented escape hatch for headless /
    container deploys where there is no keychain, or where an operator
    wants the pre-cred-2 plaintext-on-disk shape).
    """
    return os.environ.get(ENV_NO_KEYCHAIN, "").strip().lower() in _KILL_ON


# Lazily-imported optional backends. Probed once, cached.
_KEYRING = None  # type: ignore[var-annotated]
_KEYRING_PROBED = False
_SECRETSTORAGE = None  # type: ignore[var-annotated]
_SECRETSTORAGE_PROBED = False


def _try_keyring():
    global _KEYRING, _KEYRING_PROBED  # noqa: PLW0603
    if not _KEYRING_PROBED:
        _KEYRING_PROBED = True
        try:
            import keyring  # type: ignore[import-not-found]

            _KEYRING = keyring
        except Exception:
            _KEYRING = None
    return _KEYRING


def _try_secretstorage():
    global _SECRETSTORAGE, _SECRETSTORAGE_PROBED  # noqa: PLW0603
    if not _SECRETSTORAGE_PROBED:
        _SECRETSTORAGE_PROBED = True
        try:
            import secretstorage  # type: ignore[import-not-found]

            _SECRETSTORAGE = secretstorage
        except Exception:
            _SECRETSTORAGE = None
    return _SECRETSTORAGE


def detect_backend() -> Optional[str]:
    """Return the backend id that will service store/load, or ``None``.

    Order: ``keyring`` (if importable) → platform-native subprocess /
    library → ``None`` (plaintext fallback). Centralised so callers and
    tests branch / patch in one place.
    """
    if _try_keyring() is not None:
        return "keyring"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux") and _try_secretstorage() is not None:
        return "linux_ss"
    return None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def marker_for(account: str) -> str:
    """Build the on-disk marker for a keychain *account* (the EOA)."""
    return f"{MARKER_PREFIX}{account}"


def is_marker(value: object) -> bool:
    """True iff *value* is a keychain-marker string."""
    return isinstance(value, str) and value.startswith(MARKER_PREFIX)


def account_for_mnemonic(mnemonic: str) -> str:
    """Derive the keychain *account* (the EOA address) from a mnemonic.

    Deterministic + available synchronously (no network), so the same
    phrase always maps to the same keychain entry. Uses the canonical
    BIP-44 path ``m/44'/60'/0'/0/0`` already used across the codebase
    (``client._get_eoa_account``, ``cli`` doctor, ``hermes`` validator).
    """
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(mnemonic.strip(), account_path="m/44'/60'/0'/0/0")
    return acct.address


# ---------------------------------------------------------------------------
# Backend store / load (dispatch). Tests patch THESE module-level names.
# ---------------------------------------------------------------------------


def store_secret(account: str, secret: str) -> None:
    """Store *secret* under *account* in the OS keychain.

    Raises :class:`KeychainUnavailable` when there is no backend or the
    store fails. Never returns the secret in any error surface.
    """
    backend = detect_backend()
    try:
        if backend == "keyring":
            _store_keyring(account, secret)
        elif backend == "linux_ss":
            _store_linux(account, secret)
        else:
            # #558: the macOS `security add-generic-password -w` WRITE path
            # is retired (argv exposure). `keyring` is a mandatory darwin
            # dependency, so `backend == "macos"` should never happen in
            # normal operation — detect_backend() checks keyring first. If
            # it somehow does (corrupted install), fall back to plaintext
            # rather than shelling out with the secret in argv.
            raise KeychainUnavailable(UNAVAILABLE_MESSAGE)
    except KeychainUnavailable:
        raise
    except Exception:  # noqa: BLE001 — any backend error is a fallback signal
        logger.debug("credentials_wrap: keychain store failed (backend=%s)", backend)
        raise KeychainUnavailable(UNAVAILABLE_MESSAGE)


def load_secret(account: str) -> str:
    """Load the secret for *account* from the OS keychain.

    Raises :class:`KeychainEntryMissing` when the entry is absent / the
    keychain is locked, and :class:`KeychainUnavailable` when there is no
    usable backend.
    """
    backend = detect_backend()
    try:
        if backend == "keyring":
            return _load_keyring(account)
        if backend == "macos":
            return _load_macos(account)
        if backend == "linux_ss":
            return _load_linux(account)
        raise KeychainUnavailable(UNAVAILABLE_MESSAGE)
    except (KeychainEntryMissing, KeychainUnavailable):
        raise
    except Exception:  # noqa: BLE001
        logger.debug("credentials_wrap: keychain load failed (backend=%s)", backend)
        raise KeychainUnavailable(UNAVAILABLE_MESSAGE)


def delete_secret(account: str) -> None:
    """Best-effort delete of the secret stored under *account*.

    Never raises. A deletion failure (locked keychain, backend error,
    entry already gone) is not a reason to fail whatever cleanup called
    this — an orphaned stale entry is harmless once its marker no longer
    exists anywhere referencing it (nothing reads it once
    ``credentials.json`` has moved on). Added for Option E Phase 2 / #581
    (``hermes/auto_migrate.py`` drops the v1 entry after a v2 migration
    reads back correctly).

    The legacy macOS subprocess write path (``_store_macos``) is retired
    (#558) and never had a corresponding delete; on that degenerate
    ``backend == "macos"`` path this is a no-op.
    """
    backend = detect_backend()
    try:
        if backend == "keyring":
            kr = _try_keyring()
            if kr is not None:
                kr.delete_password(SERVICE_NAME, account)
        elif backend == "linux_ss":
            ss = _try_secretstorage()
            if ss is not None:
                bus = ss.dbus_init()
                col = ss.get_default_collection(bus)
                if col.is_locked():
                    col.unlock()
                for item in col.search_items({"service": SERVICE_NAME, "account": account}):
                    item.delete()
        # backend in (None, "macos"): nothing to delete through this module.
    except Exception:  # noqa: BLE001 — best-effort, never raise
        logger.debug("credentials_wrap: delete_secret failed (backend=%s)", backend)


def _store_keyring(account: str, secret: str) -> None:
    kr = _try_keyring()
    assert kr is not None  # narrow for type-checkers; detect_backend guards
    kr.set_password(SERVICE_NAME, account, secret)


def _load_keyring(account: str) -> str:
    kr = _try_keyring()
    assert kr is not None
    val = kr.get_password(SERVICE_NAME, account)
    if val is None:
        raise KeychainEntryMissing(MISSING_MESSAGE)
    return val


def _load_macos(account: str) -> str:
    # #558: legacy-compat READ only. This only finds entries written by
    # the now-removed `_store_macos` subprocess (pre-#558 installs). It is
    # NOT a general recovery net for keyring-written entries -- see the
    # module docstring's "macOS keyring is a hard dependency" section.
    res = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", account, "-w"],
        capture_output=True,
    )
    if res.returncode != 0:
        # Missing entry OR locked keychain — caller can't recover either.
        raise KeychainEntryMissing(MISSING_MESSAGE)
    return res.stdout.decode("utf-8", "replace").rstrip("\n")


def _store_linux(account: str, secret: str) -> None:
    ss = _try_secretstorage()
    assert ss is not None
    bus = ss.dbus_init()
    col = ss.get_default_collection(bus)
    if col.is_locked():
        col.unlock()
    col.create_item(
        f"{SERVICE_NAME}:{account}",
        {"service": SERVICE_NAME, "account": account},
        secret.encode("utf-8"),
        replace=True,
    )


def _load_linux(account: str) -> str:
    ss = _try_secretstorage()
    assert ss is not None
    bus = ss.dbus_init()
    col = ss.get_default_collection(bus)
    if col.is_locked():
        col.unlock()
    items = list(col.search_items({"service": SERVICE_NAME, "account": account}))
    if not items:
        raise KeychainEntryMissing(MISSING_MESSAGE)
    return items[0].get_secret().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Credential-dict helpers
# ---------------------------------------------------------------------------


def _extract_raw(creds: object) -> tuple[Optional[str], str]:
    """Return ``(key, stripped_value)`` for the mnemonic-bearing field.

    Canonical ``mnemonic`` key wins over the legacy ``recovery_phrase`` key
    (Bug #7 / Wave 2a — same preference as ``onboarding._CREDENTIAL_KEYS``
    and the plugin-side ``extractBootstrapMnemonic``). ``key`` is ``None``
    (and value ``""``) when neither carries a non-empty string. Kept here
    as the single source of truth so neither ``agent.state`` nor
    ``onboarding`` need to re-derive it.
    """
    if not isinstance(creds, dict):
        return None, ""
    primary = creds.get("mnemonic")
    if isinstance(primary, str) and primary.strip():
        return "mnemonic", primary.strip()
    alias = creds.get("recovery_phrase")
    if isinstance(alias, str) and alias.strip():
        return "recovery_phrase", alias.strip()
    return None, ""


# ---------------------------------------------------------------------------
# High-level wrap / resolve — the integration surface for call sites
# ---------------------------------------------------------------------------


def wrap_credentials(creds: dict, *, account: Optional[str] = None) -> dict:
    """Store the mnemonic in the keychain; return creds with a marker.

    On success the returned dict has the mnemonic-bearing field replaced
    by the marker and ``keychain_wrapped`` set to ``True`` (plus every
    other field preserved). On ANY failure — kill-switch armed, no
    backend, store error — the input dict is returned **unchanged**
    (plaintext fallback) and nothing sensitive is recorded. Never raises.
    """
    # #262 review finding 5: idempotence — if the field already carries the
    # keychain marker there is nothing to wrap; re-running store_secret every
    # boot would needlessly repeat the macOS subprocess argv-exposure window.
    for _k in ("mnemonic", "recovery_phrase"):
        if is_marker(creds.get(_k)):
            return dict(creds)
    key, value = _extract_raw(creds)
    if not value:
        return creds
    if is_kill_switch_on() or detect_backend() is None:
        return creds
    if account is None:
        try:
            account = account_for_mnemonic(value)
        except Exception:  # noqa: BLE001 — can't derive account → stay plaintext
            return creds
    try:
        store_secret(account, value)
    except Exception:  # noqa: BLE001 — phrase-safety: never raise on wrap
        return creds
    out = dict(creds)
    out[key] = marker_for(account)
    out["keychain_wrapped"] = True
    return out


def resolve_mnemonic(creds: dict) -> str:
    """Return the real mnemonic for *creds*.

    * Plaintext field → returned as-is (no keychain touch).
    * Marker field → fetched from the keychain.
    * No credential field → ``""``.
    * Marker present but the keychain entry is gone / locked / the
      kill-switch is armed → raises :class:`KeychainEntryMissing` with the
      non-sensitive :data:`MISSING_MESSAGE`.
    """
    _key, value = _extract_raw(creds)
    if not value:
        return ""
    if not is_marker(value):
        return value
    # Marker: we MUST go through the keychain.
    if is_kill_switch_on() or detect_backend() is None:
        raise KeychainEntryMissing(MISSING_MESSAGE)
    account = value[len(MARKER_PREFIX):]
    try:
        return load_secret(account)
    except Exception:  # noqa: BLE001 — entry gone / unavailable → clean error
        raise KeychainEntryMissing(MISSING_MESSAGE)


# ---------------------------------------------------------------------------
# v2 — bundle wrap (Option E Phase 2 / #581). See the module docstring's
# "v2 — bundle wrap" section for the on-disk shape.
# ---------------------------------------------------------------------------


def marker_for_v2(smart_account: str) -> str:
    """Build the keychain *account* identifier for a wrapped bundle.

    Unlike v1's :func:`marker_for` (which returns a value embedded in a
    credentials.json field), this IS the keychain account string passed to
    :func:`store_secret` / :func:`load_secret` — see :func:`wrap_bundle` /
    :func:`unwrap_bundle`.
    """
    return f"{MARKER_PREFIX_V2}{smart_account}"


def is_marker_v2(value: object) -> bool:
    """True iff *value* is a v2 keychain-marker string.

    v2 markers never appear as a credentials.json field value in normal
    operation (see the module docstring), but this exists for the same
    defensive reason :func:`is_marker` does: so a v2 marker accidentally
    handed to v1-shaped code is recognisable rather than silently
    misinterpreted.
    """
    return isinstance(value, str) and value.startswith(MARKER_PREFIX_V2)


def wrap_bundle(smart_account: str, secret_subtree_json: str) -> bool:
    """Store a derived-bundle-v1 secret subtree (``{"vault": …, "signing":
    …}`` as a JSON string) in the OS keychain, keyed on *smart_account*.

    Returns ``True`` on success. Returns ``False`` — never raises — on ANY
    failure: kill-switch armed, no backend, or a store error. On ``False``
    the caller (``hermes/auto_migrate.py``) falls back to writing the
    subtree plaintext into ``credentials.json`` (headless / no-keychain
    host) — see derived-bundle-v1.md §4.3.

    Phrase-safety: *secret_subtree_json* is never logged, printed, or
    embedded in any exception message — mirrors :func:`wrap_credentials`'s
    v1 contract exactly.
    """
    if is_kill_switch_on() or detect_backend() is None:
        return False
    account = marker_for_v2(smart_account)
    try:
        store_secret(account, secret_subtree_json)
    except Exception:  # noqa: BLE001 — phrase-safety: never raise on wrap
        return False
    return True


def unwrap_bundle(smart_account: str) -> str:
    """Return the derived-bundle-v1 secret subtree JSON for *smart_account*
    from the OS keychain.

    Raises :class:`KeychainEntryMissing` — with the non-sensitive
    :data:`MISSING_MESSAGE`, never the payload — when the entry is
    absent/locked, the backend is unavailable, or the kill-switch is
    armed. Mirrors :func:`resolve_mnemonic`'s v1 fail-loud contract: a v2
    caller MUST go through the keychain and never silently substitutes a
    default.
    """
    if is_kill_switch_on() or detect_backend() is None:
        raise KeychainEntryMissing(MISSING_MESSAGE)
    account = marker_for_v2(smart_account)
    try:
        return load_secret(account)
    except Exception:  # noqa: BLE001 — entry gone / unavailable → clean error
        raise KeychainEntryMissing(MISSING_MESSAGE)
