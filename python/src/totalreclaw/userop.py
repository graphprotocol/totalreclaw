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

Key addresses (deployed on all supported chains):
  - EntryPoint v0.7:        0x0000000071727De22E5E9d8BAf0edAc6f37da032
  - SimpleAccountFactory:   0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985
  - DataEdge (staging+prod): 0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from eth_account import Account
from eth_hash.auto import keccak

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

# execute(address,uint256,bytes)
EXECUTE_SELECTOR = keccak(b"execute(address,uint256,bytes)")[:4].hex()
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


def _encode_bytes_dynamic(data: bytes) -> str:
    """ABI-encode a dynamic ``bytes`` value (length-prefixed, padded to 32).

    Returns the length word + ceil-padded data, without the offset word.
    """
    hex_data = data.hex()
    # Pad to next multiple of 64 hex chars (32 bytes)
    padded_len = ((len(hex_data) + 63) // 64) * 64
    padded = hex_data.ljust(padded_len, "0")
    return _encode_uint256(len(data)) + padded


def encode_execute_calldata(target: str, value: int, data: bytes) -> str:
    """ABI-encode ``SimpleAccount.execute(address dest, uint256 value, bytes func)``.

    Returns 0x-prefixed hex.
    """
    dest = _pad32(target)
    val = _encode_uint256(value)
    # Offset to dynamic ``bytes`` param = 3 * 32 = 96
    offset = _encode_uint256(96)
    encoded_data = _encode_bytes_dynamic(data)
    return f"0x{EXECUTE_SELECTOR}{dest}{val}{offset}{encoded_data}"


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
# UserOp hash computation (v0.7)
# ---------------------------------------------------------------------------


def _pack_user_op_v07(user_op: dict) -> bytes:
    """Pack a v0.7 UserOp for hashing (excludes ``signature``).

    ERC-4337 v0.7 packing:
      sender, nonce, keccak256(initCode), keccak256(callData),
      accountGasLimits, preVerificationGas,
      gasFees, keccak256(paymasterAndData)
    """

    def _to_bytes32(hex_val: str) -> bytes:
        return bytes.fromhex(hex_val.replace("0x", "").zfill(64))

    def _to_uint(hex_val: str) -> int:
        if isinstance(hex_val, int):
            return hex_val
        return int(hex_val, 16) if hex_val.startswith("0x") else int(hex_val)

    sender = _to_bytes32(user_op["sender"])
    nonce = _to_bytes32(user_op["nonce"])

    # initCode = factory + factoryData (or empty)
    factory = user_op.get("factory", "")
    factory_data = user_op.get("factoryData", "")
    if factory and factory != "0x" and factory != "":
        init_code = bytes.fromhex(
            factory.replace("0x", "") + factory_data.replace("0x", "")
        )
    else:
        init_code = b""
    init_code_hash = keccak(init_code)

    call_data = bytes.fromhex(user_op["callData"].replace("0x", ""))
    call_data_hash = keccak(call_data)

    # accountGasLimits = verificationGasLimit (16 bytes) || callGasLimit (16 bytes)
    vgl = _to_uint(user_op.get("verificationGasLimit", "0x0"))
    cgl = _to_uint(user_op.get("callGasLimit", "0x0"))
    account_gas_limits = vgl.to_bytes(16, "big") + cgl.to_bytes(16, "big")

    pvg = _to_bytes32(user_op.get("preVerificationGas", "0x0"))

    # gasFees = maxPriorityFeePerGas (16 bytes) || maxFeePerGas (16 bytes)
    mpfpg = _to_uint(user_op.get("maxPriorityFeePerGas", "0x0"))
    mfpg = _to_uint(user_op.get("maxFeePerGas", "0x0"))
    gas_fees = mpfpg.to_bytes(16, "big") + mfpg.to_bytes(16, "big")

    # paymasterAndData = paymaster + paymasterVerificationGasLimit (16B)
    #                    + paymasterPostOpGasLimit (16B) + paymasterData
    paymaster = user_op.get("paymaster", "")
    if paymaster and paymaster != "0x" and paymaster != "":
        pm_vgl = _to_uint(
            user_op.get("paymasterVerificationGasLimit", "0x0")
        )
        pm_pogl = _to_uint(user_op.get("paymasterPostOpGasLimit", "0x0"))
        pm_data = user_op.get("paymasterData", "0x").replace("0x", "")
        paymaster_and_data = (
            bytes.fromhex(paymaster.replace("0x", ""))
            + pm_vgl.to_bytes(16, "big")
            + pm_pogl.to_bytes(16, "big")
            + bytes.fromhex(pm_data)
        )
    else:
        paymaster_and_data = b""
    paymaster_and_data_hash = keccak(paymaster_and_data)

    return (
        sender
        + nonce
        + init_code_hash
        + call_data_hash
        + account_gas_limits
        + pvg
        + gas_fees
        + paymaster_and_data_hash
    )


def compute_user_op_hash(
    user_op: dict, entry_point: str, chain_id: int
) -> bytes:
    """Compute the ERC-4337 v0.7 UserOperation hash for signing.

    ``hash = keccak256(keccak256(packUserOp), entryPoint, chainId)``
    """
    packed = _pack_user_op_v07(user_op)
    inner_hash = keccak(packed)

    entry_point_bytes = bytes.fromhex(entry_point.replace("0x", "").zfill(64))
    chain_id_bytes = chain_id.to_bytes(32, "big")

    outer = inner_hash + entry_point_bytes + chain_id_bytes
    return keccak(outer)


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

    _MAX_NONCE_RETRIES = 3

    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. Encode the execute calldata (idempotent, does not depend on nonce)
        #    SmartAccount.execute(dataEdgeAddress, 0, protobufPayload)
        call_data = encode_execute_calldata(
            DATA_EDGE_ADDRESS, 0, protobuf_payload
        )

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
                #    SimpleAccount's _validateSignature uses
                #    userOpHash.toEthSignedMessageHash().recover(signature),
                #    so we must sign with the Ethereum message prefix.
                from eth_account.messages import encode_defunct

                user_op_hash = compute_user_op_hash(
                    user_op, ENTRYPOINT_V07, chain_id
                )
                msg = encode_defunct(user_op_hash)
                signed = Account.sign_message(msg, eoa_private_key)
                sig_hex = signed.signature.hex()
                if not sig_hex.startswith("0x"):
                    sig_hex = "0x" + sig_hex
                user_op["signature"] = sig_hex

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
                if "AA25" in str(e) and attempt < _MAX_NONCE_RETRIES - 1:
                    logger.warning(
                        "AA25 nonce conflict (attempt %d/%d), retrying...",
                        attempt + 1,
                        _MAX_NONCE_RETRIES,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
