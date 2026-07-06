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
import json
import logging
from typing import TYPE_CHECKING, Optional

import httpx
from eth_hash.auto import keccak

import totalreclaw_core

from .grant import (
    SessionKeyPermissionGrant,
    encode_install_signature,
    sign_digest,
)
from .relay import _client_header_value

if TYPE_CHECKING:
    from .relay import RelayClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Well-known addresses
# ---------------------------------------------------------------------------

ENTRYPOINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
SIMPLE_ACCOUNT_FACTORY = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985"

# ---------------------------------------------------------------------------
# Per-account submission mutex — 3.3.1-rc.3 AA25 serialization
# ---------------------------------------------------------------------------
#
# Concurrent UserOp submissions for the SAME Smart Account used to race
# at the nonce fetch:
#   - Call A: get_nonce()=5, build UserOp, submit, await receipt.
#   - Call B: get_nonce()=5 (A not mined yet), build UserOp, submit → AA25.
#
# The fix: serialize per-``sender`` with an ``asyncio.Lock`` so call B
# does not even START its nonce fetch until call A has settled. Existing
# AA25 retry with fresh nonce continues to catch relay-side zombie
# UserOps. Per-sender dict lives in module scope; keys are lowercased
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


# ---------------------------------------------------------------------------
# Local monotonic nonce cache — F3 / internal #423
# ---------------------------------------------------------------------------
#
# The EntryPoint nonce only advances at ON-CHAIN EXECUTION, but this client
# returns as soon as the bundler accepts an op into its mempool. Sequential
# submissions (import batches fire back-to-back) therefore re-fetched the
# SAME nonce while the previous op was unmined -> AA25 -> blind 1+2+4s retry
# lost the race against ~5s Gnosis blocks -> whole batches dropped (rc12 QA:
# 15/44 facts stored = only the first batch landed).
#
# Fix: remember the next expected nonce per sender and submit with
# max(chain_nonce, cached). Bundlers accept queued sequential-nonce ops from
# one sender, so pipelining is preserved. The cache is process-local and
# self-healing: max() with the chain nonce means external activity or a
# restart can only correct it upward from reality; an AA25 resets it.
# Reads/writes happen under the per-sender submission lock.
_sender_next_nonce: dict[str, int] = {}


def _resolve_submission_nonce(sender: str, chain_nonce: int) -> int:
    """Nonce to submit with: the chain nonce or our pipelined next, whichever is higher."""
    return max(chain_nonce, _sender_next_nonce.get(sender.lower(), 0))


def _record_submitted_nonce(sender: str, nonce: int) -> None:
    """Record a bundler-accepted submission; never decreases."""
    key = sender.lower()
    _sender_next_nonce[key] = max(_sender_next_nonce.get(key, 0), nonce + 1)


def _reset_sender_nonce(sender: str) -> None:
    """Drop the cached nonce (AA25 path) — fall back to chain truth."""
    _sender_next_nonce.pop(sender.lower(), None)


def _reset_nonce_cache_for_tests() -> None:
    """Clear the nonce cache. Test-only helper."""
    _sender_next_nonce.clear()


# ---------------------------------------------------------------------------
# Deployed-account cache — F3 / internal #435
# ---------------------------------------------------------------------------
#
# A Smart Account deploys exactly once (its first UserOp carries the factory /
# initCode; every op after that must NOT). The old code re-derived "is
# deployed" every submission from ``eth_getCode`` — but ``_eth_get_code``
# silently read any RPC error as ``"0x"`` (= not deployed). Under import bursts
# the public Gnosis RPC rate-limits, so a deployed account read as undeployed,
# the factory was re-attached, and the paymaster reverted the whole batch with
# ``-32500 "Sender does not implement validateUserOp or factory is not
# deployed"`` — dropping every fact in it (rc13 QA: 52/78 lost).
#
# Fix: a monotonic per-sender "deployed" latch. Once we have ANY proof the
# account executed on-chain (code present, chain_nonce > 0, a confirmed send,
# or a sponsor-stage sender-state error), we never attach the factory again.
# Keys are lowercased addresses; reads/writes happen under the per-sender
# submission lock. Mirrors the OpenClaw plugin's PR #407 deploy-state cache.
_sender_deployed: set[str] = set()


