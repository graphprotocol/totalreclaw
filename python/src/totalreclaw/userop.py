"""
ERC-4337 v0.7 UserOperation construction for TotalReclaw.

Builds, signs, and submits UserOperations through the relay bundler.
The relay proxies all bundler/paymaster JSON-RPC to Pimlico server-side,
so the Python client never needs a Pimlico API key.

Flow:
  1. Encode the DataEdge call as SmartAccount.execute(dataEdge, 0, protobuf)
  2. Get nonce from EntryPoint via public RPC
  3. Check if account is deployed (to set factory/factoryData if needed)
  4. Get gas prices from Pimlico (pimlico_getUserOperationGasPrice)
  5. Get paymaster sponsorship (pm_getPaymasterData)
  6. Sign the UserOp with the EOA private key
  7. Submit via eth_sendUserOperation

Pure computation (calldata encoding, UserOp hashing, ECDSA signing) delegates
to totalreclaw_core (Rust/PyO3). I/O (RPC calls, relay HTTP) stays in Python.

Key addresses (deployed on all supported chains):
  - EntryPoint v0.7:        0x0000000071727De22E5E9d8BAf0edAc6f37da032
  - SimpleAccountFactory:   0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985
  - DataEdge (staging+prod): 0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Optional

import httpx
from eth_hash.auto import keccak

import totalreclaw_core

from .grant import (
    SessionKeyPermissionGrant,
    encode_install_signature,
    sign_digest,
)
from .relay import (
    RelayWriteQuotaExceeded,
    _client_header_value,
    raise_for_bundler_status,
)

if TYPE_CHECKING:
    from .relay import RelayClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Well-known addresses
# ---------------------------------------------------------------------------

ENTRYPOINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
SIMPLE_ACCOUNT_FACTORY = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985"

# ---------------------------------------------------------------------------
# Test seams (internal #646 / #650)
# ---------------------------------------------------------------------------
#
# Every wait and deadline in this module goes through ``_now`` / ``_sleep`` /
# ``_rand`` so the unit tests can drive a virtual clock instead of sleeping.
# Production behaviour is unchanged: they are the real monotonic clock,
# ``asyncio.sleep`` and ``random.random``.
_now = time.monotonic
_sleep = asyncio.sleep
_rand = random.random


# ---------------------------------------------------------------------------
# Timing constants (module-level so tests can override them)
# ---------------------------------------------------------------------------

#: Poll cadence while waiting for a predecessor / our own op (plus jitter).
GATE_POLL_S: float = 1.5
#: After this long the poll cadence widens (a slow bundler is not hammered).
GATE_SLOW_POLL_AFTER_S: float = 20.0
GATE_SLOW_POLL_S: float = 5.0
#: Back-off when Pimlico's simulator looks one block behind the public RPC.
SIM_LAG_BACKOFF_S: tuple[float, ...] = (1.0, 2.0)
#: Give up on an unreadable public RPC after this long (fail fast, nothing sent).
RPC_DOWN_FAIL_FAST_S: float = 10.0
#: Two chain reads at least this far apart must both sit below the nonce floor
#: before the floor is reset to chain truth (reorg recovery).
REORG_CONFIRM_GAP_S: float = 10.0
#: An accepted-but-unresolved op older than this stops blocking the account.
PENDING_MAX_AGE_S: float = 600.0
#: Sends of one logical write (fee-bumped re-sends after a ``gone`` verdict
#: included). Unchanged from the old retry budget.
_MAX_NONCE_RETRIES: int = 3
#: Builds of one logical write, counting retries that never reached the
#: bundler (sponsor-stage back-offs).
_MAX_BUILD_ATTEMPTS: int = 6
#: Bound for a wait on someone else's op (foreign nonce holder / deployment).
FOREIGN_WAIT_S: float = 15.0

#: Deadline profiles. ``gate`` is the predecessor-gate window, measured from
#: the predecessor's ``accepted_at``. ``total`` is the budget of one call,
#: measured from call entry (lock wait included). ``batch_total`` is the
#: overall cap for a batch, which also waits for its own receipt.
PROFILES: dict[str, dict[str, float]] = {
    "interactive": {"gate": 30.0, "total": 60.0, "batch_total": 60.0},
    "background": {"gate": 90.0, "total": 150.0, "batch_total": 390.0},
}


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class NothingSentError(RuntimeError):
    """Base class: the call ended BEFORE anything reached the bundler.

    Nothing was sent and nothing was debited, so the caller may simply try
    again later. Callers that need to tell "nothing sent" from "maybe stored"
    catch this class (and :class:`UserOpAmbiguousError`)."""


class PredecessorPendingError(NothingSentError):
    """An earlier accepted op of this account is still unresolved, so a new
    write was not started. Nothing was sent."""


class AccountBusyError(NothingSentError):
    """The per-account lock was not free before the call's deadline. Nothing
    was sent."""


class WriteDeadlineError(NothingSentError):
    """The call's deadline ran out before any send. Nothing was sent."""


class UserOpDroppedError(RuntimeError):
    """The bundler accepted a UserOp but dropped it: it never executed and
    its nonce is unused (proven by two matching checks). Not stored."""


class UserOpRevertedError(RuntimeError):
    """A UserOp was mined but reverted (``receipt.success=false``). ERC-4337
    consumes the nonce even then. The facts were not stored; a new op is
    safe."""


