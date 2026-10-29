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

Built-in memory mirror
----------------------

TotalReclaw runs alongside Hermes' built-in memory (``USER.md`` /
``MEMORY.md``), which Hermes always keeps next to the one external
provider. :meth:`TotalReclawMemoryProvider.on_memory_write` keeps the
vault consistent with what the built-in ``memory`` tool commits: adds are
stored, replaces store the new entry and forget the old one, removes forget
the matching fact. A fact is forgotten only on a confident match (see
:func:`_find_mirrored_facts`); anything less is logged and skipped.

Writes Hermes never reports (its background memory review, hand edits) are
caught by :mod:`.memory_reconcile`, queued at ``initialize`` and
``on_session_end`` on the same worker.
"""
from __future__ import annotations

import functools
import json
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Built-in memory mirror
# ---------------------------------------------------------------------------

#: Cosine similarity at or above which a vault fact counts as the same entry
#: as a built-in memory entry whose text it doesn't match exactly. Stricter
#: than store-time dedup (0.85): a wrong match here deletes a memory, a wrong
#: dedup only skips a write.
MIRROR_MATCH_THRESHOLD = 0.92
#: Candidates fetched when looking for the fact behind a built-in entry.
_MIRROR_MATCH_TOP_K = 5
#: Most facts one built-in replace/remove may forget (identical-text copies).
#: More than that is not a match, it's a reason to stop.
_MIRROR_MAX_FORGETS = 3
_MIRROR_QUEUE_MAX = 256
#: ``memory`` = MEMORY.md (agent notes), ``user`` = USER.md (user profile).
_MIRROR_TARGETS = frozenset({"user", "memory"})
#: Hermes 0.22 sends a patch as a replace; older shapes may send it as is.
_MIRROR_ACTIONS = frozenset({"add", "replace", "patch", "remove"})


class _MirrorWorker:
    """One daemon thread that applies mirrored writes in order.

    Hermes calls ``on_memory_write`` inline from the built-in memory tool
    (``MemoryManager.notify_memory_tool_write``), so a mirror that embeds,
    searches and writes on chain there would stall the agent's tool call.
    Jobs run here instead, one at a time so an add and a later replace of the
    same entry stay in order. Daemon, so a wedged relay never blocks exit.
    """

    def __init__(self, maxsize: int = _MIRROR_QUEUE_MAX) -> None:
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue *job*; False when the queue is full (the job is dropped)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="totalreclaw-memory-mirror", daemon=True
                )
                self._thread.start()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception as exc:  # jobs guard themselves; belt and braces
                logger.warning("TotalReclaw memory mirror job failed: %s", exc)
            finally:
                self._queue.task_done()

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until every queued job has run. True when the queue emptied."""
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True


_MIRROR_WORKER = _MirrorWorker()


def _parse_tool_result(result: Any) -> Dict[str, Any]:
    """A ``tools.*`` handler result (JSON string or dict) as a dict."""
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _similarity(result: Any, embedding: List[float]) -> float:
    """Cosine similarity between a recall result and the query embedding."""
    candidate = getattr(result, "embedding", None)
    if candidate:
        try:
            from totalreclaw.reranker import cosine_similarity

            return float(cosine_similarity(embedding, candidate))
        except Exception:
            pass
    return float(getattr(result, "cosine_sim", None) or 0.0)


