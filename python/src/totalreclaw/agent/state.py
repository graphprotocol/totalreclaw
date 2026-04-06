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
        """Try to configure from env vars or config file."""
        # Check env var
        mnemonic = os.environ.get("TOTALRECLAW_RECOVERY_PHRASE", "")
        if mnemonic:
            self.configure(mnemonic)
            return

        # Check credentials file
        creds_path = Path.home() / ".totalreclaw" / "credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text())
                mnemonic = creds.get("recovery_phrase", "")
                if mnemonic:
                    self.configure(mnemonic)
            except Exception:
                pass

    def configure(self, mnemonic: str) -> None:
        """Configure the TotalReclaw client with a mnemonic."""
        from totalreclaw.client import TotalReclaw

        relay_url = (
            self._server_url
            or os.environ.get("TOTALRECLAW_SERVER_URL", "https://api.totalreclaw.xyz")
        )
        self._client = TotalReclaw(mnemonic=mnemonic, relay_url=relay_url)

        # Save credentials
        creds_path = Path.home() / ".totalreclaw" / "credentials.json"
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(json.dumps({"recovery_phrase": mnemonic}))
        creds_path.chmod(0o600)

        logger.info("TotalReclaw configured: wallet=%s", self._client.wallet_address)

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
