"""LLM confirmation of a pinned-fact conflict (internal Hermes QA of 2.6.0rc2, Finding #4).

Core's resolver reports ``skip_new`` / ``existing_pinned`` when a new claim
overlaps a pinned claim (cosine band plus a shared name). That detects topic
overlap, not contradiction: facts that only share the pin's subject score in
the same range as true reversals. Before the Python agent layer drops an
auto-extracted fact because of a pin, it asks the extraction LLM one yes/no
question: does NEW say PINNED is no longer true?

Failure rule (decided by the caller, see ``contradiction.py``): this function
returns ``None`` whenever it cannot get a clear answer (no config, timeout,
upstream error, unparseable reply). It never logs either text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from .llm_client import LLMConfig, chat_completion, complete_with_extraction_fallback
from .loop_runner import InterpreterShutdownError, is_interpreter_shutdown_error

logger = logging.getLogger(__name__)

#: One check, including the extraction-model fallback walk.
PIN_CHECK_TIMEOUT_S = 6.0
#: Shared deadline for all checks of one ``detect_and_resolve_contradictions``
#: call, so several slow checks cannot stack up on the finalize path. It is
#: measured from the start of that call, so slow recalls before the first
#: pinned hit also use it up (the failure rule then applies).
PIN_CHECK_CYCLE_BUDGET_S = 8.0
#: With less than this left in the cycle budget no check is started.
PIN_CHECK_MIN_REMAINING_S = 0.5
#: Max LLM checks per ``detect_and_resolve_contradictions`` call.
PIN_CHECK_MAX_CALLS_PER_CYCLE = 3

PIN_CHECK_SYSTEM_PROMPT = (
    "You compare two statements from one user's memory. PINNED is a decision the user locked. "
    "NEW is a statement about to be saved. Answer YES only if NEW says or clearly implies that "
    "PINNED is no longer true: it reverses, replaces, cancels or contradicts it. Answer NO if NEW "
    "agrees with PINNED, adds detail to it, is about a different subject, or only mentions the "
    'same names. Reply with JSON only: {"answer": "YES"} or {"answer": "NO"}.'
)

_ANSWER_RE = re.compile(r"\W*(yes|no)\b", re.IGNORECASE)


def _yes_no(value: object) -> Optional[bool]:
    if not isinstance(value, str):
        return None
    word = value.strip().strip(".,!?;:\"' \t\n").lower()
    if word == "yes":
        return True
    if word == "no":
        return False
    return None


def _parse_answer(resp: object) -> Optional[bool]:
    """Parse a reply: JSON ``{"answer": "YES"}`` (what JSON-mode providers
    return), a bare JSON string, or a bare word. Anything else is None."""
    if not isinstance(resp, str):
        return None
    text = resp.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = _ANSWER_RE.match(text)
        return (m.group(1).lower() == "yes") if m else None
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and k.lower() == "answer":
                return _yes_no(v)
        strs = [v for v in data.values() if isinstance(v, str)]
        if len(data) == 1 and len(strs) == 1:
            return _yes_no(strs[0])
        return None
    return _yes_no(data)


async def llm_confirms_pin_conflict(
    llm_config: Optional[LLMConfig],
    pinned_text: str,
    new_text: str,
    timeout: Optional[float] = None,
) -> Optional[bool]:
    """Ask the LLM whether ``new_text`` contradicts ``pinned_text``.

    Returns True (clear YES), False (clear NO) or None (no clear answer).
    Raises :class:`InterpreterShutdownError` only for interpreter shutdown.
    """
    if llm_config is None:
        return None
    if timeout is None:
        timeout = PIN_CHECK_TIMEOUT_S
    try:
        response, _used = await asyncio.wait_for(
            complete_with_extraction_fallback(
                llm_config,
                PIN_CHECK_SYSTEM_PROMPT,
                f"PINNED: {pinned_text}\nNEW: {new_text}",
                completion=chat_completion,
            ),
            timeout,
        )
    except Exception as exc:
        if is_interpreter_shutdown_error(exc):
            raise InterpreterShutdownError(str(exc)) from exc
        logger.debug("Pin conflict check failed (%s)", type(exc).__name__)
        return None
    return _parse_answer(response)
