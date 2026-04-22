"""pair.session_store — atomic TTL-evicted session store for the Hermes
QR-pairing flow.

Python parity of ``skill/plugin/pair-session-store.ts`` (v3.3.0). Same
on-disk schema (so a CLI run against the TS gateway can inspect the
same file, though in practice each gateway owns its own sessions file),
same status values, same retention windows.

Storage path: ``~/.totalreclaw/pair-sessions.json`` (default — tests pass
a hermetic tmpdir). Mode 0600; parent dir mode 0700. Atomic writes via
temp-file + rename.

Locking: a cooperative ``.lock`` sentinel via ``os.O_CREAT | os.O_EXCL``
— the Python equivalent of Node's ``openSync(path, 'wx')``. Stale locks
older than 30 s are force-broken. No multi-process races expected (one
gateway per host), but the lock still prevents tearing between the
async agent-tool path and the HTTP handler path.

No recovery-phrase material EVER enters this module. Private keys (sk_b64)
are stored cleartext under the 0600 file — attacker model per design doc
§5d (rooted gateway host is out-of-scope).
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Types (mirrored in skill/plugin/pair-session-store.ts)
# ---------------------------------------------------------------------------

#: Mode the operator chose when starting the session. Drives the browser
#: pair-page's UI branch.
PairSessionMode = Literal["generate", "import"]

#: Lifecycle state. Matches TS ``PairSessionStatus``.
PairSessionStatus = Literal[
    "awaiting_scan",
    "device_connected",
    "completed",
    "consumed",
    "expired",
    "rejected",
]


@dataclass
class PairOperatorContext:
    channel: str = "agent"
    sender_id: Optional[str] = None
    account_id: Optional[str] = None


@dataclass
class PairSession:
    """Persistent record written to ``~/.totalreclaw/pair-sessions.json``.

    Field names stay in snake_case on the Python side but the on-disk JSON
    uses the TS-side camelCase so a future migration (or a TS plugin
    sharing the same file) reads the same record. :func:`_session_to_dict`
    / :func:`_session_from_dict` handle the rename.
    """

    sid: str
    sk_gateway_b64: str
    pk_gateway_b64: str
    created_at_ms: int
    expires_at_ms: int
    secondary_code: str
    secondary_code_attempts: int
    operator_context: PairOperatorContext
    mode: PairSessionMode
    status: PairSessionStatus
    last_status_change_at_ms: int


@dataclass
class PairSessionFile:
    version: int = 1
    sessions: List[PairSession] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAIR_SESSION_FILE_VERSION = 1
DEFAULT_PAIR_TTL_MS = 15 * 60 * 1000
MIN_PAIR_TTL_MS = 5 * 60 * 1000
MAX_PAIR_TTL_MS = 60 * 60 * 1000
TERMINAL_RETENTION_MS = 60 * 60 * 1000
MAX_SECONDARY_CODE_ATTEMPTS = 5

LOCK_STALE_MS = 30_000
LOCK_WAIT_MS = 10_000
LOCK_RETRY_MS = 50

NowFn = Callable[[], int]


def default_now_ms() -> int:
    """Default milliseconds-since-epoch clock. Mirrors TS ``Date.now``."""
    return int(time.time() * 1000)


def default_pair_sessions_path(base_dir: Path) -> Path:
    """Return the pair-sessions.json path inside ``base_dir``."""
    return Path(base_dir) / "pair-sessions.json"


# ---------------------------------------------------------------------------
# Default randomness
# ---------------------------------------------------------------------------


def _default_sid() -> str:
    """16 random bytes rendered as 32 hex chars — same as TS ``defaultRngSid``."""
    return secrets.token_hex(16)


def _default_secondary_code() -> str:
    """Uniform 6-digit numeric string, left-padded. Reject-sample to
    avoid the small bias of naive ``secrets.randbelow(1_000_000)``.

    Actually ``secrets.randbelow`` is already uniform (it uses a reject-
    sample internally), so we can call it directly. Kept as a helper so
    tests can swap it for a deterministic stub.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# JSON (de)serialization
# ---------------------------------------------------------------------------

