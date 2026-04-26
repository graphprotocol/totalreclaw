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
  and a session-id-fragment prefix are logged.

Session-id attribute compatibility (rc.23 fix for QA finding F4):
this handler gets called from BOTH the local-mode HTTP path
(``..pair.session_store.PairSession`` — has ``.sid``) and the
relay-mode shared completion path (``..pair.remote_client
.RemotePairSession`` — has ``.token``). Pre-rc.23 the helper hard-coded
``session.sid``, which raised ``AttributeError`` on the relay shape.
The fix below uses :func:`_session_id_fragment` to read either
attribute defensively so a single completion handler can serve both
paths.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state import PluginState
    from ..pair.session_store import PairSession  # noqa: F401 — type-only

from ..pair.http_server import CompletePairingResult

logger = logging.getLogger(__name__)


def _session_id_fragment(session: Any) -> str:
    """Best-effort short identifier for log lines.

    Reads either ``.token`` (``RemotePairSession``) or ``.sid``
    (``PairSession``) — the local handler shape has ``.sid``, the relay
    handshake exposes ``.token``. Returns an 8-char prefix or ``"?"``
    if neither attribute is present / both are empty.

    NEVER raises — log lines must not torpedo a successful pairing.
    """
    candidate = getattr(session, "token", None) or getattr(session, "sid", None)
    if not isinstance(candidate, str) or not candidate:
        return "?"
    return candidate[:8]


def complete_pairing(
    phrase: str,
    session: Any,
    state: "PluginState",
) -> CompletePairingResult:
    """Apply a browser-decrypted phrase to the Hermes plugin state.

    Called from BOTH the local HTTP handler thread and the relay-mode
    completion path. ``state.configure`` is synchronous (writes
    credentials.json, derives the EOA address). The async Smart-Account
    derivation happens lazily on the first remember/recall call.

    ``session`` accepts either a ``PairSession`` (local mode, has
    ``.sid``) or a ``RemotePairSession`` (relay mode, has ``.token``).
    The id is used only for log-line correlation — callers don't need
    the same shape on both paths.
    """
    sid_frag = _session_id_fragment(session)
    try:
        state.configure(phrase)
        client = state.get_client()
        eoa = getattr(client, "_eoa_address", None)
        logger.info(
            "pair-tool: credentials configured for EOA %s (session %s…)",
            eoa or "unknown",
            sid_frag,
        )
        return CompletePairingResult(state="active", account_id=eoa)
    except Exception as err:  # pragma: no cover — defensive
        logger.error(
            "pair-tool: complete_pairing failed for session %s…: %r",
            sid_frag,
            err,
        )
        return CompletePairingResult(state="error", error=str(err))
    finally:
        # Best-effort phrase zeroization.
        phrase = ""  # noqa: F841
