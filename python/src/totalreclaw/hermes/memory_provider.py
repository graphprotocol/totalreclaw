"""TotalReclaw as a Hermes MemoryProvider (Path B — issue #275).

This module exposes :class:`TotalReclawMemoryProvider`, a subclass of
Hermes' ``MemoryProvider`` ABC, so TotalReclaw can be installed as
Hermes' active memory provider alongside its existing generic-plugin
registration (tools + lifecycle hooks).

Architecture
------------

The class is *importable but not auto-loaded* — the generic plugin
``register()`` in :mod:`totalreclaw.hermes.__init__` does not touch it,
so users with another provider configured (Honcho, Byterover, ...) are
unaffected. Activation happens via a one-file sidecar shim dropped at
``$HERMES_HOME/plugins/memory/totalreclaw/__init__.py`` by the
``totalreclaw hermes install-memory-provider`` CLI subcommand. The
sidecar imports this class and registers it through Hermes' standard
``plugins/memory/<name>/`` loader path.

The class itself only ever runs inside the Hermes process, where
``agent.memory_provider.MemoryProvider`` is importable. For unit tests
we also import the real ABC — Hermes ships it as a runtime dependency
so it's present in the test env.

Dedup strategy
--------------

``on_pre_compress`` receives the messages slice Hermes is about to
discard. Many of those have already been auto-extracted by the
``post_llm_call`` hook (every N turns). To avoid double-storing, we
defer to :class:`AgentState`'s existing message-buffer pointer
(``_last_processed_idx``). The state's ``get_unprocessed_messages()``
already filters to "not yet extracted-from", so calling
:func:`auto_extract` is safe: it operates on that slice, calls
``mark_messages_processed()`` on success, and embedding-dedup catches
any near-duplicates that slip through.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base-class resolution
# ---------------------------------------------------------------------------

try:  # Hermes runtime path — present both at install and in CI tests.
    from agent.memory_provider import MemoryProvider as _MemoryProviderBase  # type: ignore[import-not-found]
    _MEMORY_PROVIDER_IMPORTED = True
except Exception:  # pragma: no cover — only taken in stripped-down envs
    _MemoryProviderBase = object  # type: ignore[assignment,misc]
    _MEMORY_PROVIDER_IMPORTED = False


def _summarize_facts_for_compression(fact_texts: List[str]) -> str:
    """Format newly-stored fact texts for injection into Hermes' compression prompt."""
    if not fact_texts:
        return ""

    bullets = "\n".join(f"- {t}" for t in fact_texts if t)
    return (
        "## TotalReclaw — facts stored before compression\n"
        "The following user-relevant facts were extracted from the "
        "conversation window being compressed and persisted to the "
        "TotalReclaw vault (E2E-encrypted, on-chain). The compressed "
        "summary should preserve references to these so later turns can "
        "recall them via `totalreclaw_recall`.\n\n"
        f"{bullets}"
    )


# Mapping of TR tool name → handler in ``totalreclaw.hermes.tools`` or
# ``totalreclaw.hermes.pair_tool``. Used by :meth:`handle_tool_call` to
# dispatch when the model invokes one of our tools through the
# MemoryProvider path (as opposed to the generic-plugin tool path).
_TOOL_HANDLERS = (
    ("totalreclaw_remember", "tools", "remember"),
    ("totalreclaw_recall", "tools", "recall"),
    ("totalreclaw_forget", "tools", "forget"),
    ("totalreclaw_export", "tools", "export_all"),
    ("totalreclaw_status", "tools", "status"),
    ("totalreclaw_pair", "pair_tool", "pair"),
    ("totalreclaw_pin", "tools", "pin"),
    ("totalreclaw_unpin", "tools", "unpin"),
    ("totalreclaw_retype", "tools", "retype"),
    ("totalreclaw_set_scope", "tools", "set_scope"),
    ("totalreclaw_import_from", "tools", "import_from"),
    ("totalreclaw_import_batch", "tools", "import_batch"),
    ("totalreclaw_import_status", "tools", "import_status"),
    ("totalreclaw_import_abort", "tools", "import_abort"),
    ("totalreclaw_upgrade", "tools", "upgrade"),
    ("totalreclaw_debrief", "tools", "debrief"),
)


