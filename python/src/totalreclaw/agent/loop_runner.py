"""Process-wide background event loop for sync hook callers.

QA reference: ``docs/notes/QA-V1CLEAN-VPS-20260418.md``. v2.0.1 had three
user-visible "Event loop is closed" failures:

* ``totalreclaw_status`` (every call)
* ``totalreclaw_export`` (every call)
* ``pre_llm_call`` auto-recall hook (every first turn)

Root cause
----------
Every sync hook and sync tool wrapper built a fresh ``asyncio.new_event_loop()``
for a single ``run_until_complete`` and then closed it. That worked for the
*first* call, but inside ``client.recall`` / ``client.status`` an
``httpx.AsyncClient`` gets cached on the RelayClient for reuse. ``httpx``
binds to whichever loop was running when it was constructed. The second sync
call spun up a *new* loop, reused the cached httpx client, and blew up with
``RuntimeError: Event loop is closed`` the moment the anyio/httpcore pool
tried to schedule anything on the now-dead loop.

Fix
---
One loop, one thread, for the entire process lifetime. All sync callers go
through ``get_sync_loop_runner().run(coro)`` which schedules the coroutine
on the persistent loop via ``asyncio.run_coroutine_threadsafe``. The httpx
client is thus always constructed on (and reused from) the same loop, so
nothing can ever get orphaned.

The loop is lazily created on first use. The thread is a daemon, so it
shuts down when the host process exits. Explicit shutdown is available via
``shutdown_sync_loop_runner`` for tests that want to assert deterministic
teardown.

Thread-safety
-------------
``get_sync_loop_runner`` is protected by a module lock so concurrent
importers can't race on the first call. Once installed, the runner itself
is read-only state; ``run`` is safe from any thread because
``asyncio.run_coroutine_threadsafe`` handles the cross-thread hand-off.

Not a general-purpose asyncio host
----------------------------------
This module is intentionally narrow. It exists so that sync hook callbacks
(Hermes ``pre_llm_call``, ``post_llm_call``, etc.) and sync tool shims can
drive async code without the event-loop-is-closed trap. If you are already
inside an event loop, don't use this — just ``await`` directly.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from typing import Any, Awaitable, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class _SyncLoopRunner:
    """One-loop, one-thread runner for sync-calls-async bridging.

    Use via :func:`get_sync_loop_runner`.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start()

    def _start(self) -> None:
        def _loop_thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                # Drain any pending tasks so we don't leak warnings on shutdown.
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:  # pragma: no cover — best-effort drain
                    pass
                loop.close()

        t = threading.Thread(
            target=_loop_thread_main,
            name="totalreclaw-sync-loop",
            daemon=True,
        )
        t.start()
        # Block until the loop is actually running — otherwise .run() races.
        self._ready.wait(timeout=5.0)
        if self._loop is None:  # pragma: no cover — sanity
            raise RuntimeError("TotalReclaw sync loop failed to start within 5s")
        self._thread = t

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run a coroutine on the persistent loop and return its result.

        Blocks the calling thread until the coroutine completes. Propagates
        exceptions from the coroutine synchronously.

        Must not be called from inside the loop's own thread.
        """
        if self._loop is None:  # pragma: no cover — sanity, _start blocks
            raise RuntimeError("sync loop runner is not started")

        # Guard against accidental self-scheduling (would deadlock).
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:  # pragma: no cover
            raise RuntimeError(
                "_SyncLoopRunner.run called from inside its own loop; "
                "use await directly instead."
            )

        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and join the thread.

        Idempotent. Safe to call multiple times. Not usually needed — the
        thread is a daemon and goes away with the process.
        """
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        self._loop = None
        self._thread = None


_singleton: Optional[_SyncLoopRunner] = None
_singleton_lock = threading.Lock()


def get_sync_loop_runner() -> _SyncLoopRunner:
    """Return the process-wide :class:`_SyncLoopRunner`, creating it once.

    The runner is lazily started on first call. All subsequent calls from
    any thread return the same instance.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = _SyncLoopRunner()
            # Clean shutdown on interpreter exit. This is best-effort — the
            # thread is a daemon so it'll die with the process anyway, but
            # the explicit stop avoids "event loop is running during shutdown"
            # warnings on some platforms.
            atexit.register(_atexit_shutdown)
    return _singleton


def _atexit_shutdown() -> None:  # pragma: no cover — exit path
    global _singleton
    if _singleton is not None:
        try:
            _singleton.shutdown(timeout=1.0)
        except Exception:
            pass
        _singleton = None


def shutdown_sync_loop_runner() -> None:
    """Test-visible shutdown for deterministic teardown."""
    global _singleton
    if _singleton is not None:
        _singleton.shutdown()
        _singleton = None


def run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Convenience wrapper: run a coroutine on the persistent loop.

    Short alias for ``get_sync_loop_runner().run(coro)``. Preferred by
    sync callers that want a one-liner.
    """
    return get_sync_loop_runner().run(coro)
