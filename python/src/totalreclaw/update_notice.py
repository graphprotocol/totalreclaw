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


# ---------------------------------------------------------------------------
# Generic timestamp-sentinel helpers — shared by the update notice, the
# recall-health notice (recall_health.py), and the PyPI lookup cache. One
# unix-seconds float per file under ~/.totalreclaw/. Best-effort semantics:
# unreadable ⇒ "never", write failure ⇒ silently skipped.
# ---------------------------------------------------------------------------


def sentinel_read(name: str) -> Optional[float]:
    """Unix timestamp stored in sentinel *name*, or None if never / unreadable."""
    try:
        raw = (_STATE_DIR / name).read_text(encoding="utf-8").strip()
        return float(raw)
    except Exception:
        return None


def sentinel_within(name: str, interval: float, now: Optional[float] = None) -> bool:
    """True when sentinel *name* was stamped within the last *interval* seconds."""
    last = sentinel_read(name)
    if last is None:
        return False
    current = time.time() if now is None else now
    return (current - last) < interval


def sentinel_mark(name: str, now: Optional[float] = None) -> None:
    """Stamp sentinel *name* with *now* (best-effort)."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        current = time.time() if now is None else now
        (_STATE_DIR / name).write_text(str(current), encoding="utf-8")
    except OSError:
        pass


def last_notified_at() -> Optional[float]:
    """Unix timestamp of the last notice shown, or None if never / unreadable."""
    return sentinel_read(_NOTICE_SENTINEL_NAME)


def within_rate_limit(now: Optional[float] = None) -> bool:
    """True if a notice was shown within the last :data:`NOTICE_INTERVAL_SECONDS`.

    Used to suppress a repeat notice. A missing/unreadable sentinel ⇒ False
    (i.e. not rate-limited ⇒ allowed to notify).
    """
    return sentinel_within(_NOTICE_SENTINEL_NAME, NOTICE_INTERVAL_SECONDS, now)


def mark_notified(now: Optional[float] = None) -> None:
    """Persist 'notice shown at now' so the next 24h are suppressed.

    Best-effort — a write failure means at most one extra notice, not a crash.
    """
    sentinel_mark(_NOTICE_SENTINEL_NAME, now)


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


# ---------------------------------------------------------------------------
# PyPI fallback — latest-stable lookup when the relay doesn't advertise one.
#
# The relay's `features.latest_stable_python` (env `LATEST_STABLE_PYTHON`)
# is the primary channel, but it depends on an operator remembering the env
# flip at each stable promote — it shipped dark and stayed dark for weeks.
# This fallback asks PyPI directly so `totalreclaw_status` / `doctor` can
# still answer "is there a newer stable?" when the relay feature is unset.
#
# Deliberately NOT wired into per-turn hooks: it is a network call (3s
# timeout) and belongs only on explicit surfaces (status tool, doctor).
# Result is cached for 24h in ~/.totalreclaw/ so repeated status calls
# don't re-hit PyPI. Failures are never cached.
# ---------------------------------------------------------------------------

PYPI_JSON_URL = "https://pypi.org/pypi/totalreclaw/json"
_PYPI_CACHE_NAME = "pypi-latest-stable-cache"
PYPI_CACHE_TTL_SECONDS: int = 24 * 60 * 60


def _latest_final_from_releases(releases: dict) -> Optional[str]:
    """Pick the greatest FINAL (non-pre-release, non-fully-yanked) version.

    PyPI's ``info.version`` is close to this but its pre-release handling
    has edge cases; filtering ``releases`` through our own comparator keeps
    the rc<final semantics identical to :func:`is_newer_stable`.
    """
    best_key = None
    best = None
    for version, files in releases.items():
        parsed = _parse_version(version)
        if parsed is None or parsed[3] != _PHASE_ORDER[""]:
            continue  # pre-release or unparseable
        if isinstance(files, list) and files and all(
            isinstance(f, dict) and f.get("yanked") for f in files
        ):
            continue  # every artifact yanked ⇒ not installable
        if best_key is None or parsed > best_key:
            best_key, best = parsed, version
    return best


def _pypi_cache_read(now: Optional[float] = None) -> Optional[str]:
    """Cached PyPI answer if fresh (<24h), else None. File shape: 'ts version'."""
    try:
        raw = (_STATE_DIR / _PYPI_CACHE_NAME).read_text(encoding="utf-8").strip()
        ts_str, _, version = raw.partition(" ")
        ts = float(ts_str)
        current = time.time() if now is None else now
        if version and (current - ts) < PYPI_CACHE_TTL_SECONDS:
            return version
    except Exception:
        pass
    return None


def _pypi_cache_write(version: str, now: Optional[float] = None) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        current = time.time() if now is None else now
        (_STATE_DIR / _PYPI_CACHE_NAME).write_text(
            f"{current} {version}", encoding="utf-8"
        )
    except OSError:
        pass


def fetch_latest_stable_from_pypi(
    timeout: float = 3.0, now: Optional[float] = None
) -> Optional[str]:
    """Best-effort latest FINAL version of ``totalreclaw`` on PyPI.

    Honours the kill-switch (this is still update-notice machinery), serves
    from the 24h cache when fresh, and returns None on any failure — the
    caller treats None exactly like "relay didn't advertise either".
    """
    if disabled_by_env():
        return None
    cached = _pypi_cache_read(now)
    if cached:
        return cached
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.get(PYPI_JSON_URL)
            if resp.status_code != 200:
                return None
            data = resp.json()
        latest = _latest_final_from_releases(data.get("releases") or {})
        if latest:
            _pypi_cache_write(latest, now)
        return latest
    except Exception as exc:
        logger.debug("PyPI latest-stable lookup failed: %s", exc)
        return None


def resolve_latest_stable(
    relay_advertised: Optional[str],
    *,
    allow_pypi_fallback: bool = True,
    timeout: float = 3.0,
) -> Tuple[Optional[str], str]:
    """Resolve the latest stable version and where the answer came from.

    Returns ``(version, source)`` with source ``"relay"`` | ``"pypi"`` | ``""``.
    The relay value wins when present (operator-controlled, zero extra I/O);
    the PyPI fallback only fires when the relay is dark AND fallback is
    allowed AND the kill-switch is off.
    """
    if relay_advertised and isinstance(relay_advertised, str):
        return relay_advertised, "relay"
    if allow_pypi_fallback:
        via_pypi = fetch_latest_stable_from_pypi(timeout=timeout)
        if via_pypi:
            return via_pypi, "pypi"
    return None, ""