class TotalReclawMemoryProvider(_MemoryProviderBase):  # type: ignore[misc,valid-type]
    """Hermes ``MemoryProvider`` adapter for TotalReclaw.

    Receives state from the generic plugin (single shared
    :class:`PluginState` per Hermes process) so the existing tool /
    hook registration and this provider observe the same buffer,
    turn counter, and TotalReclaw client.
    """

    def __init__(self, state: Optional["PluginState"] = None) -> None:
        super().__init__()
        if state is None:
            # Default path — Hermes' loader instantiates with no args.
            # Lazy-import to avoid a circular import at module load.
            from .state import PluginState
            state = PluginState()
        self._state = state
        self._session_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Core lifecycle (abstract)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "totalreclaw"

    def is_available(self) -> bool:
        """True iff TotalReclaw credentials are present + parsed.

        Hermes calls this during agent init to decide whether to keep
        the provider active. Must not make network calls per the ABC
        contract — :meth:`PluginState.is_configured` is a pure
        in-memory check.
        """
        return self._state.is_configured()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Per-session init. Matches the generic plugin's session-start path."""
        self._session_id = session_id

        from .hooks import on_session_start

        try:
            on_session_start(self._state, session_id=session_id, **kwargs)
        except Exception as exc:
            logger.warning("TotalReclaw MemoryProvider.initialize failed: %s", exc)

    def shutdown(self) -> None:
        """Best-effort flush at agent exit. Mirrors ``on_session_finalize``."""
        if not self._state.is_configured():
            return

        from .hooks import on_session_finalize

        try:
            on_session_finalize(self._state)
        except Exception as exc:
            logger.warning("TotalReclaw MemoryProvider.shutdown failed: %s", exc)

    # ------------------------------------------------------------------
    # Tool surface
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return TR's canonical tool schemas.

        Single source of truth — these are the exact dicts the generic
        plugin registers via ``ctx.register_tool``.
        """
        from . import schemas
        from . import pair_tool

        return [
            schemas.REMEMBER,
            schemas.RECALL,
            schemas.FORGET,
            schemas.EXPORT,
            schemas.STATUS,
            pair_tool.PAIR_SCHEMA,
            schemas.PIN,
            schemas.UNPIN,
            schemas.RETYPE,
            schemas.SET_SCOPE,
            schemas.IMPORT_FROM,
            schemas.IMPORT_BATCH,
            schemas.IMPORT_STATUS,
            schemas.IMPORT_ABORT,
            schemas.UPGRADE,
            schemas.DEBRIEF,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch a TR tool call. Returns a JSON-encoded result.

        Hermes calls this only for tool names in :meth:`get_tool_schemas`.
        We import handlers lazily so module load stays cheap.
        """
        for name, module, attr in _TOOL_HANDLERS:
            if name == tool_name:
                if module == "tools":
                    from . import tools as _mod
                else:
                    from . import pair_tool as _mod  # type: ignore[no-redef]
                handler = getattr(_mod, attr)
                from totalreclaw.agent.loop_runner import run_sync

                try:
                    result = run_sync(handler(args, self._state, **kwargs))
                except Exception as exc:  # pragma: no cover — surfaces in Hermes logs
                    logger.warning(
                        "TotalReclaw tool %s raised: %s", tool_name, exc
                    )
                    return json.dumps({"error": str(exc)})
                return json.dumps(result) if not isinstance(result, str) else result

        # Coord-review feedback (PR #234): the upstream ``MemoryProvider``
        # ABC does not define ``handle_tool_call`` — that method is
        # TR-side. Delegating to ``super()`` raises ``AttributeError``,
        # not the documented ``NotImplementedError``. Raise the latter
        # directly so callers + tests see a stable, well-typed signal
        # for unknown tools.
        raise NotImplementedError(
            f"TotalReclaw MemoryProvider has no handler for tool: "
            f"{tool_name!r}. Registered tools: "
            f"{[name for name, _, _ in _TOOL_HANDLERS]}"
        )

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Describe TR's setup surface for ``hermes memory setup``.

        TotalReclaw does not need traditional config fields — pairing
        happens via the ``totalreclaw_pair`` chat tool (browser-side
        crypto; phrase never crosses the LLM context). We return an
        empty fields list per the ABC contract for "no config needed".
        """
        return []

    def system_prompt_block(self) -> str:
        """Static text included in the system prompt when TR is active.

        Reminds the agent of TR's role + how the pair flow works, so
        unconfigured users still get pointed at it on first contact.
        """
        if self._state.is_configured():
            return (
                "## TotalReclaw memory\n"
                "TotalReclaw is the active memory provider for this user. "
                "Memories are end-to-end encrypted, on-chain, "
                "and portable across every AI tool the user runs. Prefer "
                "`totalreclaw_remember` / `totalreclaw_recall` for any "
                "user-fact, preference, directive, commitment, or episode."
            )
        return (
            "## TotalReclaw setup pending\n"
            "TotalReclaw is installed as the memory provider but the user "
            "has not yet paired. When the user references memory semantics, "
            "offer to set up TotalReclaw via the `totalreclaw_pair` tool — "
            "DO NOT ask for a recovery phrase in chat (browser-side crypto)."
        )

    # ------------------------------------------------------------------
    # Optional lifecycle hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        """Per-user-turn tick.

        Upstream bug NousResearch/hermes-agent#7193: this hook does not
        fire for some configs. The generic plugin's ``post_llm_call``
        keeps the turn counter accurate as a fallback. To avoid
        double-counting on configs where BOTH hooks fire, we trust the
        caller-supplied ``turn_number`` when valid, otherwise increment.
        """
        if isinstance(turn_number, int) and turn_number >= 0:
            self._state._turn_count = turn_number  # type: ignore[attr-defined]
        else:
            self._state.increment_turn()

    def on_pre_compress(self, messages: Optional[List[Dict[str, Any]]] = None, **kwargs: Any) -> str:
        """Hermes compaction hook — extract facts before context discard."""
        if not self._state.is_configured():
            return ""

        if not self._state.has_unprocessed_messages():
            return ""

        from totalreclaw.agent.lifecycle import auto_extract as _auto_extract
        from .hooks import _get_hermes_llm_config

        try:
            stored = _auto_extract(
                self._state,
                mode="full",
                llm_config=_get_hermes_llm_config(),
            )
        except Exception as exc:
            logger.warning("TotalReclaw on_pre_compress extraction failed: %s", exc)
            return ""

        return _summarize_facts_for_compression(stored)

    def on_session_end(self, messages: Optional[List[Dict[str, Any]]] = None, **kwargs: Any) -> None:
        """Session-boundary hook — flush + debrief.

        Mirrors the generic plugin's ``on_session_finalize``. Safe to
        run alongside it: ``auto_extract`` is idempotent via the
        unprocessed-buffer pointer, and the debrief writes a single
        session-summary fact that's embedding-deduped against any
        debrief written by the parallel hook in the same session.
        """
        if not self._state.is_configured():
            return

        from .hooks import on_session_finalize

        try:
            on_session_finalize(self._state, **kwargs)
        except Exception as exc:
            logger.warning("TotalReclaw on_session_end finalize failed: %s", exc)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror Hermes-side ``add user-fact`` writes into TotalReclaw.

        Fixes the 6-denied-writes finding from Pedro's 2026-05-15 audit:
        Hermes' Background Review runs a separate agent loop with a
        hardcoded ``enabled_toolsets=["memory", "skills"]`` allowlist,
        so it cannot call ``totalreclaw_remember`` directly. This hook
        catches the equivalent writes via the MemoryProvider abstraction
        and routes them through our standard ``remember`` path.

        Only handles ``action="add" target="user"`` writes — other
        action/target combinations (e.g. session-state updates,
        plugin-internal bookkeeping) are not user memories and are
        passed through silently.

        Idempotency: writes go through :func:`tools.remember`, which
        runs cosine-embedding dedup against existing on-chain memories.
        """
        if action != "add" or target != "user":
            return

        if not self._state.is_configured():
            return

        if isinstance(content, str):
            text = content
            extra: Dict[str, Any] = {}
        elif isinstance(content, dict):
            text = str(content.get("text") or "")
            extra = {k: v for k, v in content.items() if k != "text"}
        else:
            return

        if not text or not text.strip():
            return

        args: Dict[str, Any] = {"text": text.strip()}
        # Surface common metadata shapes Hermes might use when the
        # built-in memory tool serializes its write.
        for src_key, dst_key in (
            ("type", "type"),
            ("fact_type", "type"),
            ("scope", "scope"),
            ("importance", "importance"),
        ):
            if src_key in extra and extra[src_key] is not None and dst_key not in args:
                args[dst_key] = extra[src_key]

        from .tools import remember as _remember
        from totalreclaw.agent.loop_runner import run_sync

        try:
            run_sync(_remember(args, self._state))
        except Exception as exc:
            logger.warning(
                "TotalReclaw on_memory_write mirror failed (action=%s target=%s): %s",
                action, target, exc,
            )

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Sub-agent completion hook — v1 no-op stub.

        Routing sub-agent memories into a parent's TotalReclaw vault is
        a separate problem (sub-agent identity, scope inheritance, write
        attribution). See spec §"Out of scope".
        """
        return None
