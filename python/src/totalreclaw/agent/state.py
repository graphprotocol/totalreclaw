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
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_EXTRACTION_INTERVAL = 3
DEFAULT_MAX_FACTS = 15
DEFAULT_MIN_IMPORTANCE = 6
DEFAULT_RECALL_TOP_K = 16
DEFAULT_MAX_CANDIDATE_POOL = 250
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


def _uuid7() -> str:
    """Generate a UUIDv7 (RFC 9562) — 48-bit ms timestamp + random tail.

    Time-ordered + k-sortable. Stdlib ``uuid.uuid7`` lands in CPython
    3.14; this package's floor is 3.11, so the implementation is
    inlined here. Format: ``xxxxxxxx-xxxx-7xxx-Yxxx-xxxxxxxxxxxx`` with
    version nibble ``7`` and variant nibble in ``{8,9,a,b}``.
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF
    val = (
        (ts_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    h = f"{val:032x}"
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# Fixed namespace for deriving a STABLE per-conversation session id from a host
# identifier (e.g. a Hermes session_id / Telegram chat id). Same host id → same
# UUID for the life of the process, so a conversation's memories share one key.
_SESSION_NAMESPACE = uuid.UUID("b7f3c2a4-0e5d-4a6b-9c1f-2d3e4f5a6b7c")


def _session_id_from_host(external_id: str) -> str:
    """Derive a deterministic UUIDv5 session id from a host-provided id.

    Used so that when the agent host distinguishes conversations (passes a
    distinct id per chat/topic), TotalReclaw inherits that boundary instead of
    minting one global id per process. Deterministic: the same conversation id
    always maps to the same session key.
    """
    return str(uuid.uuid5(_SESSION_NAMESPACE, external_id))


def _session_idle_seconds() -> int:
    """Idle gap (seconds) after which a new turn rolls into a fresh session.

    ``TOTALRECLAW_SESSION_IDLE_MINUTES`` (default 60). ``0`` disables rollover.
    A long silence almost always means a new conversation/topic — rolling keeps
    unrelated bursts from interleaving into one session Crystal when the host
    doesn't distinguish parallel conversations for us.
    """
    raw = os.environ.get("TOTALRECLAW_SESSION_IDLE_MINUTES")
    if raw is None:
        return 60 * 60
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return 60 * 60
    return max(0, minutes) * 60


def _normalize_for_dedup(text: str) -> str:
    """Normalize a message for substring-comparison dedup.

    Lowercases, strips punctuation, collapses whitespace. Output is
    suitable for the F7 manual-vs-auto-extract suppression check —
    NOT for embedding-based semantic similarity, which lives in
    storage-layer dedup. This is a cheap pre-filter at the tool-
    handler boundary.
    """
    import re
    if not text:
        return ""
    # Lowercase + strip leading/trailing whitespace.
    t = text.lower().strip()
    # Drop punctuation. Keep alphanumerics + spaces.
    t = re.sub(r"[^\w\s]", " ", t)
    # Collapse runs of whitespace to single spaces.
    t = re.sub(r"\s+", " ", t).strip()
    return t


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


# Sentinel key for the implicit "default" conversation slot — the live state a
# host uses when it never supplies a per-conversation id (legacy single-session
# behavior). A NUL prefix keeps it disjoint from any real host session id.
_DEFAULT_SLOT_KEY = "\x00default"

# The per-conversation slot = the live AgentState fields that belong to ONE
# conversation. Everything else (client, billing cache, config, quota) is
# process-global and shared across conversations.
_SLOT_FIELDS = (
    "_messages",
    "_last_processed_idx",
    "_turn_count",
    "_session_id",
    "_session_id_from_host",
    "_last_activity",
    "_pending_extract_buffer",
)


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
        self._recall_top_k = DEFAULT_RECALL_TOP_K
        # Server-advertised candidate-pool size (from billing features).
        # ``None`` until update_from_billing populates it; getter falls back
        # to env override then DEFAULT_MAX_CANDIDATE_POOL.
        self._max_candidate_pool_server: Optional[int] = None
        self._quota_warning: Optional[str] = None
        self._server_url = server_url
        self._session_id: Optional[str] = None
        # True when the current session id was derived from a host-provided id
        # (the host distinguishes conversations for us → don't idle-roll it).
        self._session_id_from_host: bool = False
        # monotonic() of the last turn — drives idle-timeout session rollover.
        self._last_activity: float = 0.0
        # Per-conversation session isolation (parallel-chat fix). When the host
        # supplies a per-conversation id every turn (Hermes
        # ``MemoryProvider.sync_turn`` / the per-turn hooks), each conversation
        # gets its OWN slot — message buffer, turn counter, processed index,
        # session id — so interleaved/parallel conversations no longer collapse
        # into one session Crystal. Empty ⇒ legacy single-session behavior: the
        # live attributes above ARE the one and only slot.
        self._session_slots: dict[str, dict] = {}
        self._active_conversation_key: Optional[str] = None
        # 2.4.4rc2 (F7) — auto-extract pending-queue tracking. Each
        # post_llm_call appends an entry for the just-completed turn's
        # user message; the totalreclaw_remember tool consults this
        # buffer to suppress duplicates of content the next auto-
        # extraction batch will capture. Bounded to last 10 entries.
        self._pending_extract_buffer: list[dict] = []
        self._suppressed_manual_writes: int = 0

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

        recall_top_k_env = os.environ.get("TOTALRECLAW_RECALL_TOP_K")
        if recall_top_k_env:
            try:
                val = int(recall_top_k_env)
                if val > 0:
                    self._recall_top_k = val
            except ValueError:
                pass

        max_facts_env = os.environ.get("TOTALRECLAW_MAX_FACTS_PER_EXTRACTION")
        if max_facts_env:
            try:
                val = int(max_facts_env)
                if val > 0:
                    self._max_facts = val
            except ValueError:
                pass

        self._env_interval_override = interval_env is not None
        self._env_importance_override = importance_env is not None
        self._env_recall_top_k_override = recall_top_k_env is not None
        self._env_max_facts_override = max_facts_env is not None

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

        # cred-3 stage 3 — route credential discovery through the
        # credential-provider abstraction so Hermes can boot against an
        # env-var (TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON) or mounted file
        # (TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH) secret manager.
        # TOTALRECLAW_CREDENTIALS_PROVIDER=external switches transports;
        # default 'file' mode is byte-identical to the prior path.
        # Accepts both canonical ``mnemonic`` and legacy ``recovery_phrase``
        # spellings.
        from totalreclaw.credential_provider import get_credential_provider

        creds = get_credential_provider().load()
        if creds is None:
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

        # cred-3 stage 3 — route the credential write through the
        # credential-provider abstraction. In default ``file`` mode this is
        # byte-identical to the prior path. In ``external`` mode the
        # provider is read-only: secret-manager owns the source of truth,
        # so ``provider.save()`` returns ``False`` and we skip the write
        # (the secret already lives in the manager — no point writing to
        # disk and splitting the source of truth).
        from totalreclaw.credential_provider import get_credential_provider

        provider = get_credential_provider()

        should_preserve_legacy = False
        if provider.mode == "file":
            existing = provider.load()
            if (
                isinstance(existing, dict)
                and "mnemonic" not in existing
                and isinstance(existing.get("recovery_phrase"), str)
                and existing.get("recovery_phrase", "").strip() == mnemonic.strip()
            ):
                # Existing file has legacy ``recovery_phrase`` shape and
                # matches the configured mnemonic — leave the file
                # untouched (Bug #7 / Wave 2a write policy).
                should_preserve_legacy = True

        if not should_preserve_legacy:
            # In ``external`` mode this is a no-op (returns False); the
            # secret manager already holds the credentials.
            provider.save({"mnemonic": mnemonic})

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

    # Session id (memq-3)
    #
    # In-memory only — never persisted to disk per the memq spec
    # (encrypted-blob-only). Populated on session start by the hermes
    # ``on_session_start`` hook, cleared on ``on_session_finalize``.
    # Consumers (extraction → Crystal → debrief) tag emissions with
    # this ID so all artefacts from one Hermes session share a key.
    @property
    def session_id(self) -> Optional[str]:
        """Current session UUIDv7, or ``None`` when no session active."""
        return self._session_id

    def start_session(self, external_id: Optional[str] = None) -> str:
        """Start a session, returning its id.

        If *external_id* is given (a host-provided conversation/session id, e.g.
        the ``session_id`` Hermes passes to ``on_session_start``), the session
        key is derived deterministically from it — so when the host distinguishes
        conversations (per Telegram chat/topic), TotalReclaw inherits that
        boundary instead of collapsing every parallel conversation into one id.
        Otherwise a fresh time-ordered UUIDv7 is minted. Repeated calls start a
        new session; the last-activity clock is reset either way.
        """
        if external_id:
            self._session_id = _session_id_from_host(external_id)
            self._session_id_from_host = True
        else:
            self._session_id = _uuid7()
            self._session_id_from_host = False
        self._last_activity = time.monotonic()
        return self._session_id

    def end_session(self) -> None:
        """Clear the session id. Idempotent."""
        self._session_id = None
        self._session_id_from_host = False

    # ------------------------------------------------------------------
    # Per-conversation session slots (parallel-chat fix)
    #
    # The live ``_SLOT_FIELDS`` are the *active* conversation. Any other
    # conversation seen this lifecycle is stashed in ``_session_slots`` keyed
    # by the host's per-conversation id. ``activate_conversation`` swaps the
    # active slot; ``stash_active_conversation`` + ``pop_next_conversation``
    # let a session-finalize sweep crystallize every conversation separately.
    # When the host never supplies a per-conversation id these stay empty and
    # every method below is a no-op → byte-identical legacy behavior.
    # ------------------------------------------------------------------
    def _ensure_slot_store(self) -> None:
        """Lazily initialize the slot store. Defensive: legacy ``AgentState``
        subclasses / test mocks that override ``__init__`` without calling
        ``super().__init__()`` won't have set these, and every slot method must
        tolerate that (behave as a fresh single-session state)."""
        if not hasattr(self, "_session_slots"):
            self._session_slots = {}
        if not hasattr(self, "_active_conversation_key"):
            self._active_conversation_key = None

    def _capture_slot(self) -> dict:
        return {f: getattr(self, f) for f in _SLOT_FIELDS}

    def _restore_slot(self, slot: dict) -> None:
        for f in _SLOT_FIELDS:
            setattr(self, f, slot[f])

    def _blank_live_slot(self) -> None:
        """Reset the live conversation fields to a fresh, empty slot."""
        self._messages = []
        self._last_processed_idx = 0
        self._turn_count = 0
        self._pending_extract_buffer = []
        self._session_id = None
        self._session_id_from_host = False
        self._last_activity = 0.0

    def activate_conversation(self, external_id: Optional[str]) -> None:
        """Route the live per-conversation state to the slot for *external_id*.

        Called once per turn with the host's per-conversation id (Hermes
        ``sync_turn`` / the per-turn hooks pass it). Each distinct id gets its
        own message buffer + turn counter + session id, so parallel/interleaved
        conversations no longer collapse into one session Crystal.

        No-op when *external_id* is falsy (legacy single-session behavior) or
        already the active conversation.
        """
        self._ensure_slot_store()
        if not external_id or external_id == self._active_conversation_key:
            return
        # Stash the currently-live slot so we can come back to it. Skip a still
        # empty legacy/default slot (the coarse, message-less state that
        # ``on_session_start`` set up before the first real turn) — it carries
        # no conversation content worth preserving.
        if self._active_conversation_key is not None:
            self._session_slots[self._active_conversation_key] = self._capture_slot()
        elif self._messages:
            self._session_slots[_DEFAULT_SLOT_KEY] = self._capture_slot()
        # Load the target slot if we've seen it, else mint a fresh one whose
        # session id is derived from the host id (per-conversation, stable).
        existing = self._session_slots.pop(external_id, None)
        if existing is not None:
            self._restore_slot(existing)
        else:
            self._blank_live_slot()
            self._session_id = _session_id_from_host(external_id)
            self._session_id_from_host = True
            self._last_activity = time.monotonic()
        self._active_conversation_key = external_id

    def stash_active_conversation(self) -> None:
        """Move the live conversation into ``_session_slots`` so a finalize
        sweep can iterate every conversation (active + previously stashed).
        Idempotent-ish: leaves the live slot blank afterwards."""
        self._ensure_slot_store()
        key = (
            _DEFAULT_SLOT_KEY
            if self._active_conversation_key is None
            else self._active_conversation_key
        )
        self._session_slots[key] = self._capture_slot()
        self._active_conversation_key = None
        self._blank_live_slot()

    def pop_next_conversation(self) -> bool:
        """Load one stashed conversation as the live slot. Returns False when
        none remain. Drives the per-conversation crystallize loop at finalize."""
        self._ensure_slot_store()
        if not self._session_slots:
            return False
        key, slot = self._session_slots.popitem()
        self._restore_slot(slot)
        self._active_conversation_key = None if key == _DEFAULT_SLOT_KEY else key
        return True

    def reset_conversations(self) -> None:
        """Drop all stashed conversation slots. Called at session boundaries."""
        self._session_slots = {}
        self._active_conversation_key = None

    def find_idle_slots(self, idle_seconds: int) -> list[str]:
        """Keys of STASHED conversation slots idle past *idle_seconds* — the
        conversations that have gone quiet and are ready to crystallize + retire.

        The live/active slot is never returned (it's the conversation currently
        being talked to). Returns ``[]`` when *idle_seconds* <= 0 (disabled) or a
        slot's activity clock was never set. Uses ``time.monotonic()``.
        """
        self._ensure_slot_store()
        if idle_seconds <= 0:
            return []
        now = time.monotonic()
        idle: list[str] = []
        for key, slot in self._session_slots.items():
            last = slot.get("_last_activity", 0.0) or 0.0
            if last > 0 and (now - last) >= idle_seconds:
                idle.append(key)
        return idle

    def sweep_idle_slots(self, idle_seconds: int, finalize_one) -> int:
        """Crystallize + retire every stashed slot idle past *idle_seconds*.

        For each idle slot: load it as the live conversation, call
        ``finalize_one()`` (the caller's per-conversation flush + Crystal), then
        drop it. The currently-live conversation is saved first and restored
        afterward, so the in-progress turn is left exactly as it was. Returns the
        number of slots crystallized. Never raises from the slot bookkeeping;
        ``finalize_one`` is the caller's responsibility to keep safe.
        """
        self._ensure_slot_store()
        idle_keys = self.find_idle_slots(idle_seconds)
        if not idle_keys:
            return 0
        saved = self._capture_slot()
        saved_key = self._active_conversation_key
        swept = 0
        try:
            for key in idle_keys:
                slot = self._session_slots.pop(key, None)
                if slot is None:
                    continue
                self._restore_slot(slot)
                self._active_conversation_key = None if key == _DEFAULT_SLOT_KEY else key
                finalize_one()
                swept += 1
        finally:
            # Always put the live conversation back, even if finalize_one raised.
            self._restore_slot(saved)
            self._active_conversation_key = saved_key
        return swept

    def note_activity(self) -> None:
        """Record that a turn just happened (feeds idle-timeout rollover)."""
        self._last_activity = time.monotonic()

    def should_roll_idle_session(self, idle_seconds: int) -> bool:
        """True when the active session has been idle past *idle_seconds*.

        Only rolls sessions we minted ourselves — a host-derived session id
        means the host owns the conversation boundary, so we never split it on a
        timer. Disabled when ``idle_seconds <= 0`` or no session/activity yet.
        """
        if idle_seconds <= 0:
            return False
        if self._session_id is None or self._session_id_from_host:
            return False
        if self._last_activity <= 0:
            return False
        return (time.monotonic() - self._last_activity) >= idle_seconds

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

    # ------------------------------------------------------------------
    # 2.4.4rc2 (F7) — pending-auto-extract buffer + suppression API.
    # See `plans/2026-05-29-skill-md-enforcement-hooks.md` for full
    # design. The buffer is bounded to the last 10 entries to cap
    # memory. The suppression check compares against the LAST
    # `lookback_turns` (default 3, matches the extraction cadence).
    # ------------------------------------------------------------------
    def track_pending_extract(self, user_message: str) -> None:
        """Record `user_message` for the current turn as pending auto-
        extraction. Called from the ``post_llm_call`` hook regardless
        of whether the every-N-turns extraction fires this turn —
        the entry is what the next batch will process.

        Defensive: tolerates state subclasses / mocks that don't
        initialise ``_pending_extract_buffer`` in their ``__init__``.
        Lazy-create the attribute on first use rather than crash with
        AttributeError. Surfaces the same behaviour for legacy
        ``_FakeState`` test mocks built before this attribute existed.
        """
        if not user_message or not user_message.strip():
            return
        if not hasattr(self, "_pending_extract_buffer"):
            self._pending_extract_buffer = []
        normalized = _normalize_for_dedup(user_message)
        self._pending_extract_buffer.append({
            "turn": getattr(self, "_turn_count", 0),
            "text": user_message.strip(),
            "text_normalized": normalized,
        })
        # Bound buffer to last 10 entries.
        if len(self._pending_extract_buffer) > 10:
            self._pending_extract_buffer = self._pending_extract_buffer[-10:]

    def manual_remember_is_dup_of_pending(
        self,
        text: str,
        lookback_turns: int = 3,
    ) -> bool:
        """Return True if `text` is a near-duplicate of a recent user
        message that auto-extraction will capture. Used by
        ``totalreclaw_remember`` to suppress eager manual writes that
        would race the ``post_llm_call`` extraction pipeline.

        Heuristic: substring-containment of the normalized forms in
        either direction, against entries within `lookback_turns` of
        the current turn count. Embedding similarity would be more
        accurate but adds latency to every manual write; the
        substring check catches the common case (agent paraphrases
        the user's just-spoken sentence).
        """
        if not text or not text.strip():
            return False
        norm = _normalize_for_dedup(text)
        if not norm:
            return False
        if not hasattr(self, "_pending_extract_buffer"):
            return False
        cutoff = getattr(self, "_turn_count", 0) - lookback_turns
        for entry in self._pending_extract_buffer:
            if entry["turn"] < cutoff:
                continue
            other = entry["text_normalized"]
            if not other:
                continue
            # Either direction of containment counts. Catches agent
            # paraphrase-shortening AND verbatim echo.
            if norm in other or other in norm:
                return True
        return False

    def increment_suppressed_writes(self) -> None:
        """Increment the suppressed-manual-writes counter (F7)."""
        if not hasattr(self, "_suppressed_manual_writes"):
            self._suppressed_manual_writes = 0
        self._suppressed_manual_writes += 1

    def get_suppressed_writes_count(self) -> int:
        """Total manual ``totalreclaw_remember`` calls suppressed this
        session because they matched a pending auto-extract entry.
        Surfaced via ``totalreclaw_status`` so users can observe the
        dedup mechanism working."""
        return getattr(self, "_suppressed_manual_writes", 0)

    def clear_pending_extract_buffer(self) -> None:
        """Reset the pending-extract buffer + the suppressed-writes
        counter. Called at session boundaries (``on_session_start``,
        ``on_session_reset``) so the buffer reflects only the active
        session."""
        self._pending_extract_buffer = []
        self._suppressed_manual_writes = 0

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

    def get_recall_top_k(self) -> int:
        """Return the recall top_k limit.

        Precedence (highest first):
          1. Billing-cache value (``features.recall_top_k``) — set by
             ``update_from_billing`` unless overridden by env var.
          2. ``TOTALRECLAW_RECALL_TOP_K`` env var — applied at init by
             ``_apply_env_overrides`` and protected from server override.
          3. ``DEFAULT_RECALL_TOP_K`` (16) — compile-time default.
        """
        return self._recall_top_k

    def get_max_candidate_pool(self) -> int:
        """Return the search candidate-pool size.

        Precedence (highest first), matching the ZeroClaw Rust crate:
          1. Tier-aware env override — ``CANDIDATE_POOL_MAX_PRO`` (pro) or
             ``CANDIDATE_POOL_MAX_FREE`` (free/unknown), resolved at call
             time so the tier from the live billing cache is respected.
          2. Billing-cache value (``features.max_candidate_pool``) — set by
             ``update_from_billing``.
          3. ``DEFAULT_MAX_CANDIDATE_POOL`` (250) — compile-time default.
        """
        tier = (self.get_cached_billing() or {}).get("tier")
        env_name = (
            "CANDIDATE_POOL_MAX_PRO" if tier == "pro" else "CANDIDATE_POOL_MAX_FREE"
        )
        env_val = os.environ.get(env_name)
        if env_val:
            try:
                val = int(env_val)
                if val > 0:
                    return val
            except ValueError:
                pass
        if self._max_candidate_pool_server is not None:
            return self._max_candidate_pool_server
        return DEFAULT_MAX_CANDIDATE_POOL

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

        # Only apply server max_facts if no env var override
        if not self._env_max_facts_override:
            server_max_facts = features.get("max_facts_per_extraction")
            if server_max_facts is not None:
                try:
                    self._max_facts = int(server_max_facts)
                except (ValueError, TypeError):
                    pass

        server_pool = features.get("max_candidate_pool")
        if server_pool is not None:
            try:
                val = int(server_pool)
                if val > 0:
                    self._max_candidate_pool_server = val
            except (ValueError, TypeError):
                pass

        # Only apply server recall_top_k if no env var override
        if not self._env_recall_top_k_override:
            server_recall_top_k = features.get("recall_top_k")
            if server_recall_top_k is not None:
                try:
                    val = int(server_recall_top_k)
                    if val > 0:
                        self._recall_top_k = val
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
