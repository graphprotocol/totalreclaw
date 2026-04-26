"""Read-after-write primitive for Hermes — confirm a fact id has been indexed
by the subgraph after an on-chain mutation.

Wraps the pure-compute halves exported by ``totalreclaw_core``
(``confirm_indexed_query``, ``confirm_indexed_parse``) in an async polling
loop. The subgraph indexer typically lags 5-30s behind L1 inclusion on
Gnosis production; without this wait, mutation helpers (``pin_fact``,
``unpin_fact``, ``forget_fact``) can return before a follow-up
``client.export()`` / ``client.recall()`` sees the new state.

Mnemonic isolation: this helper never touches the mnemonic, encryption
key, or any decrypted blob. It only reads the public ``{id, isActive,
blockNumber}`` of a fact.

Graceful binding-fallback (rc.23 fix, mirrors ``skill/plugin/confirm-
indexed.ts`` commit ``d9c5352``): the ``confirm_indexed_*`` PyO3 bindings
were added to ``totalreclaw-core`` only in the Rust source slated for
2.3.x. Hermes Python's PyPI floor (``totalreclaw-core>=2.2.0``) means
existing user installs may pull a wheel that DOESN'T export them —
``getattr(_core, 'confirm_indexed_query', None)`` returns ``None`` and a
naive call would raise ``AttributeError``. The chain write itself has
already succeeded before this helper runs, so a missing-bindings case
must surface as ``indexed=False`` (caller flips ``partial=True``), NOT
as an error that fails the whole tool invocation. This module wraps the
binding lookup + first-use in a try/except so callers see a uniform
``ConfirmIndexedResult.indexed=False`` whether the cause is timeout,
indexer transient, or wheel-missing-bindings.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Literal

import totalreclaw_core as _core

from .relay import RelayClient

logger = logging.getLogger(__name__)

ExpectDirection = Literal["active", "inactive"]


@dataclass
class ConfirmIndexedResult:
    """Detailed outcome of a confirm-indexed poll loop.

    Mirrors the TS ``ConfirmIndexedResult`` shape (see
    ``skill/plugin/confirm-indexed.ts``). Returned by
    :func:`confirm_indexed_detailed`. The legacy boolean :func:`confirm_indexed`
    is preserved as a thin wrapper that returns ``result.indexed``.
    """

    indexed: bool
    attempts: int
    elapsed_ms: int
    last_error: Optional[str] = None


def _default_poll_ms() -> int:
    fn = getattr(_core, "confirm_indexed_default_poll_ms", None)
    return int(fn()) if callable(fn) else 1_000


def _default_timeout_ms() -> int:
    fn = getattr(_core, "confirm_indexed_default_timeout_ms", None)
    return int(fn()) if callable(fn) else 30_000


async def confirm_indexed_detailed(
    fact_id: str,
    relay: RelayClient,
    *,
    expect: ExpectDirection = "active",
    poll_interval_ms: Optional[int] = None,
    timeout_ms: Optional[int] = None,
) -> ConfirmIndexedResult:
    """Poll the subgraph until the fact reaches the expected state.

    Returns a :class:`ConfirmIndexedResult` describing the outcome — never
    raises on transient errors or missing PyO3 bindings. The chain write
    has already succeeded before this helper runs, so on
    ``indexed=False`` the caller should surface ``partial=True`` rather
    than failing the whole tool.

    Specifically guards against ``totalreclaw-core`` wheels published
    before the ``confirm_indexed_*`` PyO3 bindings were added (any wheel
    pinned to ``2.2.x`` on PyPI as of rc.22). Without this guard, a naive
    ``_core.confirm_indexed_query()`` call raises::

        AttributeError: module 'totalreclaw_core' has no attribute 'confirm_indexed_query'

    which would bubble to the calling tool (``pin_fact`` / ``forget_fact``
    / ``retype`` / ``set_scope``) and surface to the user as a hard crash
    even though the on-chain write succeeded.
    """
    # Graceful fallback: if the PyO3 bindings aren't on this wheel,
    # short-circuit with indexed=False + a descriptive lastError. The
    # chain write succeeded — confirm step is observational.
    try:
        query = _core.confirm_indexed_query()
        poll_ms = (
            poll_interval_ms if poll_interval_ms is not None else _default_poll_ms()
        )
        total_ms = timeout_ms if timeout_ms is not None else _default_timeout_ms()
    except Exception as exc:
        return ConfirmIndexedResult(
            indexed=False,
            attempts=0,
            elapsed_ms=0,
            last_error=f"confirm-indexed bindings unavailable: {exc}",
        )

    start = asyncio.get_event_loop().time()
    deadline = start + (total_ms / 1000.0)

    attempts = 0
    last_error: Optional[str] = None
    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        try:
            data = await relay.query_subgraph(query, {"id": fact_id})
            # `relay.query_subgraph` returns the parsed JSON envelope.
            # `confirm_indexed_parse` accepts either {data:{fact}} or {fact}.
            import json as _json

            payload = _json.dumps(data)
            is_active = _core.confirm_indexed_parse(payload)
            resolved = is_active if expect == "active" else not is_active
            if resolved:
                logger.debug(
                    "confirm_indexed: fact_id=%s expect=%s resolved after %d attempts",
                    fact_id,
                    expect,
                    attempts,
                )
                elapsed_ms = int(
                    (asyncio.get_event_loop().time() - start) * 1000
                )
                return ConfirmIndexedResult(
                    indexed=True, attempts=attempts, elapsed_ms=elapsed_ms
                )
        except Exception as exc:  # pragma: no cover — best-effort polling
            last_error = str(exc)
            logger.debug("confirm_indexed: poll attempt failed: %s", exc)

        # Sleep before the next attempt — but only if there's still budget.
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_ms / 1000.0, remaining))

    elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    logger.info(
        "confirm_indexed: fact_id=%s expect=%s NOT resolved within %dms (%d attempts)",
        fact_id,
        expect,
        total_ms,
        attempts,
    )
    return ConfirmIndexedResult(
        indexed=False,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        last_error=last_error,
    )


async def confirm_indexed(
    fact_id: str,
    relay: RelayClient,
    *,
    expect: ExpectDirection = "active",
    poll_interval_ms: Optional[int] = None,
    timeout_ms: Optional[int] = None,
) -> bool:
    """Boolean wrapper over :func:`confirm_indexed_detailed`.

    Returns ``True`` when the subgraph confirmed the fact reached the
    expected state inside the timeout budget. Returns ``False`` on
    timeout, transient indexer errors, OR missing PyO3 bindings (the
    rc.23 graceful-fallback case). The chain write is **already
    acknowledged** before this function is called — a ``False`` return
    only means the subgraph is still propagating (or the wheel doesn't
    export the bindings yet), not that the chain write failed. Callers
    should surface this to users as ``partial=True`` rather than as an
    error.
    """
    result = await confirm_indexed_detailed(
        fact_id,
        relay,
        expect=expect,
        poll_interval_ms=poll_interval_ms,
        timeout_ms=timeout_ms,
    )
    return result.indexed
