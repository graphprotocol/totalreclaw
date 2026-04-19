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
from typing import Optional

import httpx
from eth_hash.auto import keccak

import totalreclaw_core

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Well-known addresses
# ---------------------------------------------------------------------------

ENTRYPOINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
SIMPLE_ACCOUNT_FACTORY = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985"
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


def encode_execute_calldata_for_data_edge(protobuf_payload: bytes) -> str:
    """ABI-encode ``SimpleAccount.execute(dataEdge, 0, protobuf)`` calldata.

    Delegates to totalreclaw_core.encode_single_call() which hardcodes the
    DataEdge address and value=0. Returns 0x-prefixed hex.
    """
    calldata_bytes = totalreclaw_core.encode_single_call(protobuf_payload)
    return "0x" + calldata_bytes.hex()


# Max batch size mirrors the Rust ``MAX_BATCH_SIZE`` constant and the TS
# ``skill/plugin/store.ts`` batcher. Going higher risks hitting block gas
# limits on some chains (e.g. Gnosis) and strains the bundler.
MAX_BATCH_SIZE: int = 15


def encode_execute_batch_calldata_for_data_edge(
    protobuf_payloads: list[bytes],
) -> str:
    """ABI-encode ``SimpleAccount.executeBatch(dests, values, datas)`` calldata.

    Delegates to :func:`totalreclaw_core.encode_batch_call` which hardcodes
    the DataEdge address (same target for every call) and ``value=0``.
    The Rust core returns ``execute(...)`` calldata (not ``executeBatch``)
    when ``len(protobuf_payloads) == 1`` so a batch-of-1 is byte-identical
    to the single-fact fast path — this preserves gas parity with the TS
    plugin's ``encodeBatchCalls`` helper.

    Parameters
    ----------
    protobuf_payloads : list of bytes
        One raw protobuf payload per fact. Must contain 1..15 entries.

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
    calldata_bytes = totalreclaw_core.encode_batch_call(protobuf_payloads)
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
    resp = await http.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [address, "latest"],
            "id": 1,
        },
    )
    return resp.json().get("result", "0x")


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
        "X-TotalReclaw-Client": client_id,
        "X-Wallet-Address": wallet_address,
    }
    if session_id:
        headers["X-TotalReclaw-Session"] = session_id

    _MAX_NONCE_RETRIES = 3

    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. Encode the execute calldata (idempotent, does not depend on nonce)
        #    SmartAccount.execute(dataEdgeAddress, 0, protobufPayload)
        #    Delegates to Rust core which hardcodes DataEdge address + value=0
        call_data = encode_execute_calldata_for_data_edge(protobuf_payload)

        # Retry loop for AA25 nonce conflicts.  When two UserOps race for
        # the same nonce the bundler rejects one with AA25.  Re-fetching
        # the nonce and rebuilding the UserOp resolves it.
        for attempt in range(_MAX_NONCE_RETRIES):
            try:
                # 2. Get nonce from EntryPoint
                nonce = await get_nonce(http, sender, chain_id)
                logger.debug("Nonce for %s: %d", sender, nonce)

                # 3. Check if account is already deployed
                code = await _eth_get_code(http, rpc_url, sender)
                is_deployed = code != "0x" and len(code) > 2

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

                return send_data.get("result", "")

            except Exception as e:
                err_str = str(e)
                if ("AA25" in err_str or "AA10" in err_str) and attempt < _MAX_NONCE_RETRIES - 1:
                    logger.warning(
                        "AA25/AA10 nonce or sender conflict (attempt %d/%d), retrying...",
                        attempt + 1,
                        _MAX_NONCE_RETRIES,
                    )
                    await asyncio.sleep(2 ** attempt)
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
        "X-TotalReclaw-Client": client_id,
        "X-Wallet-Address": wallet_address,
    }
    if session_id:
        headers["X-TotalReclaw-Session"] = session_id

    _MAX_NONCE_RETRIES = 3

    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. Encode the executeBatch calldata once. Independent of nonce,
        #    so we compute it before the retry loop. Delegates to the
        #    Rust core for byte-parity with the TS plugin.
        call_data = encode_execute_batch_calldata_for_data_edge(
            protobuf_payloads
        )

        # Retry loop for AA25 nonce conflicts.  When two UserOps race for
        # the same nonce the bundler rejects one with AA25.  Re-fetching
        # the nonce and rebuilding the UserOp resolves it. Collapses to
        # O(1) retries for the batch path vs O(n) for sequential sends.
        for attempt in range(_MAX_NONCE_RETRIES):
            try:
                # 2. Get nonce from EntryPoint
                nonce = await get_nonce(http, sender, chain_id)
                logger.debug(
                    "Batch nonce for %s: %d (batch size %d)",
                    sender, nonce, len(protobuf_payloads),
                )

                # 3. Check if account is already deployed
                code = await _eth_get_code(http, rpc_url, sender)
                is_deployed = code != "0x" and len(code) > 2

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

                return send_data.get("result", "")

            except Exception as e:
                err_str = str(e)
                if ("AA25" in err_str or "AA10" in err_str) and attempt < _MAX_NONCE_RETRIES - 1:
                    logger.warning(
                        "Batch AA25/AA10 nonce or sender conflict "
                        "(attempt %d/%d, batch size %d), retrying...",
                        attempt + 1,
                        _MAX_NONCE_RETRIES,
                        len(protobuf_payloads),
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
