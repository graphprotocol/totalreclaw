"""Pair-flow completion handler — writes credentials.json + configures
the Hermes plugin state from the decrypted browser-uploaded phrase.

Split out of ``pair_tool.py`` so the HTTP-server-side code stays free of
any Hermes runtime imports at module-load time. The phrase enters this
module ONCE, gets persisted via ``PluginState.configure``, and never
leaves.

Phrase-safety invariants enforced here:

- NEVER returns the phrase in the :class:`CompletePairingResult`. Only
  ``account_id`` (EOA address, safe) and ``state`` are passed back to the
  HTTP handler, which forwards them to the browser. The browser shows
  the "pairing complete" confirmation; the agent's chat transcript never
  sees the phrase.
- Best-effort zeroization of local variables holding the phrase after
  state configure.
- No logging of phrase content. Only the EOA address (already public)
  and sid prefix are logged.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState
    from ..pair.session_store import PairSession

from ..pair.http_server import CompletePairingResult

logger = logging.getLogger(__name__)


def complete_pairing(
    phrase: str,
    session: "PairSession",
    state: "PluginState",
) -> CompletePairingResult:
    """Apply a browser-decrypted phrase to the Hermes plugin state.

    Called FROM the HTTP handler thread. ``state.configure`` is synchronous
    (writes credentials.json, derives the EOA address). The async Smart-
    Account derivation happens lazily on the first remember/recall call.
    """
    try:
        state.configure(phrase)
        client = state.get_client()
        eoa = getattr(client, "_eoa_address", None)
        logger.info(
            "pair-tool: credentials configured for EOA %s (session %s…)",
            eoa or "unknown",
            session.sid[:8],
        )
        return CompletePairingResult(state="active", account_id=eoa)
    except Exception as err:  # pragma: no cover — defensive
        logger.error(
            "pair-tool: complete_pairing failed for session %s…: %r",
            session.sid[:8],
            err,
        )
        return CompletePairingResult(state="error", error=str(err))
    finally:
        # Best-effort phrase zeroization.
        phrase = ""  # noqa: F841
