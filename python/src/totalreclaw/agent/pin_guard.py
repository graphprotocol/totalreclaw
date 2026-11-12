"""Pin contract for LLM-guided UPDATE / DELETE actions (PRD-04 F1, leaf DEP-5).

Auto-extraction's LLM may answer UPDATE (store a replacement, then tombstone
``existing_fact_id``) or DELETE (tombstone ``existing_fact_id``). A pinned fact
must never be tombstoned by an automatic process
(``docs/specs/totalreclaw/memory-taxonomy-v1.md`` §Pin semantics), so before
``_auto_extract_inner`` applies any action this guard reads the target's pin
state with ``client.get_fact_pin_status`` and:

* target pinned -> drop the action entirely (UPDATE and DELETE). This is the
  same outcome core's contradiction resolver gives a new claim that
  contradicts a pinned one (``SkipNew { reason: ExistingPinned }``).
* pin state unknown (the read raised) -> fail closed on the destructive half:
  DELETE is dropped, UPDATE is kept as a plain ADD (no tombstone).
* target not pinned -> action unchanged.

Log lines carry the action and a reason category only -- never fact text or
fact ids. No decision-log row is written (PRD-04 DEP-5 plan, decision D2).
"""
from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Optional

from .loop_runner import InterpreterShutdownError, is_interpreter_shutdown_error

if TYPE_CHECKING:
    from .extraction import ExtractedFact

logger = logging.getLogger(__name__)

REASON_EXISTING_PINNED = "existing_pinned"
REASON_PIN_STATE_UNKNOWN = "pin_state_unknown"

_GUARDED_ACTIONS = ("UPDATE", "DELETE")


def _is_guarded(fact: "ExtractedFact") -> bool:
    return fact.action in _GUARDED_ACTIONS and bool(fact.existing_fact_id)


def _as_add(fact: "ExtractedFact") -> "ExtractedFact":
    return dataclasses.replace(fact, action="ADD", existing_fact_id=None)


def fail_closed_actions(
    facts: list["ExtractedFact"],
    log: Optional[Any] = None,
) -> list["ExtractedFact"]:
    """Apply the unknown-pin-state rule to every guarded action.

    DELETE actions are dropped and UPDATE actions become ADD, so nothing is
    tombstoned. Used per fact when its pin lookup failed, and for the whole
    batch when the guard itself could not run.
    """
    if log is None:
        log = logger
    out: list["ExtractedFact"] = []
    for fact in facts:
        if not _is_guarded(fact):
            out.append(fact)
        elif fact.action == "DELETE":
            log.info("pin_guard: skipped DELETE (reason=%s)", REASON_PIN_STATE_UNKNOWN)
        else:
            log.info(
                "pin_guard: UPDATE stored as ADD without tombstone (reason=%s)",
                REASON_PIN_STATE_UNKNOWN,
            )
            out.append(_as_add(fact))
    return out


async def apply_pin_guard(
    facts: list["ExtractedFact"],
    client: Any,
    log: Optional[Any] = None,
) -> list["ExtractedFact"]:
    """Return ``facts`` with every UPDATE/DELETE that targets a pinned fact
    removed (see module docstring for the full rule). One pin-state lookup
    per distinct target id. Re-raises ``InterpreterShutdownError``; every
    other lookup failure is treated as "pin state unknown"."""
    if log is None:
        log = logger

    targets: list[str] = []
    for fact in facts:
        if _is_guarded(fact) and fact.existing_fact_id not in targets:
            targets.append(fact.existing_fact_id)
    if not targets:
        return list(facts)

    pinned_state: dict[str, Optional[bool]] = {}
    for fact_id in targets:
        try:
            pinned_state[fact_id] = bool(await client.get_fact_pin_status(fact_id))
        except InterpreterShutdownError:
            raise
        except Exception as exc:
            if is_interpreter_shutdown_error(exc):
                raise InterpreterShutdownError(str(exc)) from exc
            log.debug("pin_guard: pin-state lookup failed (%s)", type(exc).__name__)
            pinned_state[fact_id] = None

    out: list["ExtractedFact"] = []
    for fact in facts:
        if not _is_guarded(fact):
            out.append(fact)
            continue
        state = pinned_state.get(fact.existing_fact_id)
        if state is False:
            out.append(fact)
        elif state is True:
            log.info(
                "pin_guard: skipped %s on a pinned fact (reason=%s)",
                fact.action,
                REASON_EXISTING_PINNED,
            )
        else:
            out.extend(fail_closed_actions([fact], log))
    return out