def _find_mirrored_facts(state: "PluginState", text: str) -> List[str]:
    """IDs of the vault facts that ARE the built-in entry *text*.

    Searches the vault with the entry text (the same recall the
    ``totalreclaw_recall`` tool runs) and accepts, in order:

    1. facts whose text equals the entry once normalized (case, punctuation,
       whitespace): all of them, up to :data:`_MIRROR_MAX_FORGETS`;
    2. otherwise exactly ONE fact with cosine similarity at or above
       :data:`MIRROR_MATCH_THRESHOLD`.

    Anything else (no candidate, two close candidates, too many identical
    copies, no embedding) returns ``[]``: the caller logs and skips rather
    than guess which memory to delete.
    """
    from totalreclaw.agent.loop_runner import run_sync
    from totalreclaw.agent.state import _normalize_for_dedup

    client = state.get_client()
    wanted = _normalize_for_dedup(text)
    if not client or not wanted:
        return []

    embedding = None
    try:
        from totalreclaw.embedding import get_embedding

        embedding = get_embedding(text)
    except Exception as exc:
        logger.debug("memory mirror: embed failed: %s", exc)

    results = run_sync(
        client.recall(
            text,
            query_embedding=embedding,
            top_k=_MIRROR_MATCH_TOP_K,
            max_candidates=state.get_max_candidate_pool(),
        )
    )

    exact = [r.id for r in results if _normalize_for_dedup(r.text or "") == wanted]
    if exact:
        if len(exact) > _MIRROR_MAX_FORGETS:
            logger.warning(
                "memory mirror: %d identical facts match; not forgetting any",
                len(exact),
            )
            return []
        return exact

    if embedding is None:
        return []
    close = [r.id for r in results if _similarity(r, embedding) >= MIRROR_MATCH_THRESHOLD]
    if len(close) == 1:
        return close
    if len(close) > 1:
        logger.info(
            "memory mirror: %d facts are close to the entry; ambiguous, not forgetting",
            len(close),
        )
    return []


def _mirror_remember(state: "PluginState", args: Dict[str, Any]) -> bool:
    """Store a mirrored entry through ``tools.remember``. False on error."""
    from .tools import remember as _remember
    from totalreclaw.agent.loop_runner import run_sync

    result = _parse_tool_result(run_sync(_remember(args, state)))
    if result.get("error"):
        logger.warning("memory mirror: remember failed: %s", result["error"])
        return False
    return True


def _mirror_forget(state: "PluginState", fact_ids: List[str]) -> int:
    """Forget *fact_ids* through ``tools.forget``, one at a time. Returns
    how many were forgotten."""
    from .tools import forget as _forget
    from totalreclaw.agent.loop_runner import run_sync

    forgotten = 0
    for fact_id in fact_ids:
        result = _parse_tool_result(run_sync(_forget({"fact_id": fact_id}, state)))
        if result.get("error") or not result.get("deleted", True):
            logger.warning(
                "memory mirror: forget %s failed: %s",
                fact_id, result.get("error", "not deleted"),
            )
        else:
            forgotten += 1
    return forgotten


def _queue_reconcile(state: "PluginState") -> None:
    """Queue a built-in memory reconcile pass on the mirror worker. Never
    raises, never blocks."""
    try:
        from . import memory_reconcile

        if not memory_reconcile.auto_reconcile_enabled() or not state.is_configured():
            return
        if not _MIRROR_WORKER.submit(functools.partial(_run_reconcile, state)):
            logger.warning("memory reconcile: queue full, pass skipped")
    except Exception as exc:
        logger.warning("memory reconcile: not queued: %s", exc)