class UserOpAmbiguousError(RuntimeError):
    """The write MAY be stored: an op was sent and its fate is not yet known.

    Callers must treat the facts as "maybe stored": record them in the write
    ledger and resolve the op hashes with :func:`resolve_fate` before any
    retry. Carries ``fact_ids``, ``hashes``, ``nonce`` and ``sent_block``."""

    def __init__(
        self,
        message: str,
        *,
        fact_ids: Optional[list[str]] = None,
        hashes: Optional[list[str]] = None,
        nonce: Optional[int] = None,
        sent_block: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.fact_ids: list[str] = list(fact_ids or [])
        self.hashes: list[str] = list(hashes or [])
        self.nonce = nonce
        self.sent_block = sent_block


# ---------------------------------------------------------------------------
# Per-account submission mutex — 3.3.1-rc.3 AA25 serialization
# ---------------------------------------------------------------------------
#
# Concurrent UserOp submissions for the SAME Smart Account used to race
# at the nonce fetch. The fix: serialize per-``sender`` with an
# ``asyncio.Lock`` so call B does not even START its nonce fetch until call A
# has settled. Per-sender dict lives in module scope; keys are lowercased
# addresses. Entries are never removed (bounded by # unique accounts per
# process, typically 1).
_sender_submission_locks: dict[str, asyncio.Lock] = {}
_sender_locks_map_lock = asyncio.Lock()


async def _get_sender_lock(sender: str) -> asyncio.Lock:
    """Return the asyncio.Lock for the given Smart Account address.

    Lazily creates the lock on first use. Locking the creation step itself
    prevents two coroutines from simultaneously allocating separate locks
    for the same sender.
    """
    key = sender.lower()
    async with _sender_locks_map_lock:
        lock = _sender_submission_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _sender_submission_locks[key] = lock
        return lock


def _reset_sender_locks_for_tests() -> None:
    """Clear the per-account lock map. Test-only helper."""
    _sender_submission_locks.clear()


class _Deadline:
    """Monotonic budget of one call. Starts at call entry, BEFORE the lock."""

    def __init__(
        self,
        profile: str,
        deadline_s: Optional[float],
        *,
        batch: bool,
    ) -> None:
        prof = PROFILES.get(profile) or PROFILES["background"]
        self.profile = profile if profile in PROFILES else "background"
        self.start = _now()
        if deadline_s is not None:
            total = float(deadline_s)
            self.send_by = self.start + total
            self.total = self.start + total
            self.gate_window = min(prof["gate"], total)
        else:
            self.send_by = self.start + prof["total"]
            self.total = self.start + (prof["batch_total"] if batch else prof["total"])
            self.gate_window = prof["gate"]

    def remaining(self) -> float:
        return self.total - _now()

    def send_remaining(self) -> float:
        return self.send_by - _now()


async def _acquire_sender_lock(lock: asyncio.Lock, deadline: _Deadline) -> None:
    """Take the per-account lock, or raise :class:`AccountBusyError`."""
    if not lock.locked():
        await lock.acquire()
        return
    try:
        await asyncio.wait_for(lock.acquire(), timeout=max(deadline.send_remaining(), 0.0))
    except asyncio.TimeoutError:
        raise AccountBusyError(
            "Another write for this account is still running; nothing was "
            "sent. Try again shortly."
        ) from None


# ---------------------------------------------------------------------------
# Per-account write state — internal #646 / #650
# ---------------------------------------------------------------------------
#
# The EntryPoint nonce only advances at ON-CHAIN EXECUTION, but a write used
# to return as soon as the bundler ACCEPTED it. The next write then built at
# a pipelined nonce (#423) that Pimlico's paymaster simulation rejects (AA25)
# once the predecessor is bundled but not mined (#646), or left out the factory
# because the deploy latch was set at accept time (AA20, #650).
#
# State is process-local and read/written only under the per-account lock:
#   pending[sender]  records of accepted ops whose fate is unknown when a call
#                    exits. The NEXT call resolves them BY HASH (the "gate").
#   stale[sender]    records past PENDING_MAX_AGE_S; still resolved
#                    opportunistically so a late "dead"/"gone" is reported.
#   landed_floor     highest ``nonce + 1`` observed EXECUTED. A stale read of
#                    the chain nonce can never lower the nonce below it.
#   deployed latch   set only on chain evidence (see below).


@dataclass
class _Attempt:
    """One send of one logical write."""

    hash: str
    nonce: int
    sent_block: int
    carried_factory: bool


@dataclass
class _PendingRecord:
    """An accepted op (or a write with several attempts) of unknown fate."""

    hashes: list[str]
    nonce: int
    sent_block: int
    carried_factory: bool
    fact_ids: list[str]
    accepted_at: float
    on_lost: Optional[Callable[[list[str]], Any]] = None
    #: The payloads of this write, kept so a write PROVEN replaced (dead) or
    #: dropped (gone) can be sent again. Only set where nobody else tracks the
    #: facts (a single write that returned at accept or at its deadline).
    payloads: list = field(default_factory=list)
    kind: str = "single"
    resend_ok: bool = False
    resends: int = 0


_pending: dict[str, list[_PendingRecord]] = {}
_stale: dict[str, list[_PendingRecord]] = {}
_landed_floor: dict[str, int] = {}


def _floor_of(sender: str) -> int:
    return _landed_floor.get(sender.lower(), 0)


def _raise_floor(sender: str, value: int) -> None:
    key = sender.lower()
    _landed_floor[key] = max(_landed_floor.get(key, 0), value)


def _reset_floor_to(sender: str, value: int) -> None:
    """Reorg recovery only: drop the floor to chain truth."""
    _landed_floor[sender.lower()] = value


def _reset_nonce_cache_for_tests() -> None:
    """Clear nonce floor and pending/stale records. Test-only helper."""
    _landed_floor.clear()
    _pending.clear()
    _stale.clear()


def _reset_write_state_for_tests() -> None:
    """Clear all per-account write state (floor, pending, stale, latch)."""
    _reset_nonce_cache_for_tests()
    _sender_deployed.clear()


def pending_records(sender: str) -> list[_PendingRecord]:
    """Snapshot of the unresolved records for ``sender`` (read-only)."""
    key = sender.lower()
    return list(_pending.get(key, [])) + list(_stale.get(key, []))


# ---------------------------------------------------------------------------
# Deployed-account latch — F3 / internal #435 (+ #650)
# ---------------------------------------------------------------------------
#
# A Smart Account deploys exactly once (its first UserOp carries the factory /
# initCode; every op after that must NOT). Public RPCs rate-limit under bursts
# and ``_eth_get_code`` used to read any RPC error as "0x" (= not deployed), so
# the factory was re-attached and the paymaster reverted the batch (#435). The
# fix is a monotonic per-sender "deployed" latch.
#
# #650: the latch is NO LONGER set when the bundler ACCEPTS a send. Accept is
# not execution: a second write that left out the factory while the deploy op
# was still unmined got AA20. The latch is now set only on chain evidence
# (code present, chain nonce > 0, an op of ours that carried the factory
# EXECUTED) and on the simulator-stage AA10 signal, where the simulator is the
# authority (#435).
_sender_deployed: set[str] = set()


def _mark_sender_deployed(sender: str) -> None:
    """Latch a sender as deployed."""
    _sender_deployed.add(sender.lower())


def _clear_sender_deployed(sender: str) -> None:
    """Un-latch a sender. Used when chain evidence shows the account has no
    code (AA20 with an undeployed chain, reorged deploy)."""
    _sender_deployed.discard(sender.lower())


def _is_sender_deployed_cached(sender: str) -> bool:
    return sender.lower() in _sender_deployed


def _reset_deployed_cache_for_tests() -> None:
    """Clear the deployed cache. Test-only helper."""
    _sender_deployed.clear()


# ---------------------------------------------------------------------------
# Error classification (Alto ``main`` strings, see plan section 4.4)
# ---------------------------------------------------------------------------

_MEMPOOL_AA10_MARKERS = (
    "already being processed",
    "already in the mempool",
    "another deployment operation",
    "conflicting useroperation with initcode",
)


def _is_mempool_aa10(err_str: str) -> bool:
    """Alto's mempool-stage AA10 (a second deploy op for the sender is
    queued). NOT a statement about chain state, so it must not latch."""
    low = (err_str or "").lower()
    return "AA10" in err_str and any(m in low for m in _MEMPOOL_AA10_MARKERS)


def _is_sender_state_error(err_str: str) -> bool:
    """True when the SIMULATOR says the account is ALREADY deployed (so
    re-attaching the factory is the actual fault): the paymaster revert
    "Sender does not implement validateUserOp or factory is not deployed"
    (#435) or the EntryPoint ``AA10 sender already constructed``.

    Mempool-stage AA10 (Alto telling us another deploy op is queued) is NOT a
    sender-state error: see :func:`_is_mempool_aa10`."""
    if not err_str or _is_mempool_aa10(err_str):
        return False
    return (
        "already constructed" in err_str
        or "AA10" in err_str
        or "validateUserOp or factory" in err_str
    )


def _is_not_deployed_error(err_str: str) -> bool:
    """EntryPoint AA20: the sender has no code and no factory was supplied."""
    return bool(err_str) and (
        "AA20" in err_str or "account not deployed" in err_str.lower()
    )


def _is_already_known(err_str: str) -> bool:
    return "Already known" in (err_str or "")


def _is_transient_sim_error(err_str: str) -> bool:
    """True for a ``-32500`` simulation error that is NOT a redeploy signal.
    AA20 / AA25 / AA10 are handled by their own branches first."""
    return bool(err_str) and "-32500" in err_str


def _is_shutdown_error(exc: BaseException) -> bool:
    """The host interpreter is shutting down (the httpx/anyio thread pool
    refuses new work). Nothing was sent; waits must stop at once instead of
    polling an RPC that can no longer be reached."""
    if type(exc).__name__ == "InterpreterShutdownError":
        return True
    return isinstance(exc, RuntimeError) and (
        "cannot schedule new futures after interpreter shutdown" in str(exc)
    )


class _StageError(RuntimeError):
    """A JSON-RPC rejection tagged with the stage that produced it. The
    message text is exactly what the old code raised."""

    def __init__(self, message: str, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


class _SendAmbiguous(RuntimeError):
    """The send call itself failed WITHOUT a JSON-RPC answer (transport error,
    timeout, HTTP error with no JSON-RPC body): the bundler may or may not
    hold the op."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"send outcome unknown: {type(cause).__name__}: {str(cause)[:200]}")
        self.cause = cause


async def _resolve_is_deployed(
    http: httpx.AsyncClient, rpc_url: str, sender: str, chain_nonce: int
) -> bool:
    """Decide whether ``sender`` is already deployed for THIS submission.

    Resolution order (cheapest, most-authoritative first):
      1. Deployed latch (set only from chain evidence, see above).
      2. ``chain_nonce > 0`` - the account has executed ops.
      3. ``eth_getCode`` - non-empty bytecode. A hardened ``_eth_get_code``
         raises on an RPC error instead of reading "0x".
    """
    key = sender.lower()
    if key in _sender_deployed:
        return True
    if chain_nonce > 0:
        _sender_deployed.add(key)
        return True
    code = await _eth_get_code(http, rpc_url, sender)
    if code != "0x" and len(code) > 2:
        _sender_deployed.add(key)
        return True
    return False


def _receipt_success(result: dict) -> Optional[bool]:
    """Interpret a UserOperation receipt's ``success`` field.

    Returns True/False when the receipt states success, or None when the
    receipt is absent / doesn't carry a decidable success flag. Tolerates
    both boolean and string ("true"/"false") shapes.
    """
    if not isinstance(result, dict):
        return None
    success = result.get("success")
    if isinstance(success, bool):
        return success
    if isinstance(success, str):
        low = success.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return None


async def _await_nonce_advance(
    http,
    sender: str,
    chain_id: int,
    min_nonce: int,
    timeout_s: float = 15.0,
    poll_s: float = 1.5,
) -> int:
    """Poll the EntryPoint nonce until it reaches ``min_nonce`` or timeout.
    RPC errors are tolerated. Returns the last nonce seen (may be below
    ``min_nonce`` on timeout)."""
    deadline = _now() + timeout_s
    last = -1
    while _now() < deadline:
        try:
            last = await get_nonce(http, sender, chain_id)
            if last >= min_nonce:
                return last
        except Exception as e:
            if _is_shutdown_error(e):
                raise
        await _sleep(poll_s)
    return last



DATA_EDGE_ADDRESS = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca"

# Chain-specific public RPCs
_CHAIN_RPCS = {
    84532: "https://sepolia.base.org",
    100: "https://rpc.gnosischain.com",
}

# ---------------------------------------------------------------------------
# Function selectors (keccak256 first 4 bytes)
# ---------------------------------------------------------------------------

# createAccount(address,uint256)
CREATE_ACCOUNT_SELECTOR = keccak(b"createAccount(address,uint256)")[:4].hex()
# getNonce(address,uint192)
GET_NONCE_SELECTOR = keccak(b"getNonce(address,uint192)")[:4].hex()


# ---------------------------------------------------------------------------
# ABI encoding helpers
# ---------------------------------------------------------------------------


def _pad32(hex_str: str) -> str:
    """Pad a hex string (with or without 0x) to 32 bytes (64 hex chars)."""
    return hex_str.replace("0x", "").zfill(64)


def _encode_uint256(value: int) -> str:
    """ABI-encode a uint256 as 64 hex chars."""
    return hex(value)[2:].zfill(64)


def encode_execute_calldata_for_data_edge(
    protobuf_payload: bytes,
    data_edge_address: str | None = None,
) -> str:
    """ABI-encode ``SimpleAccount.execute(dataEdge, 0, protobuf)`` calldata.

    Delegates to ``totalreclaw_core.encode_single_call()``. When
    ``data_edge_address`` is provided (the relay's authoritative
    ``data_edge_address`` from ``/v1/billing/status``, #366) the inner call
    targets that DataEdge; otherwise the core's default DataEdge is used.
    Returns 0x-prefixed hex.

    The optional 2nd arg is only passed through when set, so this stays
    compatible with a ``totalreclaw-core`` older than 2.5.0 (which does not
    accept it) on the default path.
    """
    if data_edge_address is not None:
        calldata_bytes = totalreclaw_core.encode_single_call(
            protobuf_payload, data_edge_address
        )
    else:
        calldata_bytes = totalreclaw_core.encode_single_call(protobuf_payload)
    return "0x" + calldata_bytes.hex()


# Max batch size mirrors the Rust ``MAX_BATCH_SIZE`` constant (30 since core
# 2.5.5, #392 Part 2) and the TS ``skill/plugin/store.ts`` batcher. 30/UserOp
# validated on Gnosis staging (Pimlico bundler accepts). MUST stay in sync with
# core MAX_BATCH_SIZE + the relay MAX_FACT_COUNT billing clamp.
MAX_BATCH_SIZE: int = 30


def encode_execute_batch_calldata_for_data_edge(
    protobuf_payloads: list[bytes],
    data_edge_address: str | None = None,
) -> str:
    """ABI-encode ``SimpleAccount.executeBatch(dests, values, datas)`` calldata.

    Delegates to :func:`totalreclaw_core.encode_batch_call`. When
    ``data_edge_address`` is provided (relay-authoritative, #366) every inner
    call targets that DataEdge; otherwise the core's default is used.
    The Rust core returns ``execute(...)`` calldata (not ``executeBatch``)
    when ``len(protobuf_payloads) == 1`` so a batch-of-1 is byte-identical
    to the single-fact fast path — this preserves gas parity with the TS
    plugin's ``encodeBatchCalls`` helper.

    Parameters
    ----------
    protobuf_payloads : list of bytes
        One raw protobuf payload per fact. Must contain
        1..:data:`MAX_BATCH_SIZE` entries.

    Returns
    -------
    str
        0x-prefixed hex calldata to assign to ``userOp["callData"]``.

    Raises
    ------
    ValueError
        If the batch is empty or exceeds :data:`MAX_BATCH_SIZE`. The
        Rust core raises the same errors; we defensively check here too
        so the failure mode is a stable Python ``ValueError`` regardless
        of core version.
    """
    if not protobuf_payloads:
        raise ValueError("Batch must contain at least 1 payload")
    if len(protobuf_payloads) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size {len(protobuf_payloads)} exceeds maximum of "
            f"{MAX_BATCH_SIZE}"
        )
    try:
        if data_edge_address is not None:
            calldata_bytes = totalreclaw_core.encode_batch_call(
                protobuf_payloads, data_edge_address
            )
        else:
            calldata_bytes = totalreclaw_core.encode_batch_call(
                protobuf_payloads
            )
    except ValueError as exc:
        # The batch passed our own ceiling check above, so a core-side
        # "exceeds maximum" means the installed wheel enforces a smaller
        # ceiling than this client — i.e. a totalreclaw-core older than
        # the pyproject floor (< 2.5.5 kept the pre-#392 ceiling of 15).
        if "exceeds maximum" in str(exc):
            raise ValueError(
                f"totalreclaw-core rejected a "
                f"{len(protobuf_payloads)}-fact batch that this client "
                f"allows (MAX_BATCH_SIZE={MAX_BATCH_SIZE}): {exc}. The "
                f"installed core wheel is older than the required floor "
                f"and enforces the pre-#392 ceiling — upgrade with "
                f"pip install -U 'totalreclaw-core>=2.5.5'."
            ) from exc
        raise
    return "0x" + calldata_bytes.hex()


def encode_factory_data(owner_address: str, salt: int = 0) -> str:
    """ABI-encode ``SimpleAccountFactory.createAccount(address, uint256)``."""
    owner = _pad32(owner_address)
    salt_hex = _encode_uint256(salt)
    return f"0x{CREATE_ACCOUNT_SELECTOR}{owner}{salt_hex}"


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------


def _rpc_url_for_chain(chain_id: int) -> str:
    url = _CHAIN_RPCS.get(chain_id)
    if not url:
        raise ValueError(f"No public RPC configured for chain {chain_id}")
    return url


async def _eth_call(
    http: httpx.AsyncClient,
    rpc_url: str,
    to: str,
    data: str,
    block: str = "latest",
) -> str:
    """Execute an ``eth_call`` and return the hex result.

    Raises on a JSON-RPC ``error``, a missing ``result`` or a non-hex
    ``result``. A malformed reply used to read as ``"0x0"`` (= nonce 0), the
    same misread class #435 fixed for ``eth_getCode``.
    """
    resp = await http.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": to, "data": data}, block],
            "id": 1,
        },
    )
    body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"eth_call failed: unexpected reply {str(body)[:200]}")
    if "error" in body:
        raise RuntimeError(f"eth_call failed: {body['error']}")
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        raise RuntimeError("eth_call returned no usable result")
    return result


async def _eth_get_code(
    http: httpx.AsyncClient, rpc_url: str, address: str
) -> str:
    """Return the deployed bytecode at ``address`` (``"0x"`` when none).

    Hardened for #435: an RPC error (or a body with no ``result``) is NOT
    read as ``"0x"`` — that silent misread let a rate-limited public RPC
    report a deployed account as undeployed, re-attaching the factory and
    reverting the whole batch. We retry once, then raise. The caller only
    reaches this when the deployed cache is cold AND chain_nonce == 0, so a
    genuine RPC outage propagating into the retry loop is the safe outcome.
    """
    last_err: Optional[str] = None
    for attempt in range(2):
        resp = await http.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "method": "eth_getCode",
                "params": [address, "latest"],
                "id": 1,
            },
        )
        body = resp.json()
        if isinstance(body, dict) and "error" not in body and body.get("result") is not None:
            return body["result"]
        last_err = str(body.get("error") if isinstance(body, dict) else body)
        logger.debug(
            "eth_getCode for %s returned no usable result (attempt %d/2): %s",
            address, attempt + 1, last_err,
        )
    raise RuntimeError(
        f"eth_getCode failed for {address} after 2 attempts: {last_err}"
    )


async def get_nonce(
    http: httpx.AsyncClient,
    sender: str,
    chain_id: int,
    block: Optional[int] = None,
) -> int:
    """Get the next nonce for a Smart Account from the EntryPoint.

    Uses ``eth_call`` to ``EntryPoint.getNonce(sender, 0)`` via a public RPC.
    ``block`` pins the read to one block number (default: ``"latest"``).
    """
    calldata = f"0x{GET_NONCE_SELECTOR}{_pad32(sender)}{_encode_uint256(0)}"
    rpc_url = _rpc_url_for_chain(chain_id)
    if block is None:
        result = await _eth_call(http, rpc_url, ENTRYPOINT_V07, calldata)
    else:
        result = await _eth_call(http, rpc_url, ENTRYPOINT_V07, calldata, hex(block))
    return int(result, 16)


# ---------------------------------------------------------------------------
# UserOp hash computation (v0.7) — delegates to Rust core
# ---------------------------------------------------------------------------


def compute_user_op_hash(
    user_op: dict, entry_point: str, chain_id: int
) -> bytes:
    """Compute the ERC-4337 v0.7 UserOperation hash for signing.

    Delegates to totalreclaw_core.hash_userop() (Rust/PyO3) which implements
    the canonical v0.7 packing and hashing.

    ``hash = keccak256(keccak256(packUserOp), entryPoint, chainId)``

    Note: The Rust struct requires a ``signature`` field (even though signature
    is excluded from the hash). If missing, a dummy value is injected.
    """
    # Ensure signature field exists — Rust serde requires it even though
    # the hash computation ignores it.
    op = user_op if "signature" in user_op else {**user_op, "signature": "0x"}
    userop_json = json.dumps(op)
    return bytes(totalreclaw_core.hash_userop(userop_json, entry_point, chain_id))


def sign_user_op_hash(
    user_op_hash: bytes, eoa_private_key: bytes
) -> str:
    """Sign a UserOp hash with the EOA private key (EIP-191 prefixed).

    Delegates to totalreclaw_core.sign_userop() (Rust/PyO3) which applies the
    Ethereum Signed Message prefix and returns a 65-byte signature (r+s+v).

    Returns 0x-prefixed hex signature.
    """
    sig_bytes = totalreclaw_core.sign_userop(user_op_hash, eoa_private_key)
    return "0x" + bytes(sig_bytes).hex()


# ---------------------------------------------------------------------------
# Relay JSON-RPC helpers
# ---------------------------------------------------------------------------


async def _relay_rpc(
    http: httpx.AsyncClient,
    relay_url: str,
    headers: dict,
    method: str,
    params: list,
    rpc_id: int = 1,
) -> dict:
    """Send a JSON-RPC request to the relay bundler endpoint."""
    resp = await http.post(
        f"{relay_url}/v1/bundler",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": rpc_id,
        },
    )
    # A write-quota refusal (403 quota_exceeded) raises the typed
    # ``RelayWriteQuotaExceeded`` (#747); anything else is unchanged.
    raise_for_bundler_status(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# Fate resolution by hash — internal #646 / #650
# ---------------------------------------------------------------------------
#
# "Nonce advanced" is not "our op landed": the nonce can be taken by another
# op (another process, a fee-bump replacement). So every question about an
# earlier op is answered BY HASH. ``resolve_fate`` is read-only.

#: topic0 of ``UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)``.
USER_OPERATION_EVENT_TOPIC = "0x" + keccak(
    b"UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
).hex()
#: ``eth_getLogs`` block-range chunk (public RPCs cap the range).
_LOGS_CHUNK_BLOCKS: int = 2000
_LOGS_MAX_CHUNKS: int = 25


class Fate(NamedTuple):
    """What happened to an op. ``kind`` is one of: ``executed`` (mined;
    ``success`` says whether it reverted; ``hash`` is the op that ran),
    ``dead`` (the nonce was used by another op, so ours can never execute),
    ``gone`` (the bundler dropped it and the nonce is unused), ``pending``,
    ``unknown``."""

    kind: str
    success: Optional[bool] = None
    hash: Optional[str] = None
    #: ``gone`` only: every status was a TERMINAL bundler state (``rejected`` /
    #: ``failed``), not ``not_found`` (which a lagging status index also says).
    terminal: bool = False


@dataclass
class _Ctx:
    """Per-call connection data (never logged)."""

    sender: str
    relay_url: str
    headers: dict
    chain_id: int
    rpc_url: str


async def _eth_block_number(http: httpx.AsyncClient, rpc_url: str) -> int:
    """Current block number from the public RPC. Raises on a bad reply."""
    resp = await http.post(
        rpc_url,
        json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
    )
    body = resp.json()
    if not isinstance(body, dict) or "error" in body:
        raise RuntimeError(f"eth_blockNumber failed: {str(body)[:200]}")
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RuntimeError("eth_blockNumber returned no usable result")
    return int(result, 16)


async def _find_userop_events(
    http: httpx.AsyncClient,
    rpc_url: str,
    hashes: list[str],
    from_block: int,
    to_block: int,
) -> dict[str, bool]:
    """``UserOperationEvent`` logs for ``hashes`` on the EntryPoint over
    ``[from_block, to_block]``. Returns ``{hash_lower: success}``. Raises on
    any RPC error or malformed reply (the caller then says ``unknown``)."""
    found: dict[str, bool] = {}
    wanted = [h.lower() for h in hashes]
    start = max(from_block, 0)
    chunks = 0
    while start <= to_block:
        chunks += 1
        if chunks > _LOGS_MAX_CHUNKS:
            raise RuntimeError("eth_getLogs range too large to scan")
        end = min(start + _LOGS_CHUNK_BLOCKS - 1, to_block)
        resp = await http.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "address": ENTRYPOINT_V07,
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": [USER_OPERATION_EVENT_TOPIC, wanted],
                }],
                "id": 1,
            },
        )
        body = resp.json()
        if not isinstance(body, dict) or "error" in body or not isinstance(body.get("result"), list):
            raise RuntimeError(f"eth_getLogs failed: {str(body)[:200]}")
        for log in body["result"]:
            topics = log.get("topics") or []
            data = log.get("data") or "0x"
            if len(topics) < 2 or not isinstance(data, str):
                raise RuntimeError("eth_getLogs returned a malformed log")
            raw = data[2:]
            if len(raw) < 128:
                raise RuntimeError("UserOperationEvent data too short")
            # data = nonce, success, actualGasCost, actualGasUsed (32 bytes each)
            found[str(topics[1]).lower()] = int(raw[64:128], 16) != 0
        start = end + 1
    return found


_STATUS_PENDING = {"not_submitted", "submitted", "queued", "included"}
_STATUS_GONE = {"rejected", "failed", "not_found"}


async def _fate_once(
    http: httpx.AsyncClient,
    ctx: _Ctx,
    hashes: list[str],
    nonce: int,
    sent_block: int,
) -> Fate:
    """One pass of the section-4.2 decision procedure."""
    # 1. Fast path: a receipt by hash.
    for h in hashes:
        try:
            resp = await _relay_rpc(
                http, ctx.relay_url, ctx.headers,
                "eth_getUserOperationReceipt", [h], rpc_id=4,
            )
        except Exception as e:
            if isinstance(e, RelayWriteQuotaExceeded) or _is_shutdown_error(e):
                raise
            logger.debug("receipt lookup failed (%s)", type(e).__name__)
            continue
        result = resp.get("result") if isinstance(resp, dict) else None
        verdict = _receipt_success(result)
        if verdict is not None:
            return Fate("executed", verdict, h)
    # 2. Chain nonce and logs, both at ONE block so a lagging replica cannot
    #    fake "dead".
    try:
        block = await _eth_block_number(http, ctx.rpc_url)
        chain_nonce = await get_nonce(http, ctx.sender, ctx.chain_id, block=block)
    except Exception as e:
        if _is_shutdown_error(e):
            raise
        logger.debug("fate chain read failed (%s)", type(e).__name__)
        return Fate("unknown")
    if chain_nonce > nonce:
        try:
            events = await _find_userop_events(
                http, ctx.rpc_url, hashes, max(sent_block - 2, 0), block
            )
        except Exception as e:
            if _is_shutdown_error(e):
                raise
            logger.debug("fate log read failed (%s)", type(e).__name__)
            return Fate("unknown")
        for h in hashes:
            if h.lower() in events:
                return Fate("executed", events[h.lower()], h)
        return Fate("dead")
    # 3. The nonce is still free: ask the bundler.
    kinds: list[str] = []
    all_terminal = True
    for h in hashes:
        try:
            resp = await _relay_rpc(
                http, ctx.relay_url, ctx.headers,
                "pimlico_getUserOperationStatus", [h], rpc_id=5,
            )
        except Exception as e:
            if isinstance(e, RelayWriteQuotaExceeded) or _is_shutdown_error(e):
                raise
            return Fate("unknown")
        result = resp.get("result") if isinstance(resp, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        if status in _STATUS_PENDING:
            kinds.append("pending")
        elif status in _STATUS_GONE:
            kinds.append("gone")
            if status not in ("rejected", "failed"):
                all_terminal = False
        else:
            return Fate("unknown")
    if "pending" in kinds:
        return Fate("pending")
    return Fate("gone", terminal=all_terminal)


async def resolve_fate(
    http: httpx.AsyncClient,
    ctx: _Ctx,
    hashes: list[str],
    nonce: int,
    sent_block: int,
) -> Fate:
    """Resolve the fate of the op attempts ``hashes`` (all at ``nonce``).

    ``dead`` and ``gone`` are the two verdicts that release facts for
    re-storing, so each must be seen twice, ``GATE_POLL_S`` apart, before it
    is returned (a read replica or the bundler's status index can lag).
    """
    first = await _fate_once(http, ctx, hashes, nonce, sent_block)
    if first.kind not in ("dead", "gone"):
        return first
    await _sleep(GATE_POLL_S)
    second = await _fate_once(http, ctx, hashes, nonce, sent_block)
    if second.kind == first.kind or second.kind == "executed":
        if second.kind == "gone":
            return Fate("gone", terminal=first.terminal and second.terminal)
        return second
    return second if second.kind == "pending" else Fate("unknown")


def _poll_delay(elapsed: float) -> float:
    base = GATE_POLL_S if elapsed < GATE_SLOW_POLL_AFTER_S else GATE_SLOW_POLL_S
    return base + _rand() * 0.5


def _apply_executed(
    sender: str, nonce: int, carried_factory: bool, success: Optional[bool]
) -> None:
    """Chain evidence that an op at ``nonce`` executed."""
    _raise_floor(sender, nonce + 1)
    if carried_factory or nonce > 0:
        # An executed op (even one whose inner call reverted) that carried the
        # factory deployed the account; any executed op above nonce 0 implies it.
        _mark_sender_deployed(sender)


async def _call_on_lost(cb: Optional[Callable[[list[str]], Any]], fact_ids: list[str]) -> None:
    if cb is None or not fact_ids:
        return
    try:
        res = cb(list(fact_ids))
        if inspect.isawaitable(res):
            await res
    except Exception:  # bookkeeping must never break the write path
        logger.exception("on_lost callback failed")


# ---------------------------------------------------------------------------
# Predecessor gate (start of every call, after the lock, before the nonce read)
# ---------------------------------------------------------------------------


async def _settle_pending(
    http: httpx.AsyncClient,
    ctx: _Ctx,
    deadline: _Deadline,
    lost_sink: Optional[list] = None,
) -> None:
    """Resolve earlier accepted ops of this account BY HASH.

    The gate never opens on "nonce advanced" alone. Raises
    :class:`PredecessorPendingError` (nothing sent) when a predecessor is
    still unresolved after the gate window (measured from ITS accepted_at),
    after the call's deadline, or after ``RPC_DOWN_FAIL_FAST_S`` of unreadable
    chain state.
    """
    key = ctx.sender.lower()
    for rec in list(_pending.get(key, [])):
        unknown_since: Optional[float] = None
        while True:
            fate = await resolve_fate(http, ctx, rec.hashes, rec.nonce, rec.sent_block)
            if fate.kind == "executed":
                _apply_executed(ctx.sender, rec.nonce, rec.carried_factory, fate.success)
                if fate.success is False:
                    logger.warning(
                        "earlier write %s executed but reverted; facts %s were not stored",
                        fate.hash, rec.fact_ids,
                    )
                    await _call_on_lost(rec.on_lost, rec.fact_ids)
                _drop_record(_pending, key, rec)
                break
            if fate.kind in ("dead", "gone"):
                proven = fate.kind == "dead" or fate.terminal
                if fate.kind == "dead":
                    logger.error(
                        "accepted write %s was replaced by another op at nonce %d; "
                        "facts %s were not stored",
                        rec.hashes[-1], rec.nonce, rec.fact_ids,
                    )
                else:
                    logger.error(
                        "accepted write %s was dropped by the bundler at nonce %d; "
                        "facts %s were not stored",
                        rec.hashes[-1], rec.nonce, rec.fact_ids,
                    )
                await _call_on_lost(rec.on_lost, rec.fact_ids)
                _drop_record(_pending, key, rec)
                if lost_sink is not None and rec.resend_ok and rec.resends < 1 and proven:
                    lost_sink.append(rec)
                break
            now = _now()
            if now - rec.accepted_at >= PENDING_MAX_AGE_S:
                logger.error(
                    "earlier write %s still unresolved after %.0fs; no longer "
                    "blocking this account (it may still land)",
                    rec.hashes[-1], now - rec.accepted_at,
                )
                _drop_record(_pending, key, rec)
                _stale.setdefault(key, []).append(rec)
                break
            if fate.kind == "unknown":
                unknown_since = unknown_since if unknown_since is not None else now
                if now - unknown_since >= RPC_DOWN_FAIL_FAST_S:
                    raise PredecessorPendingError(
                        "An earlier write for this account could not be "
                        "checked (chain data unreadable); nothing was sent."
                    )
            else:
                unknown_since = None
            window_end = min(rec.accepted_at + deadline.gate_window, deadline.send_by)
            if now >= window_end:
                raise PredecessorPendingError(
                    "An earlier write for this account is still being "
                    "confirmed on-chain; nothing was sent. Try again shortly."
                )
            await _sleep(_poll_delay(now - rec.accepted_at))
    # Stale records: one cheap, non-blocking check each.
    for rec in list(_stale.get(key, [])):
        try:
            fate = await resolve_fate(http, ctx, rec.hashes, rec.nonce, rec.sent_block)
        except Exception as e:
            if isinstance(e, RelayWriteQuotaExceeded) or _is_shutdown_error(e):
                raise
            continue
        if fate.kind == "executed":
            _apply_executed(ctx.sender, rec.nonce, rec.carried_factory, fate.success)
            if fate.success is False:
                await _call_on_lost(rec.on_lost, rec.fact_ids)
            _drop_record(_stale, key, rec)
            if lost_sink is not None and rec.resend_ok and rec.resends < 1 and (
                fate.kind == "dead" or fate.terminal
            ):
                lost_sink.append(rec)
        elif fate.kind in ("dead", "gone"):
            logger.error(
                "stale write %s is %s; facts %s were not stored",
                rec.hashes[-1], fate.kind, rec.fact_ids,
            )
            await _call_on_lost(rec.on_lost, rec.fact_ids)
            _drop_record(_stale, key, rec)


def _drop_record(table: dict[str, list[_PendingRecord]], key: str, rec: _PendingRecord) -> None:
    lst = table.get(key)
    if lst and rec in lst:
        lst.remove(rec)
        if not lst:
            table.pop(key, None)


def _park(
    sender: str,
    in_flight: list[_Attempt],
    fact_ids: list[str],
    on_lost: Optional[Callable[[list[str]], Any]],
    *,
    payloads: Optional[list] = None,
    kind: str = "single",
    resend_ok: bool = False,
    resends: int = 0,
) -> _PendingRecord:
    """Record an accepted write whose fate is unknown when the call exits."""
    last = in_flight[-1]
    rec = _PendingRecord(
        hashes=[a.hash for a in in_flight],
        nonce=last.nonce,
        sent_block=min(a.sent_block for a in in_flight),
        carried_factory=any(a.carried_factory for a in in_flight),
        fact_ids=list(fact_ids),
        accepted_at=_now(),
        on_lost=on_lost,
        payloads=list(payloads or []),
        kind=kind,
        resend_ok=resend_ok and bool(payloads),
        resends=resends,
    )
    _pending.setdefault(sender.lower(), []).append(rec)
    return rec


async def settle_pending_writes(
    sender: str,
    relay_url: str,
    auth_key_hex: str,
    wallet_address: str,
    chain_id: int = 84532,
    client_id: str = "python-client",
    session_id: Optional[str] = None,
    profile: str = "background",
    deadline_s: Optional[float] = None,
    *,
    eoa_address: Optional[str] = None,
    eoa_private_key: Optional[bytes] = None,
    data_edge_address: Optional[str] = None,
) -> None:
    """Resolve this account's earlier accepted writes (and apply their
    ``on_lost`` callbacks) WITHOUT sending anything of its own. Callers run
    this before their own duplicate gate so a ledger entry for a dead op
    cannot make the gate skip a fact that was never stored. Raises
    :class:`PredecessorPendingError` / :class:`AccountBusyError` (nothing
    sent).

    When the signing identity is given (``eoa_address``, ``eoa_private_key``)
    a write PROVEN replaced or dropped is sent again (once), exactly as the
    next write would do."""
    relay_url = relay_url.rstrip("/")
    ctx = _make_ctx(sender, relay_url, auth_key_hex, wallet_address, chain_id, client_id, session_id)
    deadline = _Deadline(profile, deadline_s, batch=False)
    lost: list = []
    lock = await _get_sender_lock(sender)
    await _acquire_sender_lock(lock, deadline)
    try:
        if not pending_records(sender):
            return
        async with httpx.AsyncClient(timeout=30.0) as http:
            await _settle_pending(http, ctx, deadline, lost)
    finally:
        lock.release()
    if lost and eoa_private_key is not None and eoa_address is not None:
        await _resend_lost(lost, dict(
            sender=sender, eoa_address=eoa_address, eoa_private_key=eoa_private_key,
            relay_url=relay_url, auth_key_hex=auth_key_hex, wallet_address=wallet_address,
            chain_id=chain_id, client_id=client_id, session_id=session_id,
            data_edge_address=data_edge_address, profile=profile,
        ))


async def _resend_lost(lost: list, base: dict) -> None:
    """Send again (once) the writes a gate PROVED replaced (dead) or dropped
    (gone): their nonce went to another op, or the bundler dropped them and the
    nonce is unused, so they can never execute and a second send cannot write
    them twice. Same payloads, so the same fact ids. Failures are logged; the
    ledger was already released by ``on_lost``."""
    for rec in lost:
        logger.warning(
            "re-sending write %s (proven %s): facts %s",
            rec.hashes[-1], "lost", rec.fact_ids,
        )
        try:
            await _submit_userop_once(
                kind=rec.kind, payloads=list(rec.payloads), confirm="accept",
                deadline_s=None, fact_ids=list(rec.fact_ids), on_lost=rec.on_lost,
                lost_sink=None, resends=rec.resends + 1, **base,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("re-send of lost write %s failed (facts %s): %s", rec.hashes[-1], rec.fact_ids, str(e)[:200])


def _make_ctx(
    sender: str,
    relay_url: str,
    auth_key_hex: str,
    wallet_address: str,
    chain_id: int,
    client_id: str,
    session_id: Optional[str],
) -> _Ctx:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_key_hex}",
        "X-TotalReclaw-Client": _client_header_value(client_id),
        "X-Wallet-Address": wallet_address,
    }
    if session_id:
        headers["X-TotalReclaw-Session"] = session_id
    return _Ctx(sender, relay_url, headers, chain_id, _rpc_url_for_chain(chain_id))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_STUB_SIGNATURE = (
    "0x"
    "fffffffffffffffffffffffffffffff000000000000000000000000000000000"
    "7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "1c"
)


def _jsonrpc_error_from_http(exc: BaseException) -> Optional[Any]:
    """The JSON-RPC ``error`` object of an HTTP error response, or None when
    the body is not a JSON-RPC error (then the outcome is unknown)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and ("code" in err or "message" in err):
            return err
    return None


async def _build_sign_send(
    http: httpx.AsyncClient,
    ctx: _Ctx,
    *,
    eoa_address: str,
    eoa_private_key: bytes,
    call_data: str,
    nonce: int,
    is_deployed: bool,
    sponsor_fail_msg: str,
    send_fail_msg: str,
    att: dict,
) -> str:
    """Steps 4-8 of the write: build, gas price, sponsor, sign, send.

    This is the single place a UserOp is built, sponsored, signed and sent;
    both the single and the batch writer run it, as does every re-send at a
    pinned nonce. Returns the LOCALLY computed v0.7 hash (the hash does not
    cover the signature, so it equals the bundler's). ``att`` is filled with
    ``hash`` before the send and ``sending`` while the send is in progress.
    """
    user_op: dict = {
        "sender": ctx.sender,
        "nonce": hex(nonce),
        "callData": call_data,
        "signature": _STUB_SIGNATURE,
    }

    if not is_deployed:
        user_op["factory"] = SIMPLE_ACCOUNT_FACTORY
        user_op["factoryData"] = encode_factory_data(eoa_address)
        logger.debug("Account not deployed; including factory data")

    # Gas prices from Pimlico
    gas_data = await _relay_rpc(
        http, ctx.relay_url, ctx.headers, "pimlico_getUserOperationGasPrice", [], rpc_id=1,
    )
    if "result" in gas_data and gas_data["result"].get("fast"):
        fast = gas_data["result"]["fast"]
        user_op["maxFeePerGas"] = fast["maxFeePerGas"]
        user_op["maxPriorityFeePerGas"] = fast["maxPriorityFeePerGas"]
    else:
        user_op["maxFeePerGas"] = hex(2_000_000_000)
        user_op["maxPriorityFeePerGas"] = hex(1_500_000_000)

    # Sponsor via pm_sponsorUserOperation: gas limits AND paymaster fields.
    sponsor_data = await _relay_rpc(
        http, ctx.relay_url, ctx.headers, "pm_sponsorUserOperation",
        [user_op, ENTRYPOINT_V07], rpc_id=2,
    )
    if "result" in sponsor_data:
        sp = sponsor_data["result"]
        user_op["callGasLimit"] = sp["callGasLimit"]
        user_op["verificationGasLimit"] = sp["verificationGasLimit"]
        user_op["preVerificationGas"] = sp["preVerificationGas"]
        user_op["paymaster"] = sp["paymaster"]
        user_op["paymasterData"] = sp["paymasterData"]
        user_op["paymasterVerificationGasLimit"] = sp["paymasterVerificationGasLimit"]
        user_op["paymasterPostOpGasLimit"] = sp["paymasterPostOpGasLimit"]
    elif "error" in sponsor_data:
        raise _StageError(f"{sponsor_fail_msg}{sponsor_data['error']}", "sponsor")
    else:
        raise _StageError("pm_sponsorUserOperation returned no result or error", "sponsor")

    # Hash and sign with the EOA key (Rust core; EIP-191 prefix applied there).
    user_op_hash = compute_user_op_hash(user_op, ENTRYPOINT_V07, ctx.chain_id)
    user_op["signature"] = sign_user_op_hash(user_op_hash, eoa_private_key)
    local_hash = "0x" + user_op_hash.hex()
    att["hash"] = local_hash
    # The block the op can first land in: fate lookups scan logs from here.
    att["sent_block"] = await _eth_block_number(http, ctx.rpc_url)

    # Submit the signed UserOp to the bundler.
    att["sending"] = True
    try:
        send_data = await _relay_rpc(
            http, ctx.relay_url, ctx.headers, "eth_sendUserOperation",
            [user_op, ENTRYPOINT_V07], rpc_id=3,
        )
    except RelayWriteQuotaExceeded:
        att["sending"] = False
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _is_shutdown_error(exc):
            att["sending"] = False
            raise
        err = _jsonrpc_error_from_http(exc)
        if err is not None:
            att["sending"] = False
            raise _StageError(f"{send_fail_msg}{err}", "send") from exc
        raise _SendAmbiguous(exc) from exc
    att["sending"] = False
    if "error" in send_data:
        raise _StageError(f"{send_fail_msg}{send_data['error']}", "send")
    returned = send_data.get("result", "")
    if not returned:
        logger.warning("bundler returned no op hash; using the locally computed %s", local_hash)
    elif isinstance(returned, str) and returned.lower() != local_hash.lower():
        logger.warning(
            "bundler op hash %s differs from the local hash %s; tracking the local one",
            returned, local_hash,
        )
    return local_hash


async def _chain_deployed(http: httpx.AsyncClient, ctx: _Ctx) -> Optional[bool]:
    """Fresh chain evidence that the account is deployed (code present or
    nonce > 0). ``None`` when the chain cannot be read."""
    try:
        n = await get_nonce(http, ctx.sender, ctx.chain_id)
        if n > 0:
            return True
        code = await _eth_get_code(http, ctx.rpc_url, ctx.sender)
        return code != "0x" and len(code) > 2
    except Exception as e:
        if _is_shutdown_error(e):
            raise
        return None


async def _await_deploy_evidence(http: httpx.AsyncClient, ctx: _Ctx, timeout_s: float) -> bool:
    end = _now() + max(timeout_s, 0.0)
    while True:
        if await _chain_deployed(http, ctx):
            return True
        if _now() >= end:
            return False
        await _sleep(_poll_delay(0.0))


async def _await_fate(
    http: httpx.AsyncClient,
    ctx: _Ctx,
    in_flight: list[_Attempt],
    deadline: _Deadline,
) -> Fate:
    """Poll :func:`resolve_fate` for the call's attempts until a verdict or
    the call's deadline. Returns the verdict, or ``Fate('timeout')``."""
    hashes = [a.hash for a in in_flight]
    nonce = in_flight[-1].nonce
    sent_block = min(a.sent_block for a in in_flight)
    start = _now()
    while True:
        fate = await resolve_fate(http, ctx, hashes, nonce, sent_block)
        if fate.kind in ("executed", "dead", "gone"):
            return fate
        if deadline.remaining() <= 0:
            return Fate("timeout")
        await _sleep(_poll_delay(_now() - start))


async def _submit_userop(**kw) -> str:
    """:func:`_submit_userop_once`, then a re-send (once) of every earlier write
    of this account that the gate proved replaced or dropped (#646/#650)."""
    lost: list = []
    base = {k: kw[k] for k in (
        "sender", "eoa_address", "eoa_private_key", "relay_url", "auth_key_hex",
        "wallet_address", "chain_id", "client_id", "session_id", "data_edge_address", "profile",
    )}
    try:
        result = await _submit_userop_once(**kw, lost_sink=lost, resends=0)
    except Exception:
        if lost:
            await _resend_lost(lost, base)
        raise
    if lost:
        await _resend_lost(lost, base)
    return result


async def _submit_userop_once(
    *,
    kind: str,
    sender: str,
    eoa_address: str,
    eoa_private_key: bytes,
    payloads: list[bytes],
    relay_url: str,
    auth_key_hex: str,
    wallet_address: str,
    chain_id: int,
    client_id: str,
    session_id: Optional[str],
    data_edge_address: Optional[str],
    confirm: str,
    profile: str,
    deadline_s: Optional[float],
    fact_ids: Optional[list[str]],
    on_lost: Optional[Callable[[list[str]], Any]],
    lost_sink: Optional[list] = None,
    resends: int = 0,
) -> str:
    """Shared writer behind :func:`build_and_send_userop` (``kind="single"``)
    and :func:`build_and_send_userop_batch` (``kind="batch"``)."""
    batch = kind == "batch"
    if confirm not in ("accept", "receipt"):
        raise ValueError(f"confirm must be 'accept' or 'receipt', got {confirm!r}")
    deadline = _Deadline(profile, deadline_s, batch=batch)
    relay_url = relay_url.rstrip("/")
    ctx = _make_ctx(sender, relay_url, auth_key_hex, wallet_address, chain_id, client_id, session_id)
    facts = list(fact_ids or [])
    key = sender.lower()
    lock = await _get_sender_lock(sender)
    await _acquire_sender_lock(lock, deadline)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            if batch:
                call_data = encode_execute_batch_calldata_for_data_edge(payloads, data_edge_address)
                sponsor_fail = "Batch paymaster sponsorship failed: "
                send_fail = "Batch UserOp submission failed: "
            else:
                call_data = encode_execute_calldata_for_data_edge(payloads[0], data_edge_address)
                sponsor_fail = "Paymaster sponsorship failed: "
                send_fail = "UserOp submission failed: "

            await _settle_pending(http, ctx, deadline, lost_sink)

            in_flight: list[_Attempt] = []
            pinned: Optional[int] = None
            gone_confirmed = False
            sends = 0
            soft_retries = 0
            sim_backoffs = 0
            last_exc: Optional[BaseException] = None
            builds = 0

            def _ambiguous(msg: str) -> UserOpAmbiguousError:
                rec = _park(sender, in_flight, facts, on_lost)
                return UserOpAmbiguousError(
                    msg, fact_ids=facts, hashes=rec.hashes,
                    nonce=rec.nonce, sent_block=rec.sent_block,
                )

            while True:
                if builds >= _MAX_BUILD_ATTEMPTS:
                    break
                if deadline.send_remaining() <= 0 and not in_flight:
                    raise WriteDeadlineError(
                        "The write ran out of time before anything was sent."
                    )
                builds += 1
                att: dict = {"hash": None, "sending": False, "sent_block": 0}
                chain_nonce = -1
                nonce = -1
                is_deployed = True
                sent_block = 0
                sent = False        # a send reached (or may have reached) the bundler
                ambiguous_send = False
                try:
                    chain_nonce = await get_nonce(http, sender, chain_id)
                    floor = _floor_of(sender)
                    nonce = pinned if pinned is not None else max(chain_nonce, floor)
                    logger.debug("Nonce for %s: chain=%d floor=%d using=%d", sender, chain_nonce, floor, nonce)
                    is_deployed = await _resolve_is_deployed(http, ctx.rpc_url, sender, chain_nonce)
                    local_hash = await _build_sign_send(
                        http, ctx,
                        eoa_address=eoa_address, eoa_private_key=eoa_private_key,
                        call_data=call_data, nonce=nonce, is_deployed=is_deployed,
                        sponsor_fail_msg=sponsor_fail, send_fail_msg=send_fail, att=att,
                    )
                    sends += 1
                    sent = True
                    sent_block = att["sent_block"]
                    logger.info(
                        "userop accepted hash=%s nonce=%d factory=%s", local_hash, nonce, not is_deployed,
                    )
                except _SendAmbiguous as sa:
                    sends += 1
                    sent = True
                    ambiguous_send = True
                    sent_block = att["sent_block"]
                    local_hash = att["hash"]
                    logger.warning(
                        "send outcome unknown (stage=send nonce=%d chain_nonce=%d factory=%s hash=%s): %s",
                        nonce, chain_nonce, not is_deployed, local_hash, str(sa)[:200],
                    )
                except RelayWriteQuotaExceeded:
                    if in_flight and not gone_confirmed:
                        _park(sender, in_flight, facts, on_lost)
                    raise
                except asyncio.CancelledError:
                    if in_flight and not gone_confirmed:
                        _park(sender, in_flight, facts, on_lost)
                    raise
                except Exception as e:
                    if _is_shutdown_error(e):
                        if in_flight and not gone_confirmed:
                            _park(sender, in_flight, facts, on_lost)
                        raise
                    last_exc = e
                    err_str = str(e)
                    stage = getattr(e, "stage", None) or "pre"
                    logger.warning(
                        "write attempt failed (stage=%s nonce=%d chain_nonce=%d factory=%s hash=%s build=%d/%d): %s",
                        stage, nonce, chain_nonce, not is_deployed, att.get("hash"),
                        builds, _MAX_BUILD_ATTEMPTS, err_str[:200],
                    )
                    # --- send stage: is the conflict OUR OWN op? ------------
                    if stage == "send":
                        ours_known = _is_already_known(err_str)
                        conflict = "AA25" in err_str or "AA10" in err_str
                        if ours_known or (in_flight and conflict):
                            if ours_known and att["hash"] and att["hash"] not in [a.hash for a in in_flight]:
                                in_flight.append(_Attempt(att["hash"], nonce, att["sent_block"], not is_deployed))
                                pinned = nonce
                            local_hash = None   # no new attempt; fate loop on what we have
                            sent = True
                        elif "AA25" in err_str:
                            # Someone else holds nonce k (ours was definitely rejected).
                            await _await_nonce_advance(
                                http, sender, chain_id, min_nonce=nonce + 1,
                                timeout_s=max(min(FOREIGN_WAIT_S, deadline.send_remaining()), 0.0),
                                poll_s=GATE_POLL_S,
                            )
                            continue
                        elif _is_mempool_aa10(err_str):
                            # A second deploy op for this sender is queued. Not a
                            # chain statement: do NOT latch; wait for evidence.
                            await _await_deploy_evidence(
                                http, ctx, min(FOREIGN_WAIT_S, max(deadline.send_remaining(), 0.0))
                            )
                            continue
                    if not sent:
                        if "AA25" in err_str:
                            # Sponsor-stage AA25: Pimlico's simulator is a block
                            # behind, or another process's op is bundled but not
                            # mined. NOT a reason to wait for nonce + 1.
                            floor_now = _floor_of(sender)
                            if nonce == floor_now and floor_now > chain_nonce >= 0:
                                # Our floor is ahead of the chain. A lagging read, or a
                                # reorg? Confirm with a second read REORG_CONFIRM_GAP_S later.
                                await _sleep(REORG_CONFIRM_GAP_S)
                                again = await get_nonce(http, sender, chain_id)
                                if again < floor_now and chain_nonce < floor_now:
                                    logger.error(
                                        "nonce floor %d is ahead of the chain (%d, then %d) on two "
                                        "reads %.0fs apart; assuming a reorg and resetting to chain truth",
                                        floor_now, chain_nonce, again, REORG_CONFIRM_GAP_S,
                                    )
                                    _reset_floor_to(sender, again)
                                    _clear_sender_deployed(sender)
                                continue
                            if sim_backoffs < len(SIM_LAG_BACKOFF_S):
                                await _sleep(SIM_LAG_BACKOFF_S[sim_backoffs])
                                sim_backoffs += 1
                            else:
                                await _await_nonce_advance(
                                    http, sender, chain_id, min_nonce=nonce + 1,
                                    timeout_s=max(min(6.0, deadline.send_remaining()), 0.0),
                                    poll_s=GATE_POLL_S,
                                )
                            continue
                        if _is_not_deployed_error(err_str):
                            evidence = await _chain_deployed(http, ctx)
                            if evidence:
                                # The chain says deployed: the simulator is behind.
                                _mark_sender_deployed(sender)
                                await _sleep(SIM_LAG_BACKOFF_S[min(sim_backoffs, len(SIM_LAG_BACKOFF_S) - 1)])
                                sim_backoffs += 1
                                continue
                            was_latched = _is_sender_deployed_cached(sender)
                            _clear_sender_deployed(sender)
                            carried = any(r.carried_factory for r in pending_records(sender))
                            if carried:
                                await _await_deploy_evidence(
                                    http, ctx, min(FOREIGN_WAIT_S, max(deadline.send_remaining(), 0.0))
                                )
                            elif not was_latched:
                                await _sleep(SIM_LAG_BACKOFF_S[0])
                            continue
                        retryable = soft_retries < _MAX_NONCE_RETRIES - 1
                        if _is_sender_state_error(err_str) and retryable:
                            # #435: the SIMULATOR says the account already exists.
                            soft_retries += 1
                            logger.warning(
                                "sender-state/redeploy error (stage=%s) - marking deployed and "
                                "retrying without factory: %s", stage, err_str[:200],
                            )
                            _mark_sender_deployed(sender)
                            continue
                        if _is_transient_sim_error(err_str) and retryable:
                            soft_retries += 1
                            logger.warning(
                                "transient -32500 sim error (stage=%s) - retrying with unchanged "
                                "deploy state: %s", stage, err_str[:200],
                            )
                            await _sleep(min(2 ** (soft_retries - 1), 4))
                            continue
                        if isinstance(e, (httpx.TransportError, httpx.TimeoutException)) and retryable and stage == "pre":
                            soft_retries += 1
                            await _sleep(min(2 ** (soft_retries - 1), 4))
                            continue
                        if in_flight and not gone_confirmed:
                            raise _ambiguous(
                                f"A write may have been stored (op {in_flight[-1].hash}); "
                                f"its outcome is not yet known: {err_str[:200]}"
                            ) from e
                        raise

                # ---------------- a send happened (or may have) --------------
                if local_hash is not None:
                    in_flight.append(_Attempt(local_hash, nonce, sent_block, not is_deployed))
                    pinned = nonce
                    gone_confirmed = False
                if (
                    kind == "single"
                    and confirm == "accept"
                    and not ambiguous_send
                    and local_hash is not None
                ):
                    _park(sender, in_flight, facts, on_lost, payloads=payloads,
                          kind=kind, resend_ok=True, resends=resends)
                    return local_hash

                try:
                    fate = await _await_fate(http, ctx, in_flight, deadline)
                except BaseException:
                    # Cancelled, interpreter shutdown, quota refusal on a status
                    # call...: the op is in the bundler and nobody has seen its
                    # fate. Track it so the NEXT write resolves it by hash.
                    if not gone_confirmed:
                        _park(sender, in_flight, facts, on_lost)
                    raise
                if fate.kind == "executed":
                    att_ex = next((a for a in in_flight if a.hash.lower() == (fate.hash or "").lower()), in_flight[-1])
                    _apply_executed(sender, att_ex.nonce, att_ex.carried_factory, fate.success)
                    if fate.success is False:
                        raise UserOpRevertedError(
                            f"UserOp {fate.hash} reverted on-chain (receipt.success=false)"
                        )
                    return fate.hash or att_ex.hash
                if fate.kind == "dead":
                    logger.warning(
                        "attempt(s) %s at nonce %d are dead (nonce used by another op); rebuilding at chain truth",
                        [a.hash for a in in_flight], pinned,
                    )
                    in_flight.clear()
                    pinned = None
                    gone_confirmed = False
                    continue
                if fate.kind == "gone":
                    gone_confirmed = True
                    if sends >= _MAX_NONCE_RETRIES:
                        logger.error("write dropped by the bundler after %d sends; nonce %d is unused", sends, pinned)
                        in_flight_hashes = [a.hash for a in in_flight]
                        raise UserOpDroppedError(
                            f"UserOp(s) {in_flight_hashes} were dropped by the bundler "
                            f"after {sends} sends (the account nonce did not advance)"
                        )
                    logger.warning(
                        "attempt(s) %s dropped by the bundler; re-sending at the pinned nonce %d",
                        [a.hash for a in in_flight], pinned,
                    )
                    continue
                # timeout: fate still unknown at the deadline
                if kind == "single":
                    _park(sender, in_flight, facts, on_lost, payloads=payloads,
                          kind=kind, resend_ok=True, resends=resends)
                    logger.warning(
                        "write %s not yet confirmed at the deadline; it stays tracked as pending",
                        in_flight[-1].hash,
                    )
                    return in_flight[-1].hash
                raise _ambiguous(
                    f"Batch UserOp {in_flight[-1].hash} was sent but is not confirmed "
                    f"within the deadline; it may still be stored. Do not re-send "
                    f"before its outcome is known."
                )

            # build attempts exhausted without a send decision
            if in_flight and not gone_confirmed:
                raise _ambiguous(
                    "A write may have been stored; its outcome is not yet known."
                )
            if last_exc is not None:
                raise last_exc
            raise WriteDeadlineError("The write could not be started; nothing was sent.")
    finally:
        lock.release()


async def build_and_send_userop(
    sender: str,
    eoa_address: str,
    eoa_private_key: bytes,
    protobuf_payload: bytes,
    relay_url: str,
    auth_key_hex: str,
    wallet_address: str,
    chain_id: int = 84532,
    client_id: str = "python-client",
    session_id: Optional[str] = None,
    data_edge_address: Optional[str] = None,
    *,
    confirm: str = "accept",
    profile: str = "background",
    deadline_s: Optional[float] = None,
    fact_ids: Optional[list[str]] = None,
    on_lost: Optional[Callable[[list[str]], Any]] = None,
) -> str:
    """Build, sign, and submit a UserOperation through the relay.

    Parameters
    ----------
    sender : str
        Smart Account (CREATE2) address.
    eoa_address : str
        EOA address that owns the Smart Account.
    eoa_private_key : bytes
        32-byte private key for the EOA.
    protobuf_payload : bytes
        Encrypted protobuf payload to store on-chain.
    relay_url : str
        TotalReclaw relay URL (e.g. https://api.totalreclaw.xyz).
    auth_key_hex : str
        HKDF auth key in hex for Bearer auth.
    wallet_address : str
        Smart Account address for X-Wallet-Address header.
    chain_id : int
        Target chain (84532 = Base Sepolia, 100 = Gnosis).
    client_id : str
        Client identifier for X-TotalReclaw-Client header.
    session_id : str, optional
        QA-scoped session tag forwarded as ``X-TotalReclaw-Session``.
    confirm : {"accept", "receipt"}
        ``"accept"`` (default) returns as soon as the bundler accepts the op
        and records it as pending; the NEXT write resolves it by hash before
        building (the predecessor gate). ``"receipt"`` waits for the op's own
        fate inside the call, up to the deadline.
    profile : {"interactive", "background"}
        Deadline profile (see :data:`PROFILES`).
    deadline_s : float, optional
        Total budget of this call from entry, lock wait included. Overrides
        the profile.
    fact_ids, on_lost
        Bookkeeping only. ``on_lost(fact_ids)`` is called when an accepted op
        is later proven replaced, dropped or reverted.

    Returns
    -------
    str
        The op hash (the locally computed v0.7 hash).

    Raises
    ------
    NothingSentError
        (:class:`PredecessorPendingError`, :class:`AccountBusyError`,
        :class:`WriteDeadlineError`): nothing was sent.
    UserOpAmbiguousError
        An op was sent and its fate is unknown: the write may be stored.
    """
    return await _submit_userop(
        kind="single", sender=sender, eoa_address=eoa_address,
        eoa_private_key=eoa_private_key, payloads=[protobuf_payload],
        relay_url=relay_url, auth_key_hex=auth_key_hex,
        wallet_address=wallet_address, chain_id=chain_id, client_id=client_id,
        session_id=session_id, data_edge_address=data_edge_address,
        confirm=confirm, profile=profile, deadline_s=deadline_s,
        fact_ids=fact_ids, on_lost=on_lost,
    )


async def build_and_send_userop_batch(
    sender: str,
    eoa_address: str,
    eoa_private_key: bytes,
    protobuf_payloads: list[bytes],
    relay_url: str,
    auth_key_hex: str,
    wallet_address: str,
    chain_id: int = 84532,
    client_id: str = "python-client",
    session_id: Optional[str] = None,
    data_edge_address: Optional[str] = None,
    *,
    profile: str = "background",
    deadline_s: Optional[float] = None,
    fact_ids: Optional[list[str]] = None,
    on_lost: Optional[Callable[[list[str]], Any]] = None,
) -> str:
    """Build, sign, and submit a BATCHED UserOperation through the relay.

    Wraps N protobuf payloads into one ``SimpleAccount.executeBatch(...)``
    call (one UserOp, one nonce). A batch always waits for its own fate
    (``confirm="receipt"``): it returns the op hash only once the op executed
    successfully.

    The ABI encoding comes from the shared Rust core
    (``totalreclaw_core.encode_batch_call``), byte-identical to the TS plugin.

    Raises
    ------
    ValueError
        If ``protobuf_payloads`` is empty or exceeds :data:`MAX_BATCH_SIZE`.
    UserOpRevertedError
        The op mined and reverted (not stored; a new op is safe).
    UserOpDroppedError
        The bundler dropped the op and its nonce is unused (not stored).
    UserOpAmbiguousError
        The op was sent and its fate is unknown at the deadline: the batch
        MAY be stored. Do not re-send before :func:`resolve_fate` says so.
    NothingSentError
        Nothing was sent (:class:`PredecessorPendingError`,
        :class:`AccountBusyError`, :class:`WriteDeadlineError`).
    RuntimeError
        Paymaster or bundler rejection.
    """
    if not protobuf_payloads:
        raise ValueError("Batch must contain at least 1 payload")
    if len(protobuf_payloads) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size {len(protobuf_payloads)} exceeds maximum of "
            f"{MAX_BATCH_SIZE}"
        )
    return await _submit_userop(
        kind="batch", sender=sender, eoa_address=eoa_address,
        eoa_private_key=eoa_private_key, payloads=list(protobuf_payloads),
        relay_url=relay_url, auth_key_hex=auth_key_hex,
        wallet_address=wallet_address, chain_id=chain_id, client_id=client_id,
        session_id=session_id, data_edge_address=data_edge_address,
        confirm="receipt", profile=profile, deadline_s=deadline_s,
        fact_ids=fact_ids, on_lost=on_lost,
    )



# ---------------------------------------------------------------------------
# Session-key signing (cred-8 — spec §4.3)
# ---------------------------------------------------------------------------


def sign_userop_with_session_key(
    user_op: dict,
    session_priv_key: bytes,
    entry_point: str,
    chain_id: int,
    include_grant: bool = False,
    grant: Optional[SessionKeyPermissionGrant] = None,
) -> bytes:
    """Build the v0.7 UserOp signature for a session-key-only signer.

    Two output shapes, selected by ``include_grant``:

    - ``include_grant=False`` (steady state): raw 65-byte ECDSA over the
      EntryPoint UserOp hash. The session signer must already be installed
      in the account (``TotalReclawAccount``, lazy-installed by an earlier UserOp).
    - ``include_grant=True`` (first UserOp post-pair): ``abi.encode(
      PermissionGrant, ecdsaSig)`` matching ``TotalReclawAccount._decodeInstallSig``.
      The module installs the entry on first encounter and validates the
      current call's scope.

    Unlike :func:`sign_user_op_hash` (which applies the EIP-191
    ``\\x19Ethereum Signed Message:\\n32`` prefix used by SimpleAccount's
    legacy validator), this function signs the userOpHash raw — matching
    ``TotalReclawAccount._recoverEcdsa(userOpHash, sig)`` exactly.

    Parameters
    ----------
    user_op : dict
        Fully-built UserOp (sender, nonce, callData, gas limits, paymaster
        fields). The ``signature`` field is ignored — pass any placeholder.
    session_priv_key : bytes
        32-byte secp256k1 private key of the session signer.
    entry_point : str
        ERC-4337 EntryPoint address used for the hash computation
        (typically :data:`ENTRYPOINT_V07`).
    chain_id : int
        EVM chain id used for hash computation. MUST match the chain the
        UserOp targets and (if ``include_grant=True``) ``grant.chain_id``.
    include_grant : bool
        Whether to wrap the ECDSA sig with the install-grant payload.
    grant : SessionKeyPermissionGrant, optional
        Required when ``include_grant=True``. MUST already carry a
        ``master_signature`` (call :meth:`SessionKeyPermissionGrant.sign`
        first). The module rejects mismatched ``signer`` / ``account`` /
        ``chain_id`` / ``verifyingContract`` — caller is responsible for
        consistency.

    Returns
    -------
    bytes
        Raw signature bytes ready to drop into ``user_op["signature"]``
        (hex-encode with ``"0x" + sig.hex()`` if assigning to JSON).
    """
    if len(session_priv_key) != 32:
        raise ValueError(
            f"session_priv_key must be 32 bytes; got {len(session_priv_key)}"
        )

    user_op_hash = compute_user_op_hash(user_op, entry_point, chain_id)
    ecdsa_sig = sign_digest(user_op_hash, session_priv_key)

    if not include_grant:
        return ecdsa_sig

    if grant is None:
        raise ValueError("grant is required when include_grant=True")
    if grant.chain_id != chain_id:
        raise ValueError(
            f"grant.chain_id ({grant.chain_id}) != userOp chain_id ({chain_id})"
        )
    if len(grant.master_signature) != 65:
        raise ValueError(
            "grant.master_signature must be set before include_grant=True"
        )
    return encode_install_signature(grant, ecdsa_sig)


# ---------------------------------------------------------------------------
# Subgraph helper — confirm the lazy install landed before dropping the grant
# ---------------------------------------------------------------------------


# Standard TheGraph entity-collection name for ``TotalReclawAccount``'s
# ``SessionKeyInstalled(address indexed signer, uint256 nonce)`` event (the
# emitting contract is the account, so the subgraph derives ``account`` from
# the event address). The subgraph indexing of this event is open per
# spec §11.Q4 — when the subgraph ships, this name is the contract.
_SESSION_KEY_INSTALLED_QUERY: str = (
    "query SessionKeyInstalled($account: Bytes!, $signer: Bytes!) {\n"
    "  sessionKeyInstalleds(\n"
    "    where: { account: $account, signer: $signer }\n"
    "    first: 1\n"
    "  ) {\n"
    "    id\n"
    "    nonce\n"
    "  }\n"
    "}"
)


# Chain id → subgraph routing key used by relay's ``query_subgraph(chain=...)``.
_SUBGRAPH_CHAIN_KEY: dict[int, str] = {
    84532: "base-sepolia",
    100: "gnosis",
}


async def session_key_grant_was_installed(
    smart_account: str,
    signer: str,
    chain_id: int,
    relay_client: "RelayClient",
) -> bool:
    """Return True iff the Smart Account (``TotalReclawAccount``) has emitted
    ``SessionKeyInstalled(smart_account, signer, *)`` on ``chain_id``.

    Callers use this to switch ``sign_userop_with_session_key`` from
    ``include_grant=True`` (lazy-install path, ~5K extra gas) to
    ``include_grant=False`` (steady state) after the first write confirms.

    Best-effort: if the relay's subgraph proxy is unreachable, the schema
    isn't bumped yet (spec §11.Q4), or the query errors, returns False so
    the caller keeps sending the install payload. The on-chain module is
    idempotent on the install path — re-sending a grant the module has
    already accepted just re-runs validation cheaply.

    Parameters
    ----------
    smart_account : str
        Smart Account (CREATE2) address.
    signer : str
        Session signer EOA address.
    chain_id : int
        Chain id (84532 = Base Sepolia, 100 = Gnosis).
    relay_client : RelayClient
        Active relay client used for the ``/v1/subgraph`` proxy call.
    """
    chain_key = _SUBGRAPH_CHAIN_KEY.get(chain_id)
    if chain_key is None:
        logger.debug(
            "session_key_grant_was_installed: no subgraph mapping for chain %d",
            chain_id,
        )
        return False
    variables = {
        "account": smart_account.lower(),
        "signer": signer.lower(),
    }
    try:
        result = await relay_client.query_subgraph(
            _SESSION_KEY_INSTALLED_QUERY, variables, chain=chain_key
        )
    except Exception as e:
        logger.debug(
            "session_key_grant_was_installed: subgraph query failed (%s); "
            "treating as not-installed",
            e,
        )
        return False
    data = result.get("data") if isinstance(result, dict) else None
    if not data:
        return False
    entries = data.get("sessionKeyInstalleds") or []
    return len(entries) > 0
