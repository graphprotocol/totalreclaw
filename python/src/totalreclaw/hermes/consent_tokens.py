"""One-time, hash-at-rest consent tokens.

Shared by the import privacy-disclosure gate (#437) and the pair-replace guard
(#466 Finding-2). A token proves the agent surfaced a specific tool RESPONSE
this flow before it can assert the user's consent — it forces at least one
round-trip through the guard's response instead of the agent self-asserting a
confirmation it never showed the user.

Stored HASHED at rest: the sidecar is named ``{kind}-{sha256(token)[:16]}.pending``
and holds only ``{kind, subject, minted_at}`` — the raw token appears ONLY in
the tool response, never on disk. Tokens carry a 1h TTL (expired → not
redeemable, cleaned up opportunistically on the next mint of that kind).

Limits (documented honestly): this raises the bar to "the token reached the
agent via the tool response" — it cannot stop a same-trust-domain agent that
logs its own tool responses and reuses the value. The enforcement goal is
narrowly "this guard's response was received THIS flow".

Sidecars live under ``import_state.IMPORT_STATE_DIR`` (the ``.pending`` suffix
keeps them out of the ``*.json`` state-record globs; #460).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

#: Time-to-live for any consent token, in seconds.
TOKEN_TTL_S = 3600


def token_hash(token: str) -> str:
    """SHA-256 (first 16 hex chars) of a token — the at-rest filename key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _state_dir():
    # Referenced late so tests that monkeypatch IMPORT_STATE_DIR take effect.
    from totalreclaw import import_state as ist
    return ist.IMPORT_STATE_DIR


def _sidecar_path(kind: str, token: str):
    return _state_dir() / f"{kind}-{token_hash(token)}.pending"


def is_expired(minted_at) -> bool:
    """True when ``minted_at`` (ISO) is older than the TTL, or is missing /
    unparseable (fail safe → treat as expired)."""
    if not isinstance(minted_at, str) or not minted_at:
        return True
    try:
        minted = datetime.fromisoformat(minted_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - minted).total_seconds() > TOKEN_TTL_S


def cleanup_expired(kind: str) -> None:
    """Best-effort removal of expired ``{kind}-*.pending`` sidecars."""
    try:
        for p in _state_dir().glob(f"{kind}-*.pending"):
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                try:
                    p.unlink()
                except OSError:
                    pass
                continue
            if is_expired(data.get("minted_at")):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def mint(kind: str, subject: str) -> str:
    """Mint + persist a one-time token for ``(kind, subject)``; return the raw
    token (which the caller MUST return only in the tool response)."""
    token = uuid.uuid4().hex[:16]
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        cleanup_expired(kind)
        _sidecar_path(kind, token).write_text(json.dumps({
            "kind": kind,
            "subject": subject,
            "minted_at": datetime.now(timezone.utc).isoformat(),
        }))
    except OSError:
        pass  # worst case: token can't be redeemed → the guard re-shows
    return token


def redeem(kind: str, subject: str, token) -> bool:
    """Consume a token for ``(kind, subject)``. One-time use; rejects an
    expired, wrong-kind, or wrong-subject token. Expiry is checked FIRST so a
    stale sidecar is cleaned up regardless of subject."""
    if not token or not isinstance(token, str) or not token.isalnum():
        return False
    try:
        path = _sidecar_path(kind, token)
        if not path.exists():
            return False
        data = json.loads(path.read_text())
        if is_expired(data.get("minted_at")):
            try:
                path.unlink()
            except OSError:
                pass
            return False
        # Bind to the intended kind + subject (the filename hash already binds
        # the token value). A mismatch leaves the sidecar intact so a
        # concurrent correct redemption can still succeed.
        if data.get("kind") != kind or data.get("subject") != subject:
            return False
        path.unlink()
        return True
    except (OSError, ValueError):
        return False