_TS_KEY_MAP = {
    "sid": "sid",
    "sk_gateway_b64": "skGatewayB64",
    "pk_gateway_b64": "pkGatewayB64",
    "created_at_ms": "createdAtMs",
    "expires_at_ms": "expiresAtMs",
    "secondary_code": "secondaryCode",
    "secondary_code_attempts": "secondaryCodeAttempts",
    "operator_context": "operatorContext",
    "mode": "mode",
    "status": "status",
    "last_status_change_at_ms": "lastStatusChangeAtMs",
}

_OPCTX_TS_MAP = {
    "channel": "channel",
    "sender_id": "senderId",
    "account_id": "accountId",
}

_PY_KEY_MAP = {ts: py for py, ts in _TS_KEY_MAP.items()}
_OPCTX_PY_MAP = {ts: py for py, ts in _OPCTX_TS_MAP.items()}


def _operator_context_to_dict(ctx: PairOperatorContext) -> Dict[str, Any]:
    d: Dict[str, Any] = {"channel": ctx.channel}
    if ctx.sender_id is not None:
        d["senderId"] = ctx.sender_id
    if ctx.account_id is not None:
        d["accountId"] = ctx.account_id
    return d


def _operator_context_from_dict(raw: Any) -> PairOperatorContext:
    if not isinstance(raw, dict):
        return PairOperatorContext()
    kwargs: Dict[str, Any] = {}
    for ts_key, py_key in _OPCTX_PY_MAP.items():
        if ts_key in raw and isinstance(raw[ts_key], (str, type(None))):
            kwargs[py_key] = raw[ts_key]
    return PairOperatorContext(**kwargs)


def _session_to_dict(s: PairSession) -> Dict[str, Any]:
    return {
        "sid": s.sid,
        "skGatewayB64": s.sk_gateway_b64,
        "pkGatewayB64": s.pk_gateway_b64,
        "createdAtMs": s.created_at_ms,
        "expiresAtMs": s.expires_at_ms,
        "secondaryCode": s.secondary_code,
        "secondaryCodeAttempts": s.secondary_code_attempts,
        "operatorContext": _operator_context_to_dict(s.operator_context),
        "mode": s.mode,
        "status": s.status,
        "lastStatusChangeAtMs": s.last_status_change_at_ms,
    }


def _session_from_dict(raw: Any) -> Optional[PairSession]:
    if not isinstance(raw, dict):
        return None
    required = {
        "sid",
        "skGatewayB64",
        "pkGatewayB64",
        "createdAtMs",
        "expiresAtMs",
        "secondaryCode",
        "secondaryCodeAttempts",
        "operatorContext",
        "mode",
        "status",
        "lastStatusChangeAtMs",
    }
    if not required.issubset(raw.keys()):
        return None
    try:
        if raw["mode"] not in ("generate", "import"):
            return None
        sec_code = raw["secondaryCode"]
        if not (isinstance(sec_code, str) and len(sec_code) == 6 and sec_code.isdigit()):
            return None
        return PairSession(
            sid=str(raw["sid"]),
            sk_gateway_b64=str(raw["skGatewayB64"]),
            pk_gateway_b64=str(raw["pkGatewayB64"]),
            created_at_ms=int(raw["createdAtMs"]),
            expires_at_ms=int(raw["expiresAtMs"]),
            secondary_code=sec_code,
            secondary_code_attempts=int(raw["secondaryCodeAttempts"]),
            operator_context=_operator_context_from_dict(raw["operatorContext"]),
            mode=raw["mode"],
            status=raw["status"],
            last_status_change_at_ms=int(raw["lastStatusChangeAtMs"]),
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Path + lock primitives
# ---------------------------------------------------------------------------


def _ensure_sessions_file_dir(sessions_path: Path) -> None:
    parent = sessions_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _acquire_lock(sessions_path: Path, wait_ms: int = LOCK_WAIT_MS) -> Callable[[], None]:
    """Exclusive-create a ``.lock`` sentinel. Return an unlocker.

    Deadline-bounded retry with stale-lock break, mirroring the TS version.
    Raises ``TimeoutError`` on deadline.
    """
    _ensure_sessions_file_dir(sessions_path)
    lock_path = Path(str(sessions_path) + ".lock")
    deadline = time.monotonic() + (wait_ms / 1000.0)

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            finally:
                os.close(fd)

            def release() -> None:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

            return release
        except FileExistsError:
            try:
                mtime_ns = lock_path.stat().st_mtime_ns
                age_ms = (time.time_ns() - mtime_ns) / 1_000_000
                if age_ms > LOCK_STALE_MS:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            except FileNotFoundError:
                continue

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"pair-session-store: could not acquire lock at {lock_path} within {wait_ms}ms"
                )
            time.sleep(LOCK_RETRY_MS / 1000.0)


