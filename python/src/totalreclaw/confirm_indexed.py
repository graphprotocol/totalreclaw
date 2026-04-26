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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Literal

import totalreclaw_core as _core

from .relay import RelayClient

logger = logging.getLogger(__name__)

ExpectDirection = Literal["active", "inactive"]


# Inline mirror of rust/totalreclaw-core/src/confirm.rs::FACT_BY_ID_INDEXED_QUERY.
# Used when the installed core wheel pre-dates the confirm_indexed PyO3 binding
# (totalreclaw-core <= 2.2.0). Keep this string in sync with the Rust constant.
_FALLBACK_QUERY = """
  query ConfirmIndexed($id: ID!) {
    fact(id: $id) {
      id
      isActive
      blockNumber
    }
  }
"""


def _default_poll_ms() -> int:
    fn = getattr(_core, "confirm_indexed_default_poll_ms", None)
    return int(fn()) if callable(fn) else 1_000


def _default_timeout_ms() -> int:
    fn = getattr(_core, "confirm_indexed_default_timeout_ms", None)
    return int(fn()) if callable(fn) else 30_000


def _query_string() -> str:
    fn = getattr(_core, "confirm_indexed_query", None)
    return fn() if callable(fn) else _FALLBACK_QUERY


def _parse_response(payload_json: str) -> bool:
    fn = getattr(_core, "confirm_indexed_parse", None)
    if callable(fn):
        return bool(fn(payload_json))
    import json as _json
    value = _json.loads(payload_json)
    fact = value.get("data", {}).get("fact") if isinstance(value.get("data"), dict) else value.get("fact")
    return bool(fact and fact.get("isActive"))


async def confirm_indexed(
    fact_id: str,
    relay: RelayClient,
    *,
    expect: ExpectDirection = "active",
    poll_interval_ms: Optional[int] = None,
    timeout_ms: Optional[int] = None,
) -> bool:
    """Poll the subgraph until the fact id reaches the expected state.

    Parameters
    ----------
    fact_id
        The UUID of the fact to confirm. After ``pin_fact`` / retype / etc.
        this is the **new** fact id (the supersession target). For
        ``forget_fact`` this is the **original** fact id whose ``isActive``
        bit is being flipped.
    relay
        Configured RelayClient — already knows the subgraph endpoint via
        ``relay.query_subgraph``.
    expect
        ``"active"`` (default) — resolve when ``fact.isActive == True``.
        ``"inactive"`` — resolve when fact is missing OR
        ``fact.isActive == False``. Use ``"inactive"`` for forget.
    poll_interval_ms / timeout_ms
        Override the defaults from the Rust core (1s / 30s).

    Returns
    -------
    bool
        ``True`` when the resolution condition was met inside the timeout
        budget, ``False`` on timeout. The on-chain write is **already
        acknowledged** before this function is called — a ``False`` return
        only means the subgraph is still propagating, not that the chain
        write failed. Callers should surface this to users as ``partial=
        True`` rather than as an error.
    """
    poll_ms = poll_interval_ms if poll_interval_ms is not None else _default_poll_ms()
    total_ms = timeout_ms if timeout_ms is not None else _default_timeout_ms()
    query = _query_string()

    start = asyncio.get_event_loop().time()
    deadline = start + (total_ms / 1000.0)

    attempts = 0
    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        try:
            data = await relay.query_subgraph(query, {"id": fact_id})
            # `relay.query_subgraph` returns the parsed JSON envelope.
            # `confirm_indexed_parse` accepts either {data:{fact}} or {fact}.
            import json as _json

            payload = _json.dumps(data)
            is_active = _parse_response(payload)
            resolved = is_active if expect == "active" else not is_active
            if resolved:
                logger.debug(
                    "confirm_indexed: fact_id=%s expect=%s resolved after %d attempts",
                    fact_id,
                    expect,
                    attempts,
                )
                return True
        except Exception as exc:  # pragma: no cover — best-effort polling
            logger.debug("confirm_indexed: poll attempt failed: %s", exc)

        # Sleep before the next attempt — but only if there's still budget.
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_ms / 1000.0, remaining))

    logger.info(
        "confirm_indexed: fact_id=%s expect=%s NOT resolved within %dms (%d attempts)",
        fact_id,
        expect,
        total_ms,
        attempts,
    )
    return False