def _mark_sender_deployed(sender: str) -> None:
    """Latch a sender as deployed (monotonic — never cleared in-process)."""
    _sender_deployed.add(sender.lower())


def _is_sender_deployed_cached(sender: str) -> bool:
    return sender.lower() in _sender_deployed


def _reset_deployed_cache_for_tests() -> None:
    """Clear the deployed cache. Test-only helper."""
    _sender_deployed.clear()


def _is_sender_state_error(err_str: str) -> bool:
    """True when a sponsor/bundler error indicates the account is ALREADY
    deployed (so re-attaching the factory is the actual fault).

    Matches ONLY the specific "account already exists" signals:
      - the paymaster revert \"Sender does not implement validateUserOp or
        factory is not deployed\" (the #435 root-cause message), and
      - the EntryPoint ``AA10 sender already constructed`` code / phrasing.

    Deliberately NOT keyed on the bare JSON-RPC code ``-32500``: many
    UNRELATED failures share it (AA13 initCode-failed, AA21 prefund,
    AA23 sig, AA31/AA33 paymaster, transient sim errors). Latching those as
    "deployed" would strip the factory from a FRESH account's first op —
    whose deploy DEPENDS on the factory — and permanently brick it in-process
    (the cache is never cleared). AA13 in particular must retry WITH the
    factory; see the transient-``-32500`` retry branch in the submit loops.
    Checked only AFTER an ``AA25`` nonce-conflict has been ruled out."""
    if not err_str:
        return False
    return (
        "already constructed" in err_str          # AA10 / raw phrasing
        or "AA10" in err_str
        or "validateUserOp or factory" in err_str  # paymaster -32500 revert
    )


def _is_transient_sim_error(err_str: str) -> bool:
    """True for a ``-32500`` simulation error that is NOT a redeploy signal.

    A ``-32500`` that :func:`_is_sender_state_error` did not claim (AA13
    initCode-failed, AA21 prefund, transient sim reverts, …) is treated as
    transient: rebuild and retry with the deploy state UNCHANGED — the
    account is NOT latched deployed and a fresh account keeps its factory.
    Callers must have already ruled out AA25 and the sender-state case."""
    return bool(err_str) and "-32500" in err_str


async def _resolve_is_deployed(
    http: httpx.AsyncClient, rpc_url: str, sender: str, chain_nonce: int
) -> bool:
    """Decide whether ``sender`` is already deployed for THIS submission.

    Resolution order (cheapest, most-authoritative first):
      1. Deployed cache — once True, always True.
      2. ``chain_nonce > 0`` — the account has executed ops ⇒ deployed.
         Skips ``eth_getCode`` entirely (the burst-rate-limited call that
         caused #435).
      3. ``eth_getCode`` — non-empty bytecode ⇒ deployed. A hardened
         ``_eth_get_code`` raises (rather than reading "0x") on an RPC
         error, so a transient failure propagates into the retry loop
         instead of being silently misread as undeployed.
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


# ---------------------------------------------------------------------------
# Batch receipt confirmation — F3 / internal #431
# ---------------------------------------------------------------------------
#
# ``eth_sendUserOperation`` returns as soon as the bundler ACCEPTS the op into
# its mempool — not when it mines. rc13 QA saw ``facts_stored=26`` while only
# ~11 landed on-chain, because the batch path returned the hash at accept time
# and a later on-chain revert (e.g. the #435 factory bug) silently dropped the
# facts. For the BATCH path only, we now poll ``eth_getUserOperationReceipt``
# and return the hash ONLY once a receipt reports success — so "stored" means
# on-chain. The single-fact path keeps accept-time return (auto-extraction
# latency matters more there and a single dropped fact re-extracts next turn).
#
# rc4 (internal#435): the instrumented staging repro showed >60s inclusion
# latency for larger (sim-passing) ops on the staging bundler — a 60s wait
# false-negatived batches that DID eventually mine. Lifted to 240s / 5s poll.
_BATCH_RECEIPT_TIMEOUT_S: float = 240.0
_BATCH_RECEIPT_POLL_S: float = 5.0


def _receipt_success(result: dict) -> Optional[bool]:
    """Interpret a UserOperation receipt's ``success`` field.

    Returns True/False when the receipt states success, or None when the
    receipt is absent / doesn't carry a decidable success flag (keep
    polling). Tolerates both boolean and string ("true"/"false") shapes.
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