# ---------------------------------------------------------------------------
# Load / save (no lock — callers wrap via _acquire_lock)
# ---------------------------------------------------------------------------


def _empty_file() -> PairSessionFile:
    return PairSessionFile(version=PAIR_SESSION_FILE_VERSION, sessions=[])


def _load_pair_sessions_file(sessions_path: Path) -> PairSessionFile:
    """Load sessions file; return empty on any read/parse error."""
    try:
        if not sessions_path.exists():
            return _empty_file()
        raw = sessions_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if (
            not isinstance(parsed, dict)
            or parsed.get("version") != PAIR_SESSION_FILE_VERSION
            or not isinstance(parsed.get("sessions"), list)
        ):
            return _empty_file()
        clean: List[PairSession] = []
        for s in parsed["sessions"]:
            parsed_s = _session_from_dict(s)
            if parsed_s is not None:
                clean.append(parsed_s)
        return PairSessionFile(version=PAIR_SESSION_FILE_VERSION, sessions=clean)
    except (OSError, json.JSONDecodeError, ValueError):
        return _empty_file()


def _write_pair_sessions_file(sessions_path: Path, file: PairSessionFile) -> bool:
    """Atomic write: temp file + rename. 0600. Returns False on I/O error."""
    try:
        _ensure_sessions_file_dir(sessions_path)
        tmp = Path(f"{sessions_path}.tmp-{os.getpid()}-{int(time.time() * 1_000_000)}")
        serialized = {
            "version": file.version,
            "sessions": [_session_to_dict(s) for s in file.sessions],
        }
        # Write with 0600 from the start (use os.open to control the mode).
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(serialized).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(sessions_path))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Pruning — lazy, idempotent
# ---------------------------------------------------------------------------


def _prune_stale_sessions(
    file: PairSessionFile,
    now_ms: int,
) -> Tuple[PairSessionFile, List[str]]:
    keepers: List[PairSession] = []
    pruned: List[str] = []

    for s in file.sessions:
        terminal = s.status in ("completed", "consumed", "expired", "rejected")
        next_s = s
        if not terminal and now_ms > s.expires_at_ms:
            next_s = PairSession(
                **{**asdict(s), "operator_context": s.operator_context},
            )
            # dataclasses.asdict flattens nested dataclasses; rebuild op ctx.
            next_s.operator_context = s.operator_context
            next_s.status = "expired"
            next_s.last_status_change_at_ms = s.expires_at_ms

        now_terminal = next_s.status in ("completed", "consumed", "expired", "rejected")
        if now_terminal and (now_ms - next_s.last_status_change_at_ms) > TERMINAL_RETENTION_MS:
            pruned.append(next_s.sid)
            continue
        keepers.append(next_s)

    return PairSessionFile(version=file.version, sessions=keepers), pruned


def _clamp_ttl_ms(ttl_ms: Optional[int]) -> int:
    raw = ttl_ms if isinstance(ttl_ms, int) and ttl_ms > 0 else DEFAULT_PAIR_TTL_MS
    return max(MIN_PAIR_TTL_MS, min(MAX_PAIR_TTL_MS, raw))


# ---------------------------------------------------------------------------
# Public API — all operations go through the lock
# ---------------------------------------------------------------------------


