"""
Update-notice bookkeeping for the Python (Hermes) client.

Hermes has NO native mechanism for updating a pip/entry-point plugin
(``hermes plugins update`` is git-clone-only; ``hermes update`` covers Hermes
itself). So when the relay advertises a newer stable version in the billing
features (``latest_stable_python``), the client surfaces a one-line nudge via
the existing quota-warning channel telling the user to say "update TotalReclaw".

Two concerns live here, both pure/testable and framework-agnostic:

1. **Version comparison** (:func:`is_newer_stable`) — a small internal PEP-440
   comparator, sufficient for our ``MAJOR.MINOR.PATCH`` + optional
   ``rcN``/``bN``/``aN`` pre-release scheme. We deliberately do NOT add
   ``packaging`` as a dependency: it is not a declared dep (only transitively
   present via ``transformers``), and the compare we need is narrow. The one
   subtlety we get right: an rc of X is OLDER than final X (``2.4.5rc11`` <
   ``2.4.5``), so an rc user IS nudged when the matching final ships, but a
   user already on a newer rc line (``2.4.6rc1``) is NOT nudged by an older
   final (``2.4.5``).

2. **Rate-limit persistence** (:func:`should_notify_now` / :func:`mark_notified`)
   — one notice per 24h across sessions, tracked by a timestamp sentinel under
   ``~/.totalreclaw/`` (mirrors the import-onboarding sentinel in
   ``import_state.py``). Best-effort: a failed read/write degrades to "notify"
   rather than crashing a hook.

The env kill-switch ``TOTALRECLAW_DISABLE_UPDATE_NOTICE=1`` short-circuits the
whole feature.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Mirrors ``import_state.IMPORT_STATE_DIR`` — same ~/.totalreclaw/ home so all
# client-local bookkeeping lives in one place. A timestamp file (unix seconds).
_STATE_DIR: Path = Path.home() / ".totalreclaw"
_NOTICE_SENTINEL_NAME = "update-notice-last-shown"

# One notice per 24h across sessions.
NOTICE_INTERVAL_SECONDS: int = 24 * 60 * 60

# Pre-release phase ordering: alpha < beta < rc < final. Final is represented
# by the largest sentinel so "no pre-release" always sorts after any of them.
_PHASE_ORDER = {"a": 0, "b": 1, "rc": 2, "": 3}
_VERSION_RE = re.compile(
    r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?(?:[.\-_]?(a|b|rc|alpha|beta|c)\.?(\d+))?",
    re.IGNORECASE,
)
_PHASE_ALIASES = {"alpha": "a", "beta": "b", "c": "rc", "a": "a", "b": "b", "rc": "rc"}


def disabled_by_env() -> bool:
    """True when ``TOTALRECLAW_DISABLE_UPDATE_NOTICE`` is set to a truthy value."""
    return os.environ.get("TOTALRECLAW_DISABLE_UPDATE_NOTICE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _parse_version(v: str) -> Optional[Tuple[int, int, int, int, int]]:
    """Parse ``MAJOR.MINOR[.PATCH][rcN]`` into a sortable tuple.

    Returns ``(major, minor, patch, phase_rank, phase_num)`` or ``None`` if the
    string doesn't look like a version we understand. ``phase_rank`` uses
    :data:`_PHASE_ORDER` so final (rank 3) sorts after any pre-release; a final
    release gets ``phase_num = 0`` (unused).
    """
    if not v or not isinstance(v, str):
        return None
    m = _VERSION_RE.match(v)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.group(3) is not None else 0
    phase_tok = (m.group(4) or "").lower()
    if phase_tok:
        phase = _PHASE_ALIASES.get(phase_tok, "")
        phase_num = int(m.group(5)) if m.group(5) is not None else 0
    else:
        phase = ""  # final
        phase_num = 0
    phase_rank = _PHASE_ORDER.get(phase, 3)
    return (major, minor, patch, phase_rank, phase_num)


def is_newer_stable(latest: Optional[str], installed: Optional[str]) -> bool:
    """True if ``latest`` is a strictly newer version than ``installed``.

    Handles the rc-vs-final rule correctly:

    * ``is_newer_stable("2.4.5", "2.4.5rc11")`` → True  (final beats its own rc)
    * ``is_newer_stable("2.4.5", "2.4.6rc1")``  → False (user is ahead on 2.4.6 line)
    * ``is_newer_stable("2.4.5", "2.4.5")``     → False (equal)
    * ``is_newer_stable("2.4.5", "2.4.4")``     → True
    * ``is_newer_stable("2.4.5", "2.5.0")``     → False (installed newer)

    Malformed / missing input ⇒ False (never nudge on bad data).
    """
    lp = _parse_version(latest or "")
    ip = _parse_version(installed or "")
    if lp is None or ip is None:
        return False
    return lp > ip


def _sentinel_path() -> Path:
    return _STATE_DIR / _NOTICE_SENTINEL_NAME


def last_notified_at() -> Optional[float]:
    """Unix timestamp of the last notice shown, or None if never / unreadable."""
    try:
        raw = _sentinel_path().read_text(encoding="utf-8").strip()
        return float(raw)
    except Exception:
        return None


def within_rate_limit(now: Optional[float] = None) -> bool:
    """True if a notice was shown within the last :data:`NOTICE_INTERVAL_SECONDS`.

    Used to suppress a repeat notice. A missing/unreadable sentinel ⇒ False
    (i.e. not rate-limited ⇒ allowed to notify).
    """
    last = last_notified_at()
    if last is None:
        return False
    current = time.time() if now is None else now
    return (current - last) < NOTICE_INTERVAL_SECONDS


def mark_notified(now: Optional[float] = None) -> None:
    """Persist 'notice shown at now' so the next 24h are suppressed.

    Best-effort — a write failure means at most one extra notice, not a crash.
    """
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        current = time.time() if now is None else now
        _sentinel_path().write_text(str(current), encoding="utf-8")
    except OSError:
        pass


def build_update_notice(latest: str, installed: str) -> str:
    """The one-line user-facing nudge string."""
    return (
        f"TotalReclaw {latest} is available (you're running {installed}). "
        f"Say 'update TotalReclaw' to upgrade."
    )


def maybe_build_update_notice(
    latest: Optional[str],
    installed: Optional[str],
    now: Optional[float] = None,
) -> Optional[str]:
    """Return the nudge string if a notice should fire right now, else None.

    Combines every gate in one place so the hook stays a two-liner:

    1. kill-switch env not set,
    2. ``latest`` is a strictly newer stable than ``installed``,
    3. not within the 24h rate-limit window.

    On a positive result the caller is responsible for calling
    :func:`mark_notified` after it has actually queued the notice (so a failure
    to queue doesn't burn the 24h window).
    """
    if disabled_by_env():
        return None
    if not is_newer_stable(latest, installed):
        return None
    if within_rate_limit(now):
        return None
    return build_update_notice(latest or "", installed or "")
