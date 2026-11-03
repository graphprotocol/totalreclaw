"""Relay registration for a vault whose root is on this host.

The relay authenticates every call by ``SHA-256(auth_key)``, and it only
knows an auth key after ``POST /v1/register``. A client configured from a
derived-bundle-v1 credential never registers: the bundle carries no salt,
and derived-bundle-v1.md §4.5 puts registration on the provisioning
origin, the party that still holds the root. On a Hermes host that origin
is either the pair-completion handler (a phrase just arrived from the
browser) or ``hermes/auto_migrate`` (which swaps the phrase for a bundle).

Both call :func:`register_vault` while the phrase-configured client still
exists. Registration is idempotent on the relay side (an already-known
auth key returns the existing ``user_id``), so calling it for a returning
user is harmless.

Logging is metadata only: an outcome and, on failure, the exception class
or HTTP status. Never the exception message, the client, or anything key-
derived.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound on one registration round-trip.
REGISTER_TIMEOUT_S = 15.0


def describe_register_error(err: BaseException) -> str:
    """A log-safe description of a registration failure."""
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}"
    return type(err).__name__


async def register_vault(client: Any, *, timeout_s: float = REGISTER_TIMEOUT_S) -> bool:
    """Register ``client``'s vault with the relay. Returns ``True`` on success.

    ``client`` must be a phrase-configured :class:`totalreclaw.client.TotalReclaw`
    (``register()`` refuses bundle-configured clients). Never raises: a
    failure is logged and reported as ``False``.
    """
    try:
        await asyncio.wait_for(client.register(), timeout=timeout_s)
    except Exception as err:  # noqa: BLE001 — caller decides what a failure means
        logger.warning(
            "relay registration failed (%s)", describe_register_error(err)
        )
        return False
    return True


def register_vault_sync(client: Any, *, timeout_s: float = REGISTER_TIMEOUT_S) -> bool:
    """Synchronous :func:`register_vault`, for callers on a plain thread.

    Runs on the process-wide background loop (``agent.loop_runner``), the
    same loop the Hermes hooks drive this client on, so the relay's
    per-loop HTTP client is never left bound to a short-lived loop.
    Never raises.
    """
    from totalreclaw.agent.loop_runner import run_sync

    try:
        return run_sync(register_vault(client, timeout_s=timeout_s))
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "relay registration did not run (%s)", type(err).__name__
        )
        return False