def create_pair_session(
    sessions_path: Path,
    *,
    mode: PairSessionMode,
    operator_context: Optional[PairOperatorContext] = None,
    ttl_ms: Optional[int] = None,
    sk_b64: Optional[str] = None,
    pk_b64: Optional[str] = None,
    sid: Optional[str] = None,
    secondary_code: Optional[str] = None,
    now: Optional[NowFn] = None,
) -> PairSession:
    """Create and persist a new session. Returns the in-memory record.

    ``sk_b64``, ``pk_b64``, ``sid``, ``secondary_code`` default to fresh
    random values. Callers MUST pass the base64url-encoded public/private
    keys if they want crypto-capable sessions (the defaults are 32 bytes
    of random data — valid x25519 scalar space but useless for a real
    handshake). Normal usage: wire via :func:`generate_gateway_keypair`.
    """
    now_fn = now or default_now_ms
    t = now_fn()
    ttl = _clamp_ttl_ms(ttl_ms)
    op_ctx = operator_context or PairOperatorContext()

    session = PairSession(
        sid=sid or _default_sid(),
        sk_gateway_b64=sk_b64 or _b64url_placeholder(),
        pk_gateway_b64=pk_b64 or _b64url_placeholder(),
        created_at_ms=t,
        expires_at_ms=t + ttl,
        secondary_code=secondary_code or _default_secondary_code(),
        secondary_code_attempts=0,
        operator_context=op_ctx,
        mode=mode,
        status="awaiting_scan",
        last_status_change_at_ms=t,
    )

    release = _acquire_lock(sessions_path)
    try:
        current = _load_pair_sessions_file(sessions_path)
        pruned, _ = _prune_stale_sessions(current, t)
        pruned.sessions.append(session)
        _write_pair_sessions_file(sessions_path, pruned)
    finally:
        release()

    return session


def _b64url_placeholder() -> str:
    """Return a 32-byte random value base64url-encoded. Used when the caller
    didn't supply explicit key material (tests, no-crypto stubs). NOT a
    valid x25519 scalar for actual ECDH — callers expected to pass real
    keys from :func:`generate_gateway_keypair`."""
    return secrets.token_urlsafe(32).rstrip("=")[:43]


def get_pair_session(
    sessions_path: Path,
    sid: str,
    now: Optional[NowFn] = None,
) -> Optional[PairSession]:
    now_fn = now or default_now_ms
    release = _acquire_lock(sessions_path)
    try:
        file = _load_pair_sessions_file(sessions_path)
        pruned, pruned_sids = _prune_stale_sessions(file, now_fn())
        if pruned_sids:
            _write_pair_sessions_file(sessions_path, pruned)
        for s in pruned.sessions:
            if s.sid == sid:
                return s
        return None
    finally:
        release()


def _update_pair_session(
    sessions_path: Path,
    sid: str,
    mutate: Callable[[PairSession], Optional[PairSession]],
    now: Optional[NowFn] = None,
) -> Optional[PairSession]:
    now_fn = now or default_now_ms
    release = _acquire_lock(sessions_path)
    try:
        file = _load_pair_sessions_file(sessions_path)
        pruned, pruned_sids = _prune_stale_sessions(file, now_fn())
        idx = next((i for i, s in enumerate(pruned.sessions) if s.sid == sid), -1)
        if idx < 0:
            if pruned_sids:
                _write_pair_sessions_file(sessions_path, pruned)
            return None
        current = pruned.sessions[idx]
        nxt = mutate(current)
        if nxt is None:
            pruned.sessions.pop(idx)
            result: Optional[PairSession] = None
        else:
            pruned.sessions[idx] = nxt
            result = nxt
        _write_pair_sessions_file(sessions_path, pruned)
        return result
    finally:
        release()


def transition_pair_session(
    sessions_path: Path,
    sid: str,
    next_status: PairSessionStatus,
    now: Optional[NowFn] = None,
) -> Optional[PairSession]:
    now_fn = now or default_now_ms

    def mutate(s: PairSession) -> PairSession:
        if s.status == next_status:
            return s
        return PairSession(
            sid=s.sid,
            sk_gateway_b64=s.sk_gateway_b64,
            pk_gateway_b64=s.pk_gateway_b64,
            created_at_ms=s.created_at_ms,
            expires_at_ms=s.expires_at_ms,
            secondary_code=s.secondary_code,
            secondary_code_attempts=s.secondary_code_attempts,
            operator_context=s.operator_context,
            mode=s.mode,
            status=next_status,
            last_status_change_at_ms=now_fn(),
        )

    return _update_pair_session(sessions_path, sid, mutate, now_fn)