async def _await_batch_receipt(
    http: httpx.AsyncClient,
    relay_url: str,
    headers: dict,
    user_op_hash: str,
    timeout_s: Optional[float] = None,
    poll_s: Optional[float] = None,
) -> None:
    """Poll ``eth_getUserOperationReceipt`` until success, or raise.

    Raises :class:`RuntimeError` on an explicit ``success=false`` receipt or
    on timeout — the import engine treats either as a FAILED batch (not
    stored) so ``facts_stored`` reflects on-chain reality.
    """
    import time as _time
    timeout = _BATCH_RECEIPT_TIMEOUT_S if timeout_s is None else timeout_s
    poll = _BATCH_RECEIPT_POLL_S if poll_s is None else poll_s
    deadline = _time.monotonic() + timeout
    while True:
        try:
            resp = await _relay_rpc(
                http, relay_url, headers,
                "eth_getUserOperationReceipt", [user_op_hash], rpc_id=4,
            )
        except Exception as e:
            logger.debug("receipt poll for %s failed (%s); retrying", user_op_hash, e)
            resp = {}
        result = resp.get("result") if isinstance(resp, dict) else None
        verdict = _receipt_success(result)
        if verdict is True:
            return
        if verdict is False:
            raise RuntimeError(
                f"Batch UserOp {user_op_hash} reverted on-chain "
                f"(receipt.success=false)"
            )
        if _time.monotonic() >= deadline:
            raise RuntimeError(
                f"Batch UserOp {user_op_hash} not confirmed within "
                f"{timeout:.0f}s (no successful receipt)"
            )
        await asyncio.sleep(poll)


