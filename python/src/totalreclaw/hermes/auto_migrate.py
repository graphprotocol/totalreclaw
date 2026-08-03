"""hermes/auto_migrate.py — transparent local phrase -> derived-bundle-v1
migration (Option E Phase 2 / #581, phase2-implementation-spec.md P2-9 —
the cred-10 leaf that was never built).

Called from ``hermes/__init__.py::register()`` on every plugin load, after
``maybe_emit_welcome``, best-effort — :func:`maybe_migrate` must **never**
raise out to the caller; any failure leaves the install on its existing
credential path untouched.

Flow
----
1. Acquire ``~/.totalreclaw/.migration.lock``; a lock older than 5 minutes
   is treated as stale (a prior process crashed mid-migration) and broken.
2. Read ``credentials.json``; ``version == 2`` -> already migrated, no-op.
3. Resolve the mnemonic (plaintext field, or the v1 keychain marker).
   Unreadable/corrupt credentials, or a keychain entry that's gone/locked,
   -> abort, leave the file untouched, log a clear (non-sensitive) message.
4. ``derive_bundle_from_mnemonic(mnemonic, chain_id=100,
   provisioned_by="local-migration", smart_account=...)``. The Smart
   Account is read from a cached ``scope_address``/``wallet_address``
   field when present (avoids a network round-trip on every boot,
   mirroring ``hermes/cli.py::_try_eager_resolve_scope_address``'s
   cache-then-RPC pattern); otherwise derived via the same
   ``asyncio.run`` / nested-loop-fallback pattern that function uses.
5. Wrap ``{vault, signing}`` via ``credentials_wrap.wrap_bundle`` — on ANY
   failure (kill-switch, no backend, store error) the v2 file instead
   carries the full bundle in plaintext (headless shape, mode 0600) rather
   than aborting: a keychain-less host is a *supported* configuration, not
   a failure state.
6. Rename ``credentials.json`` -> ``credentials.json.bak`` (mode 0600).
7. Atomic write of the v2 file: ``.tmp`` + ``fsync`` + ``rename``. A
   failure here restores ``credentials.json`` from ``.bak`` so the install
   never ends up in a state with neither file present.
8. Best-effort zero the local ``mnemonic`` variable (Python cannot
   guarantee this — strings are immutable and may have been copied
   internally by the interpreter or GC-compacted before this point; ``del``
   is a gesture, not a hard guarantee). The v1 keychain entry, if the
   original credential was v1-keychain-wrapped, is **retained** — see
   "Root retention" below — this is NOT the "drop the v1 entry" behaviour
   an earlier revision of this module had; that was a bug (see 2026-08-03
   fix note).
9. If (and only if) the v2 entry reads back correctly: emit the
   source-aware one-time user notice (exact wording below — do not
   paraphrase) and report success. A read-back failure is reported as a
   **failure** — no success notice, ``maybe_migrate()`` returns ``False``
   — even though the v2 file was written; both the ``.bak`` and (where
   applicable) the v1 keychain entry stay in place, so nothing about the
   root is lost even when this defensive check trips. Release the lock.

Root retention — do not add a sunset mechanism here
-----------------------------------------------------
Two independent backups of the root are kept, both **unconditionally**,
both for **one release cycle**, per Pedro's RESOLVED ruling
(``docs/plans/2026-08-02-option-e-phase2-backward-compat.md``, RESOLVED
section):

- ``credentials.json.bak`` — the renamed original file (step 6).
- **The v1 OS keychain entry**, when the original credential was
  v1-keychain-wrapped (2026-08-03 fix — see below).

**2026-08-03 fix note.** An earlier revision of this module treated
``.bak`` as sufficient recovery cover for every migration source and
called ``credentials_wrap.delete_secret`` on the v1 keychain entry after
a verified migration. That was correct for a *plaintext-sourced* install
(``.bak`` literally contains the phrase) but wrong for a
*keychain-sourced* one: `.bak` there is just a rename of the OLD
``credentials.json``, which for a keychain-wrapped install contains only
the ``__keychain__:v1:<eoa>`` **marker**, never the phrase itself. Combined
with deleting the v1 keychain entry (the phrase's ONLY location), that
left keychain-sourced users with **no recoverable copy of their root
anywhere** — while ``ONE_TIME_NOTICE`` told them ``.bak`` was their
recovery path, which was false for exactly this population. Fixed by (a)
never deleting the v1 keychain entry — it is retained on the same
one-release-cycle policy as ``.bak``, and (b) two notice variants,
selected by migration source, so a keychain-sourced user is told the
truth: their phrase is recoverable from the OS keychain, not from
``.bak``.

The change to immediate destruction of EITHER backup is a **conscious,
evidence-gated one-line edit in a LATER release**, tracked at
p-diogo/totalreclaw#579, which explicitly says: *do not automate this
transition*. There is deliberately no flag, timer, or version check
controlling either retention in this module — do not add one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Opt-out — same truthy-value parsing as credentials_wrap.ENV_NO_KEYCHAIN
#: (kept as a separate constant/set rather than importing the private
#: credentials_wrap._KILL_ON, but MUST stay in sync with it).
ENV_NO_AUTO_MIGRATE = "TOTALRECLAW_NO_AUTO_MIGRATE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: A lock older than this is treated as abandoned by a crashed process.
_STALE_LOCK_SECONDS = 5 * 60

_MIGRATION_CHAIN_ID = 100  # Gnosis mainnet — single-chain policy.
_PROVISIONED_BY = "local-migration"

#: Exact wording for a PLAINTEXT-sourced migration — the user-facing
#: consequence of the whole phase. Do not paraphrase
#: (phase2-implementation-spec.md P2-9). ``.bak`` genuinely contains the
#: phrase for this source, so it is the correct thing to point at.
ONE_TIME_NOTICE = (
    "Your recovery phrase has been replaced on this machine with derived "
    "vault credentials, and is no longer stored here. Your phrase is "
    "still your only recovery path — make sure you have your written "
    "copy. A backup of the previous credentials file is at "
    "~/.totalreclaw/credentials.json.bak; delete it once you have "
    "confirmed your phrase backup."
)

#: Exact wording for a v1-KEYCHAIN-sourced migration (2026-08-03 fix).
#: ``.bak`` here is a rename of the old credentials.json, which for a
#: keychain-wrapped install held only the ``__keychain__:v1:<eoa>``
#: marker — never the phrase. The phrase's only remaining copy on this
#: host is the retained v1 OS keychain entry, so the notice must point
#: there instead, not at ``.bak``.
ONE_TIME_NOTICE_KEYCHAIN_SOURCE = (
    "Your recovery phrase has been replaced on this machine with derived "
    "vault credentials, and credentials.json no longer references it. "
    "Your phrase itself remains in this machine's OS keychain (under the "
    "prior totalreclaw / __keychain__:v1: entry) as a one-release-cycle "
    "recovery backup — it is not in credentials.json.bak, which never "
    "held your phrase to begin with. Your phrase is still your only "
    "long-term recovery path — make sure you have your written copy."
)


def is_disabled() -> bool:
    """True when ``TOTALRECLAW_NO_AUTO_MIGRATE`` is set to a truthy value."""
    return os.environ.get(ENV_NO_AUTO_MIGRATE, "").strip().lower() in _TRUTHY


def _totalreclaw_dir() -> Path:
    d = Path.home() / ".totalreclaw"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _credentials_path() -> Path:
    from totalreclaw.credential_provider import _resolve_credentials_path

    return _resolve_credentials_path()


def _lock_path() -> Path:
    return _totalreclaw_dir() / ".migration.lock"


class _MigrationLock:
    """A simple exclusive file lock with a stale-lock breaker.

    A bare ``O_CREAT | O_EXCL`` create is portable across the platforms
    this project supports and sufficient for a single-writer-per-host
    advisory lock — no ``fcntl``/platform-specific locking needed.
    """

    def __init__(self, path: Path):
        self._path = path
        self._acquired = False

    def acquire(self) -> bool:
        try:
            if self._path.exists():
                age = time.time() - self._path.stat().st_mtime
                if age > _STALE_LOCK_SECONDS:
                    logger.warning(
                        "auto_migrate: breaking a stale migration lock (age=%.0fs)",
                        age,
                    )
                    self._path.unlink(missing_ok=True)
        except OSError:
            pass

        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            self._acquired = True
            return True
        except (FileExistsError, OSError):
            return False

    def release(self) -> None:
        if self._acquired:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            self._acquired = False


def maybe_migrate() -> bool:
    """Best-effort local phrase -> derived-bundle-v1 migration.

    Returns ``True`` iff a migration actually happened this call.
    ``False`` covers every other case — already v2, opted out via
    ``TOTALRECLAW_NO_AUTO_MIGRATE``, another process holds the lock, no
    credentials to migrate, or any failure (see the module doc's
    abort-cleanly-rather-than-half-migrate contract). **Never raises** —
    this is called from plugin load and must not crash it.
    """
    if is_disabled():
        return False

    try:
        return _maybe_migrate_inner()
    except Exception:  # noqa: BLE001 — must never crash plugin load
        logger.debug(
            "auto_migrate: unexpected error, leaving install untouched",
            exc_info=True,
        )
        return False


def _maybe_migrate_inner() -> bool:
    lock = _MigrationLock(_lock_path())
    if not lock.acquire():
        logger.debug(
            "auto_migrate: another process holds the migration lock, skipping"
        )
        return False
    try:
        return _do_migrate(_credentials_path())
    finally:
        lock.release()


def _resolve_smart_account(mnemonic: str, creds: dict) -> Optional[str]:
    """Smart Account address for the bundle's ``account.smart_account``.

    Prefers a cached value already on disk (``scope_address`` — the field
    ``hermes/cli.py::_try_eager_resolve_scope_address`` writes — falling
    back to the legacy ``wallet_address`` key) to avoid a network round
    trip on every boot. Falls back to the same RPC derivation that
    function uses, with the same ``asyncio.run`` / nested-loop fallback
    (this function runs from a synchronous call site — plugin ``register()``).
    """
    cached = creds.get("scope_address") or creds.get("wallet_address")
    if isinstance(cached, str) and cached.strip():
        return cached.strip()

    from totalreclaw.client import _derive_smart_account_address

    try:
        return asyncio.run(_derive_smart_account_address(mnemonic))
    except RuntimeError:
        # Already inside a running event loop (e.g. a programmatically
        # driven host) — asyncio.run refuses to nest. Spin a fresh loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_derive_smart_account_address(mnemonic))
        finally:
            loop.close()
    except Exception:  # noqa: BLE001 — network failure is non-fatal here
        logger.debug(
            "auto_migrate: Smart Account RPC derivation failed", exc_info=True
        )
        return None


def bundle_secret_subtree_json(bundle) -> str:
    """The ``{vault, signing}`` JSON subtree — the part that is secret and
    either keychain-wrapped or (headless) written plaintext into the v2
    file. Field order matches derived-bundle-v1.md §4.1's ``vault``/
    ``signing`` listings.

    Public (not module-private) — also reused by
    ``pair/completion_sidecar.py`` (P2-10), which needs the identical
    wrap-or-inline persistence shape for a freshly pair-completed bundle.
    """
    return json.dumps(
        {
            "vault": {
                "encryption_key": bundle.vault.encryption_key.hex(),
                "dedup_key": bundle.vault.dedup_key.hex(),
                "auth_key": bundle.vault.auth_key.hex(),
                "lsh_seed": bundle.vault.lsh_seed.hex(),
            },
            "signing": {
                "kind": bundle.signing.kind,
                "private_key": bundle.signing.private_key.hex(),
                "address": bundle.signing.address,
            },
        }
    )


def v2_credentials_dict(bundle, *, keychain_wrapped: bool) -> dict:
    """The full ``credentials.json`` shape for a freshly derived bundle.

    ``keychain_wrapped=True`` -> discovery-metadata only (secret subtree
    lives in the keychain). ``keychain_wrapped=False`` -> the complete
    object, including the secret ``vault``/full ``signing`` (headless —
    derived-bundle-v1.md §4.3).
    """
    base = {
        "version": 2,
        "schema": "derived-bundle-v1",
        "account": {
            "smart_account": bundle.account.smart_account,
            "chain_id": bundle.account.chain_id,
        },
        "provisioned_at": bundle.provisioned_at,
        "provisioned_by": bundle.provisioned_by,
    }
    if keychain_wrapped:
        base["keychain_wrapped"] = True
        base["signing"] = {"kind": bundle.signing.kind, "address": bundle.signing.address}
        return base
    base["vault"] = {
        "encryption_key": bundle.vault.encryption_key.hex(),
        "dedup_key": bundle.vault.dedup_key.hex(),
        "auth_key": bundle.vault.auth_key.hex(),
        "lsh_seed": bundle.vault.lsh_seed.hex(),
    }
    base["signing"] = {
        "kind": bundle.signing.kind,
        "private_key": bundle.signing.private_key.hex(),
        "address": bundle.signing.address,
    }
    return base


def _do_migrate(creds_path: Path) -> bool:
    from totalreclaw import credentials_wrap
    from totalreclaw.bundle import derive_bundle_from_mnemonic, parse_bundle_v1

    # Step 2: version==2 -> already migrated, no-op.
    if not creds_path.exists():
        return False
    try:
        raw = creds_path.read_text()
        creds = json.loads(raw)
    except (OSError, ValueError):
        logger.warning(
            "auto_migrate: credentials.json unreadable/corrupt — leaving "
            "the install untouched rather than risk a half-migration."
        )
        return False
    if not isinstance(creds, dict):
        return False
    if creds.get("version") == 2:
        return False

    # Step 3: resolve the mnemonic. Track whether it came from a v1
    # keychain marker — the source determines which one-time notice we
    # show at the end (2026-08-03 fix — see the module doc's "Root
    # retention" section). The v1 keychain entry is NEVER deleted by this
    # function.
    original_field_value = creds.get("mnemonic") or creds.get("recovery_phrase")
    was_v1_keychain_marker = credentials_wrap.is_marker(original_field_value)

    try:
        mnemonic = credentials_wrap.resolve_mnemonic(creds)
    except credentials_wrap.KeychainEntryMissing as err:
        logger.warning("auto_migrate: %s", str(err))
        return False
    if not mnemonic:
        # Nothing to migrate — e.g. an install running purely off
        # TOTALRECLAW_RECOVERY_PHRASE or an external-provider payload
        # never reaches this file-based path at all.
        return False

    # Step 4: derive the bundle.
    smart_account = _resolve_smart_account(mnemonic, creds)
    if not smart_account:
        logger.warning(
            "auto_migrate: could not resolve the Smart Account address "
            "(no cached value, RPC unreachable) — leaving the install on "
            "the legacy path; will retry on the next boot."
        )
        return False

    try:
        bundle = derive_bundle_from_mnemonic(
            mnemonic, _MIGRATION_CHAIN_ID, _PROVISIONED_BY, smart_account
        )
    except ValueError as err:
        # Exception-class-only logging — never exc_info=True here. A
        # ValueError from bundle derivation / validation can echo back
        # fragments of the JSON or hex it was parsing (core's error
        # messages are diagnostic, not phrase-safety-reviewed), and a full
        # traceback risks the local variables in this frame (which
        # includes `mnemonic`) leaking into the log record.
        logger.warning(
            "auto_migrate: bundle derivation failed (%s) — leaving the "
            "install untouched.",
            type(err).__name__,
        )
        return False

    # Step 5: wrap {vault, signing} via the keychain; plaintext fallback.
    secret_subtree = bundle_secret_subtree_json(bundle)
    wrapped_ok = credentials_wrap.wrap_bundle(bundle.account.smart_account, secret_subtree)
    v2_creds = v2_credentials_dict(bundle, keychain_wrapped=wrapped_ok)

    # Step 6: rename credentials.json -> credentials.json.bak.
    bak_path = creds_path.parent / f"{creds_path.name}.bak"
    try:
        os.replace(creds_path, bak_path)
    except OSError as err:
        logger.warning(
            "auto_migrate: failed to back up credentials.json (%s) — "
            "aborting, install stays on the legacy path.",
            type(err).__name__,
        )
        return False
    try:
        os.chmod(bak_path, 0o600)
    except OSError:
        pass

    # Step 7: atomic write of the v2 file. Created with mode 0600 from the
    # FIRST byte via os.open(O_CREAT|O_WRONLY|O_TRUNC, 0o600) — no window
    # where the tmp file (which, in the non-keychain-wrapped/headless
    # case, contains the full secret {vault, signing} subtree in
    # plaintext) is readable at the default umask-derived mode before a
    # later chmod fixes it. os.replace() preserves the source inode's
    # mode, so the final credentials.json inherits 0600 immediately too —
    # no separate chmod needed after the rename. A failure here restores
    # from .bak so the install never ends up with neither file present.
    tmp_path = creds_path.parent / f"{creds_path.name}.tmp"
    try:
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # takes ownership of fd
            f.write(json.dumps(v2_creds, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, creds_path)
    except OSError as err:
        logger.warning(
            "auto_migrate: failed to write v2 credentials (%s) — "
            "restoring from .bak.",
            type(err).__name__,
        )
        try:
            os.replace(bak_path, creds_path)
        except OSError:
            logger.error(
                "auto_migrate: FAILED to restore credentials.json from "
                ".bak — manual recovery needed at %s",
                bak_path,
            )
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    # Step 8: read the v2 entry back and re-validate before touching
    # anything irreversible (zeroing the local mnemonic reference, or
    # claiming success). Never trust an unverified write.
    try:
        reread = creds_path.read_text()
        reparsed = json.loads(reread)
        if reparsed.get("keychain_wrapped"):
            reunwrapped = credentials_wrap.unwrap_bundle(bundle.account.smart_account)
            full = {
                "version": reparsed.get("version"),
                "schema": reparsed.get("schema"),
                "vault": json.loads(reunwrapped).get("vault"),
                "signing": json.loads(reunwrapped).get("signing"),
                "account": reparsed.get("account"),
                "provisioned_at": reparsed.get("provisioned_at"),
                "provisioned_by": reparsed.get("provisioned_by"),
            }
            parse_bundle_v1(json.dumps(full))
        else:
            parse_bundle_v1(reread)
        v2_confirmed = True
    except Exception as err:  # noqa: BLE001 — read-back verification is defensive
        # Exception-class-only logging (same rationale as the bundle-
        # derivation catch above) — this is on the bundle-JSON-handling
        # path and a ValueError here can echo parsed fragments.
        logger.warning(
            "auto_migrate: v2 credential read-back verification failed "
            "(%s) — the migration wrote a file but could not confirm it "
            "reads back correctly. Reporting FAILURE: .bak and (if "
            "applicable) the v1 keychain entry are both retained; no "
            "success notice is shown.",
            type(err).__name__,
        )
        v2_confirmed = False

    # Best-effort zero of the local mnemonic (Python cannot guarantee
    # this — see the module doc).
    del mnemonic

    if not v2_confirmed:
        # Minor fix (2026-08-03): a read-back failure MUST be reported as
        # a failure — no success notice, no `return True`. The v2 file is
        # left in place (already written; rolling it back risks
        # compounding the inconsistency further), but nothing here claims
        # the migration succeeded, and both backups (.bak, v1 keychain
        # entry) stay untouched.
        return False

    # Step 9: one-time user notice — source-aware (2026-08-03 fix). The
    # v1 keychain entry is deliberately NEVER deleted by this function —
    # see "Root retention" in the module doc.
    if was_v1_keychain_marker:
        print(ONE_TIME_NOTICE_KEYCHAIN_SOURCE)
    else:
        print(ONE_TIME_NOTICE)
    logger.info(
        "auto_migrate: migrated to derived-bundle-v1 (smart_account=%s, "
        "keychain_wrapped=%s, source=%s)",
        bundle.account.smart_account,
        wrapped_ok,
        "v1-keychain" if was_v1_keychain_marker else "plaintext",
    )
    return True
