"""
TotalReclaw Client -- High-level API.

Usage:
    from totalreclaw import TotalReclaw

    client = TotalReclaw(recovery_phrase="abandon abandon ...")
    await client.remember("Pedro prefers dark mode")
    results = await client.recall("What does Pedro prefer?")
    await client.forget(results[0].id)
    facts = await client.export_all()
    status = await client.status()
    await client.close()

Legacy parameter names (mnemonic, relay_url) are still accepted for
backwards compatibility but are deprecated.
"""
from __future__ import annotations
from typing import Optional

from .crypto import (
    derive_keys_from_mnemonic,
    derive_lsh_seed,
    compute_auth_key_hash,
    DerivedKeys,
)
from .lsh import LSHHasher
from .relay import RelayClient, BillingStatus, DEFAULT_RELAY_URL
from .reranker import RerankerResult
from .operations import store_fact, search_facts, forget_fact, export_facts

# Smart Account address derivation constants
# These match the CREATE2 deterministic address generation used by
# SimpleSmartAccount in the permissionless library
SIMPLE_ACCOUNT_FACTORY = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985"
ENTRYPOINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"


def _get_eoa_account(mnemonic: str):
    """Derive the EOA (externally-owned account) from a BIP-39 mnemonic.

    Returns the full ``LocalAccount`` so callers can access both address and
    private key.
    """
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(mnemonic.strip(), account_path="m/44'/60'/0'/0/0")


def _get_eoa_address(mnemonic: str) -> str:
    """Derive the EOA address from a BIP-39 mnemonic."""
    return _get_eoa_account(mnemonic).address


async def _derive_smart_account_address(mnemonic: str, rpc_url: str = "https://sepolia.base.org") -> str:
    """Derive the CREATE2 Smart Account address by querying the factory contract.

    Calls SimpleAccountFactory.getAddress(owner, 0) via eth_call on a public RPC.
    This matches the `toSimpleSmartAccount` logic in permissionless/viem.

    Factory: 0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985 (v0.7)
    Method: getAddress(address owner, uint256 salt) view returns (address)
    """
    import httpx

    eoa_address = _get_eoa_address(mnemonic)

    # ABI-encode: getAddress(address,uint256)
    # keccak256("getAddress(address,uint256)")[:4] = 0x8cb84e18
    selector = "8cb84e18"

    # Pad owner address to 32 bytes and salt (0) to 32 bytes
    owner_padded = eoa_address.lower().replace("0x", "").zfill(64)
    salt_padded = "0" * 64

    calldata = f"0x{selector}{owner_padded}{salt_padded}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {"to": SIMPLE_ACCOUNT_FACTORY, "data": calldata},
                    "latest",
                ],
                "id": 1,
            },
        )
        result = resp.json().get("result", "")
        if not result or result == "0x" or len(result) < 66:
            # Fallback to EOA if RPC fails
            return eoa_address.lower()
        # Result is ABI-encoded address (32 bytes, last 20 bytes are the address)
        address = "0x" + result[-40:]
        return address.lower()


