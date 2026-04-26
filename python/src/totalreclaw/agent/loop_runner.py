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

CLI-shutdown resilience (rc.23, finding #1)
-------------------------------------------
QA reference: ``docs/notes/QA-hermes-RC-2.3.1-rc.22-20260426.md``. Under
``hermes chat -q`` (one-shot CLI) the chat process invokes
``on_session_finalize`` (auto-extract + debrief) as part of its own
teardown. By the time the lifecycle hooks reach an internal
``loop.run_in_executor(None, ...)`` (httpx / anyio default-executor
hand-off), Python's global ``concurrent.futures.thread`` shutdown flag
has already been set by an earlier atexit / interpreter-shutdown step,
and every subsequent submit raises::

    RuntimeError: cannot schedule new futures after interpreter shutdown

Auto-extraction, auto-recall, and debrief silently produced ZERO durable
memories for every CLI user (Telegram-mode users were unaffected because
their daemon process never exits). To break the dependency on the global
default executor, the persistent loop now installs its OWN
``ThreadPoolExecutor`` (lifecycle owned by this module) and binds it via
``loop.set_default_executor``. Any call to ``loop.run_in_executor(None,
...)`` inside our coroutines hits OUR executor, which we keep alive
until our own ``atexit`` shutdown runs. ``run_sync`` additionally catches
the specific RuntimeError and surfaces a structured warning so the
failure is no longer silent.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Sentinel substring identifying the post-interpreter-shutdown RuntimeError
# raised by ``concurrent.futures.thread.ThreadPoolExecutor.submit`` once
# Python has set its global ``_shutdown`` flag. Used in :class:`_SyncLoopRunner.run`
# so we can surface a structured warning instead of swallowing it silently.
_INTERPRETER_SHUTDOWN_MARKER = "cannot schedule new futures after interpreter shutdown"


def is_interpreter_shutdown_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` is the post-shutdown ThreadPoolExecutor error.

    The exact message comes from ``concurrent.futures.thread`` and is the
    fingerprint for finding #1 of the rc.22 QA umbrella (issue #148). We
    match the message rather than swallowing every ``RuntimeError`` so
    unrelated runtime errors still propagate.
    """
    if not isinstance(exc, RuntimeError):
        return False
    return _INTERPRETER_SHUTDOWN_MARKER in str(exc)


class _SyncLoopRunner:
    """One-loop, one-thread runner for sync-calls-async bridging.

    Use via :func:`get_sync_loop_runner`.

    Owns a private ``ThreadPoolExecutor`` (set as the loop's default
    executor) so any ``loop.run_in_executor(None, ...)`` call originated
    by httpx / anyio / our own coroutines runs on threads we manage.
    Python's global ``concurrent.futures.thread`` shutdown flag does NOT
    affect a privately-owned executor as long as the executor is still
    alive — the flag only blocks ``submit`` on executors registered in
    ``concurrent.futures.thread._threads_queues``. We register ours, but
    we keep it open until our atexit handler runs (registered first, so
    it runs last LIFO), giving every Hermes ``on_session_finalize`` /
    auto-extract / debrief call a live executor to land on. See the
    module docstring for the full rationale.
    """

    # Number of worker threads in our private executor. anyio's default
    # is min(32, cpu*5); we mirror that with a slightly tighter cap so
    # we don't fork a huge pool just for occasional embedding/HTTP work.
    _EXECUTOR_MAX_WORKERS = 8

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._ready = threading.Event()
        self._inflight_lock = threading.Lock()
        # Tracks futures returned by run_coroutine_threadsafe so the
        # atexit shutdown can drain in-flight calls before stopping the
        # loop. Set rather than list — we only need O(1) add/remove.
        self._inflight: set[concurrent.futures.Future] = set()
        self._start()

    def _make_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Construct a fresh private executor for the loop's default-executor slot.

        Pulled out so we can recreate it transparently if a previous
        instance got swept into Python's global shutdown — see
        :meth:`_recover_executor_if_shutdown`.
        """
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=self._EXECUTOR_MAX_WORKERS,
            thread_name_prefix="totalreclaw-sync-exec",
        )

    def _recover_executor_if_shutdown(self) -> bool:
        """Replace the loop's default executor if the current one is dead.

        Returns ``True`` if a new executor was installed (i.e. a recovery
        happened). Called from :meth:`run_factory` after catching a "cannot
        schedule new futures after interpreter shutdown" RuntimeError so
        a subsequent retry can land. Safe to call concurrently — under
        the inflight lock.
        """
        if self._loop is None:
            return False
        # Build a new executor and bind it before the next attempt. Closing
        # the old one is best-effort; if it already self-shutdown (which is
        # what triggered the recovery in the first place), shutdown() is a
        # no-op.
        new_exec = self._make_executor()
        try:
            self._loop.set_default_executor(new_exec)
        except Exception:
            # Loop may already be stopping. Don't hide the original error.
            new_exec.shutdown(wait=False, cancel_futures=True)
            return False
        old = self._executor
        self._executor = new_exec
        if old is not None:
            try:
                old.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        logger.warning(
            "TotalReclaw sync loop runner: rebuilt private executor after "
            "'cannot schedule new futures after interpreter shutdown'. The "
            "previous executor was swept into Python's global "
            "concurrent.futures shutdown."
        )
        return True

    def _start(self) -> None:
        def _loop_thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            # Bind our private executor as the loop's default. From here on,
            # any ``loop.run_in_executor(None, fn, *args)`` lands on threads
            # we own — Python's global default-executor shutdown can't reach
            # it.
            self._executor = self._make_executor()
            loop.set_default_executor(self._executor)
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
                # Tear down our private executor on the loop side. The
                # outer ``shutdown()`` already did this from the caller
                # thread; double-shutdown is a no-op on ThreadPoolExecutor.
                exec_to_close = self._executor
                if exec_to_close is not None:
                    try:
                        exec_to_close.shutdown(wait=False, cancel_futures=True)
                    except Exception:  # pragma: no cover
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

        On the post-interpreter-shutdown ``RuntimeError`` (the rc.22
        finding #1 fingerprint) the runner cannot replay the coroutine
        (it has already raised), so the original error propagates with a
        structured ``logger.warning`` so the caller sees it. Use
        :meth:`run_factory` for shutdown-resilient invocation.
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

        return self._submit_and_wait(coro)

    def _submit_and_wait(
        self,
        coro: Coroutine[Any, Any, _T],
    ) -> _T:
        """Submit ``coro`` to the persistent loop and block on the result.

        Tracks the future in ``_inflight`` for atexit drain semantics.
        On the post-shutdown RuntimeError, surfaces a structured warning
        and re-raises (callers wanting retry should use
        :meth:`run_factory`).
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("sync loop runner is not started")

        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        with self._inflight_lock:
            self._inflight.add(fut)
        try:
            return fut.result()
        except RuntimeError as exc:
            if is_interpreter_shutdown_error(exc):
                logger.warning(
                    "TotalReclaw sync loop runner: coroutine failed with "
                    "'cannot schedule new futures after interpreter shutdown'. "
                    "Caller did not request retry; the failure surface is "
                    "intentionally loud. Use ``run_sync_resilient`` for "
                    "shutdown-resilient invocation."
                )
            raise
        finally:
            with self._inflight_lock:
                self._inflight.discard(fut)

    def run_factory(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, _T]],
    ) -> _T:
        """Like :meth:`run` but accepts a coroutine FACTORY.

        On the post-interpreter-shutdown RuntimeError (finding #1), the
        runner rebuilds its private executor and calls ``coro_factory()``
        again to produce a fresh coroutine for retry. This is the
        shutdown-resilient entry point preferred by lifecycle hooks
        (auto-extract / auto-recall / debrief).
        """
        if self._loop is None:
            raise RuntimeError("sync loop runner is not started")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:  # pragma: no cover
            raise RuntimeError(
                "_SyncLoopRunner.run_factory called from inside its own "
                "loop; await directly instead."
            )

        first_coro = coro_factory()
        try:
            return self._submit_and_wait(first_coro)
        except RuntimeError as exc:
            if not is_interpreter_shutdown_error(exc):
                raise
            with self._inflight_lock:
                recovered = self._recover_executor_if_shutdown()
            if not recovered:
                # Couldn't rebuild — the loop itself is dead, give up.
                raise
            retry_coro = coro_factory()
            return self._submit_and_wait(retry_coro)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and join the thread.

        Drains in-flight ``run`` calls (up to ``timeout`` seconds total)
        before stopping the loop, so any ``on_session_finalize`` /
        auto-extract / debrief work submitted during process exit gets
        a chance to land on chain instead of being swallowed by the loop
        teardown. Idempotent. Safe to call multiple times.
        """
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return

        # Snapshot in-flight futures and wait for them to settle before
        # stopping the loop. Each ``run_sync`` caller blocks on its own
        # future already, but those callers may be on background threads
        # whose own atexit chain still runs after ours; draining here
        # gives them a deterministic landing window.
        with self._inflight_lock:
            pending = list(self._inflight)
        if pending:
            try:
                concurrent.futures.wait(pending, timeout=timeout)
            except Exception:  # pragma: no cover — best-effort
                pass

        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        # Tear down the private executor explicitly. ``cancel_futures=True``
        # prevents the global Python shutdown from blocking on stragglers.
        executor = self._executor
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # pragma: no cover
                pass
        self._loop = None
        self._thread = None
        self._executor = None


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
            #
            # rc.23 finding #1: the drain semantics in ``shutdown`` give
            # any in-flight auto-extract / debrief calls a deterministic
            # window to land on chain before the loop stops.
            atexit.register(_atexit_shutdown)
    return _singleton


def _atexit_shutdown() -> None:  # pragma: no cover — exit path
    global _singleton
    if _singleton is not None:
        try:
            # rc.23: bump the drain timeout so a slow LLM call mid-flight
            # at process exit still has a chance to land. 5s is a balance
            # between user wait time on Ctrl-C and not silently dropping
            # memory writes.
            _singleton.shutdown(timeout=5.0)
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

    NOTE: ``run_sync`` accepts an already-constructed coroutine and so
    cannot transparently retry on shutdown-induced ``RuntimeError``. For
    shutdown-resilient invocation (lifecycle hooks running during
    process teardown), use :func:`run_sync_resilient` instead.
    """
    return get_sync_loop_runner().run(coro)


def run_sync_resilient(
    coro_factory: Callable[[], Coroutine[Any, Any, _T]],
) -> _T:
    """Shutdown-resilient variant of :func:`run_sync`.

    Accepts a coroutine FACTORY (zero-arg callable that returns a fresh
    coroutine). If the persistent loop's executor was swept into
    Python's global ``concurrent.futures.thread`` shutdown, the runner
    rebuilds its private executor and calls ``coro_factory()`` a second
    time to retry on a clean slate.

    Use this from any code path that may execute during process exit:
    Hermes lifecycle hooks (``on_session_finalize`` debrief +
    auto-extract flush), atexit-driven memory persistence, etc. The
    coroutine factory pattern is necessary because Python coroutines
    cannot be re-awaited once they've started raising.
    """
    runner = get_sync_loop_runner()
    return runner.run_factory(coro_factory)