def _run_reconcile(state: "PluginState") -> None:
    from .memory_reconcile import reconcile

    try:
        reconcile(state)
    except Exception as exc:
        logger.warning("memory reconcile: pass failed: %s", exc)


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
    ("totalreclaw_top_up", "tools", "top_up"),
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
            # Default path — Hermes' loader instantiates with no args. Use the
            # PROCESS-SHARED state (§5.3) so this provider and the entry-point
            # plugin's tools/hooks observe the same client + buffer + turn
            # counter + `_provider_active` flag. Lazy-import avoids a circular
            # import at module load.
            from .state import get_shared_state
            state = get_shared_state()
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
        """Per-session init + single-driver activation (§5.3).

        Sets the ``_provider_active`` flag on the shared state. From here the
        plugin's ``pre_llm_call``/``post_llm_call`` hooks SKIP recall + extract
        (this provider's ``prefetch``/``sync_turn`` own them) — that's the
        single-driver split that ends the double-fire.

        Session lifecycle (setup, billing, banner, debrief) is DELEGATED to the
        plugin's ``on_session_start``/``on_session_finalize`` hooks, which always
        run on Hermes (entry-point plugin) and are well-tested. So this no longer
        calls ``on_session_start`` itself — doing so would double it.
        """
        self._session_id = session_id
        # Mark this process as provider-driven so the lifecycle hooks defer
        # recall/extract to us. getattr-default-False elsewhere keeps dormant
        # installs (provider never initialized) on the legacy hook path.
        self._state._provider_active = True  # type: ignore[attr-defined]
        # Catch up on built-in memory writes that never reached
        # on_memory_write (background review, hand edits). Queued on the
        # mirror worker: init never waits on the vault.
        _queue_reconcile(self._state)

    def shutdown(self) -> None:
        """Best-effort flush at agent exit.

        Session-end flush/debrief is delegated to the plugin's
        ``on_session_finalize`` hook (single-driver §5.3) — calling it here too
        would double the debrief. No-op beyond that.
        """
        return

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
            schemas.TOPUP,
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

        Sets the two memories' roles (built-in = short always-loaded
        profile, TotalReclaw = long-term store the agent writes to and
        recalls from), so "remember X" goes to ``totalreclaw_remember``
        rather than the built-in tool (the rc.24 intent-stealing NO-GO,
        #167). Injected every turn: keep it short. Unconfigured users get
        pointed at the pair flow instead.
        """
        if self._state.is_configured():
            return (
                "## TotalReclaw memory\n"
                "TotalReclaw is the user's long-term memory: end-to-end "
                "encrypted, on-chain, and portable across every AI tool they "
                "run. Hermes' built-in memory (USER.md / MEMORY.md) is only "
                "the short profile loaded each session. Use "
                "`totalreclaw_remember` to remember and `totalreclaw_recall` "
                "to recall anything about the user. Writes made with the "
                "built-in `memory` tool are copied to TotalReclaw "
                "automatically, so never save the same fact with both."
            )
        return (
            "## TotalReclaw setup pending\n"
            "TotalReclaw is installed as the memory provider but the user "
            "has not yet paired. When the user references memory semantics, "
            "offer to set up TotalReclaw via the `totalreclaw_pair` tool — "
            "DO NOT ask for a recovery phrase in chat (browser-side crypto)."
        )

    # ------------------------------------------------------------------
    # Native auto-memory (provider conformance §5.2 — #351)
    #
    # These are the two methods every other Hermes memory provider
    # (honcho/mem0/supermemory/…) implements; they are what makes the
    # native ``memory`` flow route through TotalReclaw. Both delegate to
    # the shared hook-free entry points in ``hooks.py`` (§5.1), so the
    # provider and the legacy lifecycle hooks share ONE code path.
    #
    # DORMANT until activation (#346 sidecar path + §5.3): the provider is
    # not discovered yet, so these don't run live. §5.3 gates the plugin's
    # ``pre_llm_call``/``post_llm_call`` memory work off when TR is the
    # active provider (single driver) AND reconciles turn counting — at
    # which point ``sync_turn`` (driven every turn via
    # ``memory_manager.sync_all``) becomes the sole turn counter and the
    # ``on_turn_start`` override below is dropped. Landing these now (while
    # dormant) keeps the activation PR small and reviewable.
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Auto-recall context for the upcoming turn (native memory path).

        Delegates to the shared ``hooks.recall_for_query`` — the same recall
        the plugin's ``pre_llm_call`` uses. Returns "" when unconfigured or on
        any error (the manager treats empty as "no context", never fatal).
        """
        if not self._state.is_configured():
            return ""
        try:
            from .hooks import recall_for_query

            return recall_for_query(self._state, query, top_k=self._state.get_recall_top_k()) or ""
        except Exception as exc:  # pragma: no cover — non-fatal per ABC contract
            logger.warning("TotalReclaw MemoryProvider.prefetch failed: %s", exc)
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a completed turn (native auto-extract path).

        Delegates to the shared ``hooks.ingest_turn`` — record the turn +
        interval-gated extraction, the same logic the plugin's
        ``post_llm_call`` runs. Idempotent + safe on errors.
        """
        try:
            from .hooks import ingest_turn

            # Forward the host's per-conversation ``session_id`` so parallel
            # conversations route to separate slots (parallel-chat fix). Hermes
            # passes its per-conversation ``self.agent.session_id`` here every
            # turn; ignoring it is what collapsed every chat into one Crystal.
            ingest_turn(
                self._state,
                user_content,
                assistant_content,
                session_id=session_id,
            )
        except Exception as exc:  # pragma: no cover — non-fatal per ABC contract
            logger.warning("TotalReclaw MemoryProvider.sync_turn failed: %s", exc)

    # ------------------------------------------------------------------
    # Optional lifecycle hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        """Per-user-turn tick — intentionally a counting no-op (§5.3).

        Turn counting is owned solely by ``sync_turn`` (via ``ingest_turn``),
        which Hermes drives every turn through ``memory_manager.sync_all`` —
        reliable, unlike ``on_turn_start`` (upstream NousResearch/hermes-agent
        #7193 doesn't fire it for some configs). Incrementing here too would
        double-count against ``sync_turn``. Kept as a no-op so the ABC hook
        stays satisfied without touching the turn counter.
        """
        return

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
        """Session-boundary hook: queue a built-in memory reconcile pass.

        The pass picks up what Hermes' background memory review wrote to
        USER.md / MEMORY.md during the session (those writes never reach
        ``on_memory_write``). It returns at once when the files haven't
        changed since the last pass.

        Flush + debrief stay delegated to the plugin's ``on_session_finalize``
        hook, which always runs on Hermes (entry-point plugin) and is the
        single, well-tested session-end path. Running it here too would double
        the debrief (§5.3).
        """
        _queue_reconcile(self._state)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Mirror a built-in ``memory`` tool write into TotalReclaw.

        Hermes calls this after the built-in tool commits a write (and only
        then), for either store: ``target="user"`` (USER.md) or
        ``target="memory"`` (MEMORY.md). Declaring ``metadata`` makes Hermes
        pass it by keyword; positional works too. Per action:

        - ``add``: store ``content`` via ``tools.remember`` (which dedups).
        - ``replace`` (Hermes 0.22 also sends ``patch`` as this, with
          ``content`` = the whole edited entry): find the fact for the old
          entry, store the new one, then forget the old fact. Kept if the
          store fails.
        - ``remove``: find the fact for the removed entry and forget it.

        The old entry is ``metadata["previous_content"]`` (the full entry
        Hermes changed) or, from older callers, ``metadata["old_text"]``.
        :func:`_find_mirrored_facts` decides the match conservatively; no
        confident match means nothing is forgotten.

        Runs on Hermes' tool path, so the work is queued on a background
        worker and this returns at once. Never raises.
        """
        try:
            self._queue_memory_mirror(action, target, content, metadata)
        except Exception as exc:
            logger.warning(
                "TotalReclaw on_memory_write skipped (action=%s target=%s): %s",
                action, target, exc,
            )

    def _queue_memory_mirror(
        self,
        action: str,
        target: str,
        content: Any,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        if action not in _MIRROR_ACTIONS or target not in _MIRROR_TARGETS:
            return
        if not self._state.is_configured():
            return

        if isinstance(content, dict):
            text = str(content.get("text") or "")
            extra: Dict[str, Any] = {k: v for k, v in content.items() if k != "text"}
        elif isinstance(content, str):
            text, extra = content, {}
        elif content is None:
            text, extra = "", {}
        else:
            return
        text = text.strip()
        meta = metadata if isinstance(metadata, dict) else {}
        # previous_content is the full old entry; old_text only the search
        # span a legacy caller sent (never a snapshot key).
        exact_previous = bool(meta.get("previous_content"))
        previous = str(meta.get("previous_content") or meta.get("old_text") or "").strip()

        if action == "add":
            if not text:
                return
            args: Dict[str, Any] = {"text": text}
            # Surface common metadata shapes a content dict might carry.
            for src_key, dst_key in (
                ("type", "type"),
                ("fact_type", "type"),
                ("scope", "scope"),
                ("importance", "importance"),
            ):
                if extra.get(src_key) is not None and dst_key not in args:
                    args[dst_key] = extra[src_key]
            work = functools.partial(self._mirror_add, target, args)
        elif action in ("replace", "patch"):
            if not text:
                return
            work = functools.partial(
                self._mirror_replace, target, previous, text, exact_previous
            )
        else:  # remove
            old = previous or text  # a legacy caller may pass the entry as content
            if not old:
                logger.info(
                    "memory mirror: remove (target=%s) carried no entry text; skipped",
                    target,
                )
                return
            work = functools.partial(
                self._mirror_remove, target, old, exact_previous and bool(previous)
            )

        job = functools.partial(self._mirror_job, action, target, work)
        if not _MIRROR_WORKER.submit(job):
            logger.warning(
                "memory mirror: queue full, dropped %s (target=%s)", action, target
            )

    @staticmethod
    def _mirror_job(action: str, target: str, work: Callable[[], Any]) -> None:
        try:
            work()
        except Exception as exc:
            logger.warning(
                "TotalReclaw on_memory_write mirror failed (action=%s target=%s): %s",
                action, target, exc,
            )

    # Each mirror job keeps the reconcile snapshot in step (when there is
    # one), so the next reconcile pass doesn't redo the write, and skips a
    # write a pass already applied. Both run on the mirror worker, in order.

    def _mirror_add(self, target: str, args: Dict[str, Any]) -> None:
        from .memory_reconcile import snapshot_for

        text = args["text"]
        snap = snapshot_for(self._state)
        if snap is not None and snap.exists and snap.has(target, text):
            logger.info("memory mirror: add target=%s already reconciled; skipped", target)
            return
        stored = _mirror_remember(self._state, args)
        if stored and snap is not None and snap.exists:
            snap.add(target, text)
            snap.save()
        logger.info("memory mirror: add target=%s stored=%d forgotten=0", target, int(stored))

    def _mirror_replace(self, target: str, previous: str, text: str, exact_previous: bool = False) -> None:
        from totalreclaw.agent.state import _normalize_for_dedup
        from .memory_reconcile import snapshot_for

        snap = snapshot_for(self._state)
        tracked = snap is not None and snap.exists

        def _record() -> None:
            if tracked:
                if exact_previous:
                    snap.discard(target, previous)
                snap.add(target, text)
                snap.save()

        if tracked and snap.has(target, text) and not (exact_previous and snap.has(target, previous)):
            logger.info("memory mirror: replace target=%s already reconciled; skipped", target)
            return
        if previous and _normalize_for_dedup(previous) == _normalize_for_dedup(text):
            _record()  # whitespace / punctuation-only edit: same fact
            logger.info("memory mirror: replace target=%s stored=0 forgotten=0", target)
            return
        # Look the old fact up BEFORE storing the new one, so the new fact
        # can never be the one matched and forgotten.
        old_ids: List[str] = []
        if previous:
            try:
                old_ids = _find_mirrored_facts(self._state, previous)
            except Exception as exc:  # e.g. reads paused: still store the new entry
                logger.info("memory mirror: old-entry lookup failed: %s", exc)
        if not _mirror_remember(self._state, {"text": text}):
            # Keep the old fact rather than lose both.
            logger.info("memory mirror: replace target=%s stored=0 forgotten=0", target)
            return
        forgotten = _mirror_forget(self._state, old_ids) if old_ids else 0
        if previous and not old_ids:
            logger.info("memory mirror: replace found no confident match for the old entry")
        _record()
        logger.info(
            "memory mirror: replace target=%s stored=1 forgotten=%d", target, forgotten
        )

    def _mirror_remove(self, target: str, previous: str, exact_previous: bool = False) -> None:
        from .memory_reconcile import snapshot_for

        snap = snapshot_for(self._state)
        tracked = snap is not None and snap.exists
        if tracked and exact_previous and not snap.has(target, previous):
            logger.info("memory mirror: remove target=%s already reconciled; skipped", target)
            return
        fact_ids = _find_mirrored_facts(self._state, previous)
        forgotten = _mirror_forget(self._state, fact_ids) if fact_ids else 0
        if not fact_ids:
            logger.info("memory mirror: remove found no confident match; nothing forgotten")
        if tracked and exact_previous:
            snap.discard(target, previous)
            snap.save()
        logger.info("memory mirror: remove target=%s stored=0 forgotten=%d", target, forgotten)

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