class TotalReclaw:
    """High-level TotalReclaw client with remember/recall/forget/export/status.

    The wallet_address is the CREATE2 Smart Account address. If not provided,
    call `await client.resolve_address()` before using remember/recall/forget/export
    to derive it via an RPC call to the SimpleAccountFactory contract.

    Parameters
    ----------
    recovery_phrase : str
        BIP-39 12-word recovery phrase (preferred name).
    server_url : str
        Relay server URL (preferred name, default: ``https://api.totalreclaw.xyz``).
    mnemonic : str
        Deprecated alias for ``recovery_phrase``.
    relay_url : str
        Deprecated alias for ``server_url``.
    wallet_address : str, optional
        Pre-resolved Smart Account address.
    is_test : bool
        Send ``X-TotalReclaw-Test: true`` header.
    """

    def __init__(
        self,
        recovery_phrase: Optional[str] = None,
        server_url: Optional[str] = None,
        wallet_address: Optional[str] = None,
        is_test: bool = False,
        *,
        mnemonic: Optional[str] = None,
        relay_url: Optional[str] = None,
    ):
        resolved_mnemonic = recovery_phrase or mnemonic
        if not resolved_mnemonic:
            raise ValueError("recovery_phrase is required")
        self._mnemonic = resolved_mnemonic.strip()
        self._keys = derive_keys_from_mnemonic(self._mnemonic)
        self._lsh_seed = derive_lsh_seed(self._mnemonic, self._keys.salt)
        self._lsh_hasher: Optional[LSHHasher] = None
        self._auth_key_hex = self._keys.auth_key.hex()
        resolved_url = server_url or relay_url or DEFAULT_RELAY_URL
        self._relay_url = resolved_url

        # Derive EOA account (address + private key) for UserOp signing
        eoa_acct = _get_eoa_account(self._mnemonic)
        self._eoa_address: str = eoa_acct.address
        self._eoa_private_key: bytes = bytes(eoa_acct.key)

        # Use provided address, or fall back to EOA (resolve_address fixes it later)
        self._wallet_address = (wallet_address or self._eoa_address).lower()
        self._address_resolved = wallet_address is not None
        self._relay = RelayClient(
            relay_url=resolved_url,
            auth_key_hex=self._auth_key_hex,
            wallet_address=self._wallet_address,
            is_test=is_test,
        )
        self._registered = False

    async def resolve_address(self) -> str:
        """Resolve the CREATE2 Smart Account address via RPC.

        Must be called before remember/recall/forget/export if wallet_address
        was not provided at construction time.
        """
        if self._address_resolved:
            return self._wallet_address

        self._wallet_address = await _derive_smart_account_address(self._mnemonic)
        self._address_resolved = True
        # Update relay client with resolved address
        self._relay._wallet_address = self._wallet_address
        return self._wallet_address

    async def _ensure_address(self) -> None:
        """Lazily resolve the Smart Account address if not yet done."""
        if not self._address_resolved:
            await self.resolve_address()

    async def _ensure_registered(self) -> None:
        """Register auth key with relay if not yet done (idempotent).

        Without this, all relay queries return 401. The relay returns 200
        for already-registered users, so this is safe to call on every startup.
        """
        if self._registered:
            return
        try:
            await self.register()
        except Exception:
            # Best-effort — relay may be unreachable; will retry on next call.
            pass

    def _get_lsh_hasher(self, dims: int = 1024) -> LSHHasher:
        if self._lsh_hasher is None:
            self._lsh_hasher = LSHHasher(self._lsh_seed, dims)
        return self._lsh_hasher

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def keys(self) -> DerivedKeys:
        return self._keys

    async def register(self) -> str:
        """Register with the relay. Returns user_id."""
        auth_hash = compute_auth_key_hash(self._keys.auth_key)
        user_id = await self._relay.register(auth_hash, self._keys.salt.hex())
        self._registered = True
        return user_id

    async def remember(
        self,
        text: str,
        embedding: Optional[list[float]] = None,
        importance: float = 0.5,
        source: str = "python-client",
    ) -> str:
        """Store a fact. Returns the fact ID."""
        await self._ensure_address()
        await self._ensure_registered()
        lsh = self._get_lsh_hasher() if embedding else None
        return await store_fact(
            text=text,
            keys=self._keys,
            owner=self._wallet_address,
            relay=self._relay,
            lsh_hasher=lsh,
            embedding=embedding,
            importance=importance,
            source=source,
            eoa_private_key=self._eoa_private_key,
            eoa_address=self._eoa_address,
            sender=self._wallet_address,
        )

    async def recall(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        max_candidates: int = 250,
        top_k: int = 8,
    ) -> list[RerankerResult]:
        """Search for facts matching a query. Returns ranked results."""
        await self._ensure_address()
        await self._ensure_registered()
        lsh = self._get_lsh_hasher() if query_embedding else None
        return await search_facts(
            query=query,
            keys=self._keys,
            owner=self._wallet_address,
            relay=self._relay,
            query_embedding=query_embedding,
            lsh_hasher=lsh,
            max_candidates=max_candidates,
            top_k=top_k,
        )

    async def forget(self, fact_id: str) -> bool:
        """Soft-delete a fact by writing a tombstone."""
        await self._ensure_address()
        await self._ensure_registered()
        return await forget_fact(
            fact_id,
            self._wallet_address,
            self._relay,
            eoa_private_key=self._eoa_private_key,
            eoa_address=self._eoa_address,
            sender=self._wallet_address,
        )

    async def export_all(self) -> list[dict]:
        """Export all active facts, decrypted."""
        await self._ensure_address()
        await self._ensure_registered()
        return await export_facts(self._keys, self._wallet_address, self._relay)

    async def status(self) -> BillingStatus:
        """Get billing status."""
        await self._ensure_address()
        await self._ensure_registered()
        return await self._relay.get_billing_status()

    async def close(self):
        """Close the HTTP client."""
        await self._relay.close()
