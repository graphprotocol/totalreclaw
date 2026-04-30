"""
Generic agent state management for TotalReclaw.

Framework-agnostic state class that manages the TotalReclaw client lifecycle,
turn counting, message buffering, billing cache, and extraction configuration.

Used directly by custom agents, or wrapped by framework-specific adapters
(Hermes, LangChain, CrewAI, etc.).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_EXTRACTION_INTERVAL = 3
DEFAULT_MAX_FACTS = 15
DEFAULT_MIN_IMPORTANCE = 6
BILLING_CACHE_TTL = 7200  # 2 hours
STORE_DEDUP_THRESHOLD = 0.85  # Cosine similarity threshold for near-duplicate detection


# v1 env var cleanup — warn once if any removed env var is still set.
# See docs/guides/env-vars-reference.md for the canonical list.
#
# NOTE: ``TOTALRECLAW_SESSION_ID`` was in this list in v2.0.1 and silently
# rejected with a cryptic warning, which broke Axiom log tracing — QA
# skill and internal docs rely on it (see
# docs/notes/QA-V1CLEAN-VPS-20260418.md Bug #1). Restored in v2.0.2: the
# RelayClient now reads it (or the equivalent constructor arg) and
# forwards it to the relay as ``X-TotalReclaw-Session`` on every request.
_REMOVED_ENV_VARS = (
    "TOTALRECLAW_CHAIN_ID",
    "TOTALRECLAW_EMBEDDING_MODEL",
    "TOTALRECLAW_STORE_DEDUP",
    "TOTALRECLAW_LLM_MODEL",
    "TOTALRECLAW_EXTRACTION_MODEL",
    "TOTALRECLAW_TAXONOMY_VERSION",
    "TOTALRECLAW_CLAIM_FORMAT",
    "TOTALRECLAW_DIGEST_MODE",
)

_warned_removed_env_vars = False


def _warn_removed_env_vars_once() -> None:
    global _warned_removed_env_vars
    if _warned_removed_env_vars:
        return
    _warned_removed_env_vars = True
    set_vars = [name for name in _REMOVED_ENV_VARS if os.environ.get(name) is not None]
    if set_vars:
        logger.warning(
            "TotalReclaw: ignoring removed env var(s): %s. "
            "See docs/guides/env-vars-reference.md for the v1 env var surface.",
            ", ".join(set_vars),
        )


_warn_removed_env_vars_once()


# ---------------------------------------------------------------------------
# Bug #7 (Wave 2a, 2026-04-20) — credentials.json key parity.
#
# Plugin 3.2.0 writes ``{"mnemonic": "..."}`` at
# ``~/.totalreclaw/credentials.json``. Python pre-2.2.2 wrote
# ``{"recovery_phrase": "..."}``. Both claim to use the same canonical
# path — so a Hermes user couldn't open OpenClaw with the same vault,
# and vice versa. We now accept BOTH keys on read and emit the canonical
# ``mnemonic`` key on write (matching plugin + MCP). See
# ``docs/specs/totalreclaw/flows/01-identity-setup.md`` for the schema
# addendum.
# ---------------------------------------------------------------------------


def _extract_mnemonic_from_creds(creds: dict) -> str:
    """Pull a plausible mnemonic out of a parsed credentials.json blob.

    Accepts both ``mnemonic`` (canonical, plugin 3.2.0 + Python 2.2.2+)
    and ``recovery_phrase`` (legacy Python) spellings. When both are
    present, ``mnemonic`` wins — matches the plugin-side
    ``extractBootstrapMnemonic`` in ``skill/plugin/fs-helpers.ts``.

    Returns an empty string if neither key carries a non-empty string.
    Never raises — defensive for partial file writes.
    """
    primary = creds.get("mnemonic") if isinstance(creds.get("mnemonic"), str) else ""
    primary = primary.strip() if primary else ""
    if primary:
        return primary
    alias = creds.get("recovery_phrase") if isinstance(creds.get("recovery_phrase"), str) else ""
    alias = alias.strip() if alias else ""
    return alias


class AgentState:
    """Manages TotalReclaw client lifecycle, turn tracking, and message buffer.

    This is the core state class used by all agent integrations. It can be
    used directly by custom agents or wrapped by framework adapters.

    Usage::

        from totalreclaw.agent import AgentState

        state = AgentState(recovery_phrase="abandon abandon ...")
        # or auto-configure from env/credentials:
        state = AgentState()
    """

    def __init__(
        self,
        recovery_phrase: Optional[str] = None,
        server_url: Optional[str] = None,
    ):
        self._client = None
        self._turn_count = 0
        self._messages: list[dict] = []
        self._last_processed_idx = 0
        self._billing_cache: Optional[dict] = None
        self._billing_cache_time = 0.0
        self._extraction_interval = DEFAULT_EXTRACTION_INTERVAL
        self._max_facts = DEFAULT_MAX_FACTS
        self._min_importance = DEFAULT_MIN_IMPORTANCE
        self._quota_warning: Optional[str] = None
        self._server_url = server_url

        # Apply env var overrides (highest priority)
        self._apply_env_overrides()

        # Configure from explicit params or auto-detect
        if recovery_phrase:
            self.configure(recovery_phrase)
        else:
            self._try_auto_configure()

    def _apply_env_overrides(self) -> None:
        """Apply env var overrides for extraction config (highest priority)."""
        interval_env = os.environ.get("TOTALRECLAW_EXTRACT_INTERVAL")
        if interval_env:
            try:
                self._extraction_interval = int(interval_env)
            except ValueError:
                pass

        importance_env = os.environ.get("TOTALRECLAW_MIN_IMPORTANCE")
        if importance_env:
            try:
                self._min_importance = int(importance_env)
            except ValueError:
                pass

        self._env_interval_override = interval_env is not None
        self._env_importance_override = importance_env is not None

    def _try_auto_configure(self) -> None:
        """Try to configure from env vars or config file.

        Accepts the credentials.json blob keyed as EITHER ``mnemonic``
        (canonical as of plugin 3.2.0 + Python 2.2.2) OR
        ``recovery_phrase`` (legacy Python) — Bug #7, QA 2026-04-20.
        Preference order on read: ``mnemonic`` wins when both are
        present so plugin-native files are authoritative.
        """
        # Check env var
        mnemonic = os.environ.get("TOTALRECLAW_RECOVERY_PHRASE", "")
        if mnemonic:
            self.configure(mnemonic)
            return

        # Check credentials file — accept both canonical ``mnemonic`` and
        # legacy ``recovery_phrase`` spellings.
        creds_path = Path.home() / ".totalreclaw" / "credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text())
            except Exception:
                return
            if not isinstance(creds, dict):
                return
            mnemonic = _extract_mnemonic_from_creds(creds)
            if mnemonic:
                self.configure(mnemonic)

    def configure(self, mnemonic: str) -> None:
        """Configure the TotalReclaw client with a mnemonic.

        Write policy (Bug #7 / Wave 2a):
          * If the credentials file already exists with ONLY the legacy
            ``recovery_phrase`` key, leave the legacy format in place —
            don't silently migrate on touch. The user may have external
            tooling reading that key. A full migration happens when the
            user re-onboards (explicit ``totalreclaw_setup`` call).
          * Otherwise (file missing, or already has ``mnemonic``), emit
            the canonical ``{"mnemonic": ...}`` shape. Matches what
            plugin 3.2.0 writes, giving cross-client portability.
        """
        from totalreclaw.client import TotalReclaw
        # 2.3.3-rc.1 — single-source-of-truth default URL resolution.
        # Pull from totalreclaw.relay so the build-time staging/production
        # rewrite at ``_HARDCODED_DEFAULT_URL`` is the only knob.
        from totalreclaw.relay import _default_relay_url

        relay_url = self._server_url or _default_relay_url()
        self._client = TotalReclaw(mnemonic=mnemonic, relay_url=relay_url)

        # Save credentials — preserve legacy shape when present, write
        # canonical shape otherwise.
        creds_path = Path.home() / ".totalreclaw" / "credentials.json"
        creds_path.parent.mkdir(parents=True, exist_ok=True)

        should_preserve_legacy = False
        if creds_path.exists():
            try:
                existing = json.loads(creds_path.read_text())
                if (
                    isinstance(existing, dict)
                    and "mnemonic" not in existing
                    and isinstance(existing.get("recovery_phrase"), str)
                    and existing.get("recovery_phrase", "").strip() == mnemonic.strip()
                ):
                    should_preserve_legacy = True
            except Exception:
                pass

        if should_preserve_legacy:
            # Leave the file untouched. Same mnemonic, same content —
            # no reason to rewrite and risk a partial update.
            pass
        else:
            creds_path.write_text(json.dumps({"mnemonic": mnemonic}))
            creds_path.chmod(0o600)

        # 2.3.2-rc.1 (#192): clear any stale eager-account-register
        # latch attached by ``totalreclaw.hermes.hooks._eager_account_
        # register``. If the user re-pairs (different mnemonic) mid-
        # session, the new SA needs its own first-contact request to
        # the relay; without resetting the latch we'd silently skip
        # account creation for the new SA.
        if hasattr(self, "_eager_account_registered"):
            try:
                delattr(self, "_eager_account_registered")
            except AttributeError:
                pass

        # Do NOT read `self._client.wallet_address` here — it raises until
        # `resolve_address()` has run, and `configure()` is synchronous. The
        # EOA is what we have at this point; the Smart Account address is
        # resolved lazily on the first remember/recall call.
        logger.info("TotalReclaw configured: eoa=%s", self._client._eoa_address)

    def is_configured(self) -> bool:
        """Return True if the client is configured and ready."""
        return self._client is not None

    def get_client(self):
        """Return the TotalReclaw client, or None if not configured."""
        return self._client

    # Turn tracking
    def reset_turn_counter(self) -> None:
        """Reset the turn counter (called at session start)."""
        self._turn_count = 0

    def increment_turn(self) -> None:
        """Increment the turn counter."""
        self._turn_count += 1

    @property
    def turn_count(self) -> int:
        """Current turn count."""
        return self._turn_count

    # Message buffer
    def add_message(self, role: str, content: str) -> None:
        """Add a message to the buffer."""
        if content:
            self._messages.append({"role": role, "content": content})

    def get_unprocessed_messages(self) -> list[dict]:
        """Return messages that haven't been processed for extraction."""
        return self._messages[self._last_processed_idx:]

    def has_unprocessed_messages(self) -> bool:
        """Return True if there are unprocessed messages."""
        return self._last_processed_idx < len(self._messages)

    def mark_messages_processed(self) -> None:
        """Mark all current messages as processed."""
        self._last_processed_idx = len(self._messages)

    def get_all_messages(self) -> list[dict]:
        """Return all messages from the session (processed + unprocessed)."""
        return list(self._messages)

    def clear_messages(self) -> None:
        """Clear all messages and reset the processed index."""
        self._messages.clear()
        self._last_processed_idx = 0

    # Billing cache
    def get_cached_billing(self) -> Optional[dict]:
        """Return cached billing data if still valid."""
        if self._billing_cache and (time.time() - self._billing_cache_time) < BILLING_CACHE_TTL:
            return self._billing_cache
        return None

    def set_billing_cache(self, data: dict) -> None:
        """Cache billing data."""
        self._billing_cache = data
        self._billing_cache_time = time.time()

    # Extraction config
    def get_extraction_interval(self) -> int:
        """Return the extraction interval in turns."""
        return self._extraction_interval

    def get_max_facts_per_extraction(self) -> int:
        """Return the max facts per extraction batch."""
        return self._max_facts

    def get_min_importance(self) -> int:
        """Return the minimum importance threshold for extraction."""
        return self._min_importance

    def update_from_billing(self, billing_status: dict) -> None:
        """Update extraction config from billing endpoint's features dict.

        Server config overrides defaults, but env vars override server config.
        """
        features = billing_status.get("features", {})
        if not features:
            return

        # Only apply server config if no env var override
        if not self._env_interval_override:
            server_interval = features.get("extraction_interval")
            if server_interval is not None:
                try:
                    self._extraction_interval = int(server_interval)
                except (ValueError, TypeError):
                    pass

        server_max_facts = features.get("max_facts_per_extraction")
        if server_max_facts is not None:
            try:
                self._max_facts = int(server_max_facts)
            except (ValueError, TypeError):
                pass

    # Quota warning
    def set_quota_warning(self, warning: str) -> None:
        """Set a quota warning to inject on next pre_llm_call."""
        self._quota_warning = warning

    def get_quota_warning(self) -> Optional[str]:
        """Return the pending quota warning, or None."""
        return self._quota_warning

    def clear_quota_warning(self) -> None:
        """Clear the quota warning after it has been shown."""
        self._quota_warning = None