def register_failed_secondary_code(
    sessions_path: Path,
    sid: str,
    now: Optional[NowFn] = None,
) -> Optional[PairSession]:
    now_fn = now or default_now_ms

    def mutate(s: PairSession) -> PairSession:
        nxt_attempts = s.secondary_code_attempts + 1
        should_reject = nxt_attempts >= MAX_SECONDARY_CODE_ATTEMPTS
        return PairSession(
            sid=s.sid,
            sk_gateway_b64=s.sk_gateway_b64,
            pk_gateway_b64=s.pk_gateway_b64,
            created_at_ms=s.created_at_ms,
            expires_at_ms=s.expires_at_ms,
            secondary_code=s.secondary_code,
            secondary_code_attempts=nxt_attempts,
            operator_context=s.operator_context,
            mode=s.mode,
            status="rejected" if should_reject else s.status,
            last_status_change_at_ms=now_fn() if should_reject else s.last_status_change_at_ms,
        )

    return _update_pair_session(sessions_path, sid, mutate, now_fn)


@dataclass
class ConsumeResult:
    ok: bool
    error: Optional[str] = None
    session: Optional[PairSession] = None


def consume_pair_session(
    sessions_path: Path,
    sid: str,
    now: Optional[NowFn] = None,
) -> ConsumeResult:
    """Flip session to ``consumed`` and return the pre-transition record.

    Called by the HTTP ``/pair/respond`` handler BEFORE crypto work, so
    concurrent retries see ``already_consumed`` and the creds-write logic
    doesn't race.
    """
    now_fn = now or default_now_ms
    outcome: ConsumeResult = ConsumeResult(ok=False, error="not_found")

    def mutate(s: PairSession) -> PairSession:
        nonlocal outcome
        t = now_fn()
        if t > s.expires_at_ms:
            outcome = ConsumeResult(ok=False, error="expired")
            return PairSession(
                sid=s.sid,
                sk_gateway_b64=s.sk_gateway_b64,
                pk_gateway_b64=s.pk_gateway_b64,
                created_at_ms=s.created_at_ms,
                expires_at_ms=s.expires_at_ms,
                secondary_code=s.secondary_code,
                secondary_code_attempts=s.secondary_code_attempts,
                operator_context=s.operator_context,
                mode=s.mode,
                status="expired",
                last_status_change_at_ms=t,
            )
        if s.status in ("completed", "consumed"):
            outcome = ConsumeResult(ok=False, error="already_consumed")
            return s
        if s.status in ("rejected", "expired"):
            outcome = ConsumeResult(ok=False, error=s.status)
            return s
        outcome = ConsumeResult(ok=True, session=s)
        return PairSession(
            sid=s.sid,
            sk_gateway_b64=s.sk_gateway_b64,
            pk_gateway_b64=s.pk_gateway_b64,
            created_at_ms=s.created_at_ms,
            expires_at_ms=s.expires_at_ms,
            secondary_code=s.secondary_code,
            secondary_code_attempts=s.secondary_code_attempts,
            operator_context=s.operator_context,
            mode=s.mode,
            status="consumed",
            last_status_change_at_ms=t,
        )

    _update_pair_session(sessions_path, sid, mutate, now_fn)
    return outcome


def reject_pair_session(
    sessions_path: Path,
    sid: str,
    now: Optional[NowFn] = None,
) -> Optional[PairSession]:
    return transition_pair_session(sessions_path, sid, "rejected", now)


def list_active_pair_sessions(
    sessions_path: Path,
    now: Optional[NowFn] = None,
) -> List[PairSession]:
    now_fn = now or default_now_ms
    release = _acquire_lock(sessions_path)
    try:
        file = _load_pair_sessions_file(sessions_path)
        pruned, pruned_sids = _prune_stale_sessions(file, now_fn())
        if pruned_sids:
            _write_pair_sessions_file(sessions_path, pruned)
        return [s for s in pruned.sessions if s.status in ("awaiting_scan", "device_connected")]
    finally:
        release()


def redact_pair_session(s: PairSession) -> Dict[str, Any]:
    """Scrub sensitive fields for safe logging."""
    d = _session_to_dict(s)
    d["skGatewayB64"] = "[redacted]"
    d["secondaryCode"] = "[redacted]"
    return d