async def _await_nonce_advance(
    http,
    sender: str,
    chain_id: int,
    min_nonce: int,
    timeout_s: float = 15.0,
    poll_s: float = 1.5,
) -> int:
    """Poll the EntryPoint nonce until it reaches ``min_nonce`` or timeout.

    Used by the AA25 retry path: instead of sleeping blind while the
    conflicting op mines, wait for the observable signal (nonce advance).
    RPC errors are tolerated (retry on next poll). Returns the last nonce
    seen (may be < min_nonce on timeout).
    """
    import time as _time
    deadline = _time.monotonic() + timeout_s
    last = -1
    while _time.monotonic() < deadline:
        try:
            last = await get_nonce(http, sender, chain_id)
            if last >= min_nonce:
                return last
        except Exception:
            pass
        await asyncio.sleep(poll_s)
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
    http: httpx.AsyncClient, rpc_url: str, to: str, data: str
) -> str:
    """Execute an ``eth_call`` and return the hex result."""
    resp = await http.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
            "id": 1,
        },
    )
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"eth_call failed: {body['error']}")
    return body.get("result", "0x0")


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
    http: httpx.AsyncClient, sender: str, chain_id: int
) -> int:
    """Get the next nonce for a Smart Account from the EntryPoint.

    Uses ``eth_call`` to ``EntryPoint.getNonce(sender, 0)`` via a public RPC.
    """
    calldata = f"0x{GET_NONCE_SELECTOR}{_pad32(sender)}{_encode_uint256(0)}"
    rpc_url = _rpc_url_for_chain(chain_id)
    result = await _eth_call(http, rpc_url, ENTRYPOINT_V07, calldata)
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
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
        QA-scoped session tag forwarded as ``X-TotalReclaw-Session`` for
        Axiom log tracing. Typically populated from
        ``TOTALRECLAW_SESSION_ID`` at client construction time; see
        :class:`totalreclaw.relay.RelayClient`.

    Returns
    -------
    str
        The userOpHash returned by the bundler.
    """
    relay_url = relay_url.rstrip("/")
    rpc_url = _rpc_url_for_chain(chain_id)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_key_hex}",
        "X-TotalReclaw-Client": _client_header_value(client_id),
        "X-Wallet-Address": wallet_address,
    }
    if session_id:
        headers["X-TotalReclaw-Session"] = session_id

    _MAX_NONCE_RETRIES = 3

    # rc.3 — serialize concurrent submissions for the same Smart Account.
    sender_lock = await _get_sender_lock(sender)

    async with sender_lock, httpx.AsyncClient(timeout=30.0) as http:
        # 1. Encode the execute calldata (idempotent, does not depend on nonce)
        #    SmartAccount.execute(dataEdgeAddress, 0, protobufPayload)
        #    DataEdge target is relay-authoritative (#366) when supplied.
        call_data = encode_execute_calldata_for_data_edge(
            protobuf_payload, data_edge_address
        )

        # Retry loop for AA25 nonce conflicts.  When two UserOps race for
        # the same nonce the bundler rejects one with AA25.  Re-fetching
        # the nonce and rebuilding the UserOp resolves it.
        for attempt in range(_MAX_NONCE_RETRIES):
            try:
                # 2. Get nonce from EntryPoint, pipelined past any of our own
                #    unmined submissions via the local cache (#423).
                chain_nonce = await get_nonce(http, sender, chain_id)
                nonce = _resolve_submission_nonce(sender, chain_nonce)
                logger.debug("Nonce for %s: chain=%d using=%d", sender, chain_nonce, nonce)

                # 3. Deployed check (#435): the deployed cache and
                #    chain_nonce > 0 short-circuit eth_getCode; a getCode RPC
                #    error now raises instead of silently reading as
                #    undeployed (which re-attached the factory and reverted
                #    the batch).
                is_deployed = await _resolve_is_deployed(
                    http, rpc_url, sender, chain_nonce
                )

                # 4. Build partial UserOp with stub signature for gas estimation.
                #    This specific dummy signature passes ecrecover without
                #    reverting (matches permissionless SDK's
                #    SimpleAccount.getStubSignature).
                _STUB_SIGNATURE = (
                    "0x"
                    "fffffffffffffffffffffffffffffff000000000000000000000000000000000"
                    "7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    "1c"
                )
                user_op: dict = {
                    "sender": sender,
                    "nonce": hex(nonce),
                    "callData": call_data,
                    "signature": _STUB_SIGNATURE,
                }

                if not is_deployed:
                    user_op["factory"] = SIMPLE_ACCOUNT_FACTORY
                    user_op["factoryData"] = encode_factory_data(eoa_address)
                    logger.debug(
                        "Account not deployed; including factory data"
                    )

                # 5. Get gas prices from Pimlico
                gas_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "pimlico_getUserOperationGasPrice",
                    [],
                    rpc_id=1,
                )
                if "result" in gas_data and gas_data["result"].get("fast"):
                    fast = gas_data["result"]["fast"]
                    user_op["maxFeePerGas"] = fast["maxFeePerGas"]
                    user_op["maxPriorityFeePerGas"] = fast[
                        "maxPriorityFeePerGas"
                    ]
                else:
                    # Fallback gas prices
                    user_op["maxFeePerGas"] = hex(2_000_000_000)
                    user_op["maxPriorityFeePerGas"] = hex(1_500_000_000)

                # 6. Sponsor the UserOperation via Pimlico's
                #    pm_sponsorUserOperation.  This single call returns all
                #    gas limits AND paymaster fields.
                sponsor_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "pm_sponsorUserOperation",
                    [user_op, ENTRYPOINT_V07],
                    rpc_id=2,
                )
                if "result" in sponsor_data:
                    sp = sponsor_data["result"]
                    user_op["callGasLimit"] = sp["callGasLimit"]
                    user_op["verificationGasLimit"] = sp[
                        "verificationGasLimit"
                    ]
                    user_op["preVerificationGas"] = sp["preVerificationGas"]
                    user_op["paymaster"] = sp["paymaster"]
                    user_op["paymasterData"] = sp["paymasterData"]
                    user_op["paymasterVerificationGasLimit"] = sp[
                        "paymasterVerificationGasLimit"
                    ]
                    user_op["paymasterPostOpGasLimit"] = sp[
                        "paymasterPostOpGasLimit"
                    ]
                elif "error" in sponsor_data:
                    raise RuntimeError(
                        f"Paymaster sponsorship failed: "
                        f"{sponsor_data['error']}"
                    )
                else:
                    raise RuntimeError(
                        "pm_sponsorUserOperation returned no result or error"
                    )

                # 7. Compute UserOp hash and sign with EOA key.
                #    Delegates to Rust core for both hashing and signing.
                #    The Rust sign_userop applies EIP-191 Ethereum message
                #    prefix internally.
                user_op_hash = compute_user_op_hash(
                    user_op, ENTRYPOINT_V07, chain_id
                )
                user_op["signature"] = sign_user_op_hash(
                    user_op_hash, eoa_private_key
                )

                # 8. Submit the signed UserOp to the bundler
                send_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "eth_sendUserOperation",
                    [user_op, ENTRYPOINT_V07],
                    rpc_id=3,
                )

                if "error" in send_data:
                    raise RuntimeError(
                        f"UserOp submission failed: {send_data['error']}"
                    )

                _record_submitted_nonce(sender, nonce)
                _mark_sender_deployed(sender)  # #435: a confirmed send ⇒ deployed
                return send_data.get("result", "")

            except Exception as e:
                err_str = str(e)
                retryable = attempt < _MAX_NONCE_RETRIES - 1
                if "AA25" in err_str and retryable:
                    # #423: nonce conflict — drop the pipelined nonce and wait
                    # for the conflicting op to mine (observable as a nonce
                    # advance) instead of sleeping blind against ~5s blocks.
                    logger.warning(
                        "AA25 nonce conflict (attempt %d/%d), retrying...",
                        attempt + 1, _MAX_NONCE_RETRIES,
                    )
                    _reset_sender_nonce(sender)
                    await _await_nonce_advance(
                        http, sender, chain_id, min_nonce=nonce + 1,
                        timeout_s=15.0 if attempt else 6.0,
                    )
                    continue
                if _is_sender_state_error(err_str) and retryable:
                    # #435: the account is already deployed — re-attaching the
                    # factory is the fault. Latch deployed and retry WITHOUT
                    # the factory (next iteration reads the cache). No
                    # nonce-advance wait — this is not a nonce conflict.
                    logger.warning(
                        "sender-state/redeploy error (attempt %d/%d) — marking "
                        "deployed and retrying without factory: %s",
                        attempt + 1, _MAX_NONCE_RETRIES, err_str[:200],
                    )
                    _mark_sender_deployed(sender)
                    continue
                if _is_transient_sim_error(err_str) and retryable:
                    # #435 review: a -32500 that is NOT a redeploy signal
                    # (AA13 initCode-failed, AA21 prefund, transient sim) —
                    # do NOT latch deployed or strip the factory. Rebuild and
                    # retry with the deploy state unchanged (a fresh account
                    # keeps its factory, which its deploy depends on).
                    logger.warning(
                        "transient -32500 sim error (attempt %d/%d) — retrying "
                        "with unchanged deploy state: %s",
                        attempt + 1, _MAX_NONCE_RETRIES, err_str[:200],
                    )
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                raise


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
) -> str:
    """Build, sign, and submit a BATCHED UserOperation through the relay.

    Mirrors :func:`build_and_send_userop` but wraps N protobuf payloads
    into a single ``SimpleAccount.executeBatch(...)`` call rather than
    emitting N sequential UserOps. Each element of ``protobuf_payloads``
    becomes one call to the DataEdge contract's ``fallback()`` and emits
    an independent ``Log(bytes)`` event that the subgraph indexes
    separately.

    **Why batch?**

    1. *UX*: a full 15-fact extraction cycle drops from ~60s to ~8s
       because all the per-UserOp RPC overhead (nonce fetch, gas price,
       sponsor call, bundler submit, inclusion wait) is paid once.
    2. *Gas*: the ~21k base-tx cost is amortized across all N facts.
    3. *Paymaster budget*: one UserOp counted, not N.
    4. *Nonce safety*: collapses O(n) AA25 retry potential into O(1).

    **Byte-parity with TS**: the underlying ABI encoding comes from the
    shared Rust core (``totalreclaw_core.encode_batch_call``). The TS
    plugin's ``encodeBatchCalls`` compiles to the same executeBatch
    contract call, so a Python-encoded batch UserOp is byte-identical
    to the TS equivalent for the same inputs. Verified in
    ``tests/test_userop_batch.py::test_batch_calldata_fixture_parity``.

    Parameters
    ----------
    sender : str
        Smart Account (CREATE2) address. Same for every call in the batch.
    eoa_address : str
        EOA address that owns the Smart Account.
    eoa_private_key : bytes
        32-byte private key for the EOA.
    protobuf_payloads : list[bytes]
        1..15 raw encrypted protobuf payloads. Each becomes one
        ``fallback()`` invocation on the DataEdge contract. A batch of
        1 is byte-identical to :func:`build_and_send_userop` (the Rust
        core folds it down to ``execute`` rather than ``executeBatch``).
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

    Returns
    -------
    str
        The userOpHash returned by the bundler for the single batched
        UserOperation. Callers who need per-fact subgraph IDs should use
        the ``store_fact_batch`` wrapper, which generates UUIDs upstream
        of this call.

    Raises
    ------
    ValueError
        If ``protobuf_payloads`` is empty or exceeds
        :data:`MAX_BATCH_SIZE`.
    RuntimeError
        Paymaster or bundler rejection (propagated from
        ``pm_sponsorUserOperation`` / ``eth_sendUserOperation``). AA25 /
        AA10 nonce-or-sender conflicts are retried up to 3 times before
        surfacing here; other bundler errors are not retried.
    """
    if not protobuf_payloads:
        raise ValueError("Batch must contain at least 1 payload")
    if len(protobuf_payloads) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size {len(protobuf_payloads)} exceeds maximum of "
            f"{MAX_BATCH_SIZE}"
        )

    relay_url = relay_url.rstrip("/")
    rpc_url = _rpc_url_for_chain(chain_id)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_key_hex}",
        "X-TotalReclaw-Client": _client_header_value(client_id),
        "X-Wallet-Address": wallet_address,
    }
    if session_id:
        headers["X-TotalReclaw-Session"] = session_id

    _MAX_NONCE_RETRIES = 3

    # rc.3 — serialize concurrent batch submissions for the same Smart
    # Account. Same lock shared with ``build_and_send_userop`` so a
    # single-fact + batch concurrent pair also serialize correctly.
    sender_lock = await _get_sender_lock(sender)

    async with sender_lock, httpx.AsyncClient(timeout=30.0) as http:
        # 1. Encode the executeBatch calldata once. Independent of nonce,
        #    so we compute it before the retry loop. Delegates to the
        #    Rust core for byte-parity with the TS plugin.
        call_data = encode_execute_batch_calldata_for_data_edge(
            protobuf_payloads, data_edge_address
        )

        # Retry loop for AA25 nonce conflicts.  When two UserOps race for
        # the same nonce the bundler rejects one with AA25.  Re-fetching
        # the nonce and rebuilding the UserOp resolves it. Collapses to
        # O(1) retries for the batch path vs O(n) for sequential sends.
        for attempt in range(_MAX_NONCE_RETRIES):
            try:
                # 2. Get nonce from EntryPoint, pipelined past our own unmined
                #    submissions via the local cache (#423).
                chain_nonce = await get_nonce(http, sender, chain_id)
                nonce = _resolve_submission_nonce(sender, chain_nonce)
                logger.debug(
                    "Batch nonce for %s: chain=%d using=%d (batch size %d)",
                    sender, chain_nonce, nonce, len(protobuf_payloads),
                )

                # 3. Deployed check (#435): the deployed cache and
                #    chain_nonce > 0 short-circuit eth_getCode; a getCode RPC
                #    error now raises instead of silently reading as
                #    undeployed (which re-attached the factory and reverted
                #    the batch).
                is_deployed = await _resolve_is_deployed(
                    http, rpc_url, sender, chain_nonce
                )

                # 4. Build partial UserOp with stub signature for gas estimation.
                _STUB_SIGNATURE = (
                    "0x"
                    "fffffffffffffffffffffffffffffff000000000000000000000000000000000"
                    "7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    "1c"
                )
                user_op: dict = {
                    "sender": sender,
                    "nonce": hex(nonce),
                    "callData": call_data,
                    "signature": _STUB_SIGNATURE,
                }

                if not is_deployed:
                    user_op["factory"] = SIMPLE_ACCOUNT_FACTORY
                    user_op["factoryData"] = encode_factory_data(eoa_address)
                    logger.debug(
                        "Batch: account not deployed; including factory data"
                    )

                # 5. Get gas prices from Pimlico
                gas_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "pimlico_getUserOperationGasPrice",
                    [],
                    rpc_id=1,
                )
                if "result" in gas_data and gas_data["result"].get("fast"):
                    fast = gas_data["result"]["fast"]
                    user_op["maxFeePerGas"] = fast["maxFeePerGas"]
                    user_op["maxPriorityFeePerGas"] = fast[
                        "maxPriorityFeePerGas"
                    ]
                else:
                    # Fallback gas prices — slightly higher than the
                    # single-fact fallback because executeBatch gas
                    # scales with batch size.
                    user_op["maxFeePerGas"] = hex(2_000_000_000)
                    user_op["maxPriorityFeePerGas"] = hex(1_500_000_000)

                # 6. Sponsor the UserOperation via Pimlico's
                #    pm_sponsorUserOperation. Sponsorship is per-UserOp
                #    (not per-call), so the paymaster counts this as 1
                #    op regardless of ``len(protobuf_payloads)``.
                sponsor_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "pm_sponsorUserOperation",
                    [user_op, ENTRYPOINT_V07],
                    rpc_id=2,
                )
                if "result" in sponsor_data:
                    sp = sponsor_data["result"]
                    user_op["callGasLimit"] = sp["callGasLimit"]
                    user_op["verificationGasLimit"] = sp[
                        "verificationGasLimit"
                    ]
                    user_op["preVerificationGas"] = sp["preVerificationGas"]
                    user_op["paymaster"] = sp["paymaster"]
                    user_op["paymasterData"] = sp["paymasterData"]
                    user_op["paymasterVerificationGasLimit"] = sp[
                        "paymasterVerificationGasLimit"
                    ]
                    user_op["paymasterPostOpGasLimit"] = sp[
                        "paymasterPostOpGasLimit"
                    ]
                elif "error" in sponsor_data:
                    raise RuntimeError(
                        f"Batch paymaster sponsorship failed: "
                        f"{sponsor_data['error']}"
                    )
                else:
                    raise RuntimeError(
                        "pm_sponsorUserOperation returned no result or error"
                    )

                # 7. Compute UserOp hash and sign with EOA key.
                #    Same hash shape as the single-fact path — only
                #    callData differs. EntryPoint doesn't care whether
                #    the inner SimpleAccount call is ``execute`` or
                #    ``executeBatch``; both hash identically.
                user_op_hash = compute_user_op_hash(
                    user_op, ENTRYPOINT_V07, chain_id
                )
                user_op["signature"] = sign_user_op_hash(
                    user_op_hash, eoa_private_key
                )

                # 8. Submit the signed batched UserOp to the bundler.
                send_data = await _relay_rpc(
                    http,
                    relay_url,
                    headers,
                    "eth_sendUserOperation",
                    [user_op, ENTRYPOINT_V07],
                    rpc_id=3,
                )

                if "error" in send_data:
                    raise RuntimeError(
                        f"Batch UserOp submission failed: {send_data['error']}"
                    )

                # Record the accept-time nonce/deploy state, then CONFIRM the
                # op mined before returning (#431). The nonce advance is
                # recorded at accept time so a concurrent submission pipelines
                # correctly; the receipt gate ensures ``facts_stored`` only
                # counts on-chain-confirmed batches.
                _record_submitted_nonce(sender, nonce)
                _mark_sender_deployed(sender)  # #435: a confirmed send ⇒ deployed
                batch_hash = send_data.get("result", "")
                await _await_batch_receipt(http, relay_url, headers, batch_hash)
                return batch_hash

            except Exception as e:
                err_str = str(e)
                retryable = attempt < _MAX_NONCE_RETRIES - 1
                if "AA25" in err_str and retryable:
                    # #423: nonce conflict — drop the pipelined nonce and wait
                    # for the conflicting op to mine before rebuilding.
                    logger.warning(
                        "Batch AA25 nonce conflict (attempt %d/%d, batch size "
                        "%d), retrying...",
                        attempt + 1, _MAX_NONCE_RETRIES, len(protobuf_payloads),
                    )
                    _reset_sender_nonce(sender)
                    await _await_nonce_advance(
                        http, sender, chain_id, min_nonce=nonce + 1,
                        timeout_s=15.0 if attempt else 6.0,
                    )
                    continue
                if _is_sender_state_error(err_str) and retryable:
                    # #435: account already deployed — latch deployed and
                    # retry WITHOUT the factory (the sponsor -32500 root
                    # cause). No nonce-advance wait; this is not a conflict.
                    logger.warning(
                        "Batch sender-state/redeploy error (attempt %d/%d, "
                        "batch size %d) — marking deployed and retrying "
                        "without factory: %s",
                        attempt + 1, _MAX_NONCE_RETRIES,
                        len(protobuf_payloads), err_str[:200],
                    )
                    _mark_sender_deployed(sender)
                    continue
                if _is_transient_sim_error(err_str) and retryable:
                    # #435 review: a non-redeploy -32500 (AA13 initCode-failed,
                    # AA21 prefund, transient sim) — do NOT latch deployed or
                    # strip the factory. Rebuild and retry with deploy state
                    # unchanged so a fresh account keeps its (required) factory.
                    logger.warning(
                        "Batch transient -32500 sim error (attempt %d/%d, "
                        "batch size %d) — retrying with unchanged deploy "
                        "state: %s",
                        attempt + 1, _MAX_NONCE_RETRIES,
                        len(protobuf_payloads), err_str[:200],
                    )
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                raise


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
      in the SessionKeyModule (lazy-installed by an earlier UserOp).
    - ``include_grant=True`` (first UserOp post-pair): ``abi.encode(
      PermissionGrant, ecdsaSig)`` matching ``SessionKeyModule._decodeInstallSig``.
      The module installs the entry on first encounter and validates the
      current call's scope.

    Unlike :func:`sign_user_op_hash` (which applies the EIP-191
    ``\\x19Ethereum Signed Message:\\n32`` prefix used by SimpleAccount's
    legacy validator), this function signs the userOpHash raw — matching
    ``SessionKeyModule._recoverEcdsa(userOpHash, sig)`` exactly.

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


# Standard TheGraph entity-collection name for the SessionKeyModule's
# ``SessionKeyInstalled(address indexed account, address indexed signer,
# uint256 nonce)`` event. The subgraph indexing of this event is open per
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
    """Return True iff the SessionKeyModule has emitted
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
