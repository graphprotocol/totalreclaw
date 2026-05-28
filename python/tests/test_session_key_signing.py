"""Tests for cred-8 Python EIP-712 session-key signing.

Two surfaces under test:
  - ``totalreclaw.grant.SessionKeyPermissionGrant`` and helpers
  - ``totalreclaw.userop.sign_userop_with_session_key`` +
    ``totalreclaw.userop.session_key_grant_was_installed``

The EIP-712 typehash constants are duplicated from Solidity
``SessionKeyModule.sol`` (cred-5 stage 2, PR #272). The cross-language
parity fixture (cred-9) consumes the same inputs from this file.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import decode as abi_decode
from eth_keys import keys as eth_keys
from eth_utils import keccak, to_checksum_address

from totalreclaw.grant import (
    DOMAIN_NAME_HASH,
    DOMAIN_TYPE_STR,
    DOMAIN_TYPEHASH,
    DOMAIN_VERSION_HASH,
    EXECUTE_BATCH_SELECTOR,
    EXECUTE_SELECTOR,
    GRANT_TYPE_STR,
    GRANT_TYPEHASH,
    PERMISSION_GRANT_TUPLE_ABI,
    SCOPE_TYPE_STR,
    SCOPE_TYPEHASH,
    SessionKeyPermissionGrant,
    encode_install_signature,
    recover_address,
    sign_digest,
)
from totalreclaw.userop import (
    ENTRYPOINT_V07,
    sign_userop_with_session_key,
    session_key_grant_was_installed,
)

# ---------------------------------------------------------------------------
# Shared deterministic fixture material — used by cred-9 parity tests too
# ---------------------------------------------------------------------------

# Anvil account #0 — public key 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
MASTER_PRIV: bytes = bytes.fromhex(
    "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
MASTER_ADDR: str = eth_keys.PrivateKey(MASTER_PRIV).public_key.to_checksum_address()

# Anvil account #1 — used as the session signer
SESSION_PRIV: bytes = bytes.fromhex(
    "59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
)
SESSION_ADDR: str = eth_keys.PrivateKey(SESSION_PRIV).public_key.to_checksum_address()

SMART_ACCOUNT: str = "0x2c0CF74B2b76110708CA431796367779e3738250"
DATA_EDGE: str = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca"
MODULE_ADDR: str = "0x1234567890123456789012345678901234567890"


def _make_grant(
    chain_id: int = 84532,
    verifying_contract: str = MODULE_ADDR,
    nonce: int = 1,
    issued_at: int = 1748275200,
    value_max: int = 0,
) -> SessionKeyPermissionGrant:
    return SessionKeyPermissionGrant(
        account=SMART_ACCOUNT,
        signer=SESSION_ADDR,
        target=DATA_EDGE,
        selectors=(EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR),
        value_max=value_max,
        nonce=nonce,
        issued_at=issued_at,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
    )


# ---------------------------------------------------------------------------
# EIP-712 typehash constants — must match Solidity SessionKeyModule.sol
# ---------------------------------------------------------------------------


class TestTypehashConstants:
    """Lock the typehash byte values so any drift fails loudly.

    Hex values are recomputed from the canonical type strings; if the
    Solidity contract ever changes the type string, both sides break in
    lockstep — the cred-9 fixture is the on-chain check.
    """

    def test_domain_typehash(self):
        assert DOMAIN_TYPEHASH == keccak(DOMAIN_TYPE_STR)
        assert DOMAIN_TYPEHASH.hex() == (
            "8b73c3c69bb8fe3d512ecc4cf759cc79239f7b179b0ffacaa9a75d522b39400f"
        )

    def test_scope_typehash(self):
        assert SCOPE_TYPEHASH == keccak(SCOPE_TYPE_STR)
        assert SCOPE_TYPEHASH.hex() == (
            "ce3f87372ac9cd6fc3f75d271423fb91bee1ffc3196bdd074222633e134b9d79"
        )

    def test_grant_typehash(self):
        assert GRANT_TYPEHASH == keccak(GRANT_TYPE_STR)
        assert GRANT_TYPEHASH.hex() == (
            "5e852a79525963282127d9175cacc3e5ae8b893fd67f226fa694c192c75a2cc3"
        )

    def test_domain_name_hash(self):
        assert DOMAIN_NAME_HASH == keccak(b"TotalReclawSessionKey")

    def test_domain_version_hash(self):
        assert DOMAIN_VERSION_HASH == keccak(b"1")

    def test_execute_selector_matches_solidity_constant(self):
        # SessionKeyModule.sol: EXECUTE_SELECTOR = 0xb61d27f6
        assert EXECUTE_SELECTOR == bytes.fromhex("b61d27f6")
        # Sanity: matches keccak("execute(address,uint256,bytes)")[:4]
        assert EXECUTE_SELECTOR == keccak(b"execute(address,uint256,bytes)")[:4]

    def test_execute_batch_selector_matches_solidity_constant(self):
        assert EXECUTE_BATCH_SELECTOR == bytes.fromhex("47e1da2a")
        assert (
            EXECUTE_BATCH_SELECTOR
            == keccak(b"executeBatch(address[],uint256[],bytes[])")[:4]
        )


# ---------------------------------------------------------------------------
# Digest + sign + recover round-trip
# ---------------------------------------------------------------------------


class TestGrantDigestAndSigning:
    def test_digest_is_32_bytes(self):
        g = _make_grant()
        assert len(g.eip712_digest()) == 32

    def test_digest_deterministic(self):
        g1 = _make_grant()
        g2 = _make_grant()
        assert g1.eip712_digest() == g2.eip712_digest()

    def test_sign_returns_65_byte_signature_with_v_27_or_28(self):
        g = _make_grant()
        signed = g.sign(MASTER_PRIV)
        assert len(signed.master_signature) == 65
        v = signed.master_signature[64]
        assert v in (27, 28)

    def test_recover_master_round_trip(self):
        g = _make_grant().sign(MASTER_PRIV)
        assert g.recover_master().lower() == MASTER_ADDR.lower()

    def test_recover_rejects_empty_signature(self):
        g = _make_grant()
        with pytest.raises(ValueError, match="master_signature is empty"):
            g.recover_master()

    def test_with_signature_rejects_wrong_length(self):
        g = _make_grant()
        with pytest.raises(ValueError, match="65 bytes"):
            g.with_signature(b"\x00" * 64)

    def test_sign_rejects_short_priv_key(self):
        g = _make_grant()
        with pytest.raises(ValueError, match="32 bytes"):
            g.sign(b"\x00" * 16)


# ---------------------------------------------------------------------------
# Replay-protection: chainId + verifyingContract change the digest
# ---------------------------------------------------------------------------


class TestReplayProtection:
    def test_chain_id_changes_digest(self):
        d1 = _make_grant(chain_id=84532).eip712_digest()
        d2 = _make_grant(chain_id=100).eip712_digest()
        assert d1 != d2

    def test_verifying_contract_changes_digest(self):
        d1 = _make_grant(verifying_contract=MODULE_ADDR).eip712_digest()
        d2 = _make_grant(
            verifying_contract="0x0000000000000000000000000000000000001234"
        ).eip712_digest()
        assert d1 != d2

    def test_account_changes_digest(self):
        d1 = _make_grant().eip712_digest()
        g2 = SessionKeyPermissionGrant(
            account="0x0000000000000000000000000000000000005678",
            signer=SESSION_ADDR,
            target=DATA_EDGE,
            selectors=(EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR),
            value_max=0,
            nonce=1,
            issued_at=1748275200,
            chain_id=84532,
            verifying_contract=MODULE_ADDR,
        )
        assert d1 != g2.eip712_digest()

    def test_nonce_changes_digest(self):
        d1 = _make_grant(nonce=1).eip712_digest()
        d2 = _make_grant(nonce=2).eip712_digest()
        assert d1 != d2

    def test_selector_order_changes_digest(self):
        """``keccak256(abi.encodePacked(selectors))`` is order-sensitive."""
        g1 = SessionKeyPermissionGrant(
            account=SMART_ACCOUNT, signer=SESSION_ADDR, target=DATA_EDGE,
            selectors=(EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR),
            value_max=0, nonce=1, issued_at=1, chain_id=84532,
            verifying_contract=MODULE_ADDR,
        )
        g2 = SessionKeyPermissionGrant(
            account=SMART_ACCOUNT, signer=SESSION_ADDR, target=DATA_EDGE,
            selectors=(EXECUTE_BATCH_SELECTOR, EXECUTE_SELECTOR),
            value_max=0, nonce=1, issued_at=1, chain_id=84532,
            verifying_contract=MODULE_ADDR,
        )
        assert g1.eip712_digest() != g2.eip712_digest()


# ---------------------------------------------------------------------------
# Selector normalisation
# ---------------------------------------------------------------------------


class TestSelectorNormalisation:
    def test_hex_string_selectors_accepted(self):
        g_bytes = _make_grant()
        g_hex = SessionKeyPermissionGrant(
            account=SMART_ACCOUNT, signer=SESSION_ADDR, target=DATA_EDGE,
            selectors=("0xb61d27f6", "0x47e1da2a"),
            value_max=0, nonce=1, issued_at=1748275200, chain_id=84532,
            verifying_contract=MODULE_ADDR,
        )
        assert g_bytes.scope_struct_hash() == g_hex.scope_struct_hash()

    def test_rejects_wrong_length_selector(self):
        g = SessionKeyPermissionGrant(
            account=SMART_ACCOUNT, signer=SESSION_ADDR, target=DATA_EDGE,
            selectors=(b"\x00\x01\x02",),  # 3 bytes
            value_max=0, nonce=1, issued_at=1, chain_id=84532,
            verifying_contract=MODULE_ADDR,
        )
        with pytest.raises(ValueError, match="4 bytes"):
            g.scope_struct_hash()


# ---------------------------------------------------------------------------
# Lazy-install signature ABI encoding
# ---------------------------------------------------------------------------


class TestEncodeInstallSignature:
    def _signed_grant(self) -> SessionKeyPermissionGrant:
        return _make_grant().sign(MASTER_PRIV)

    def test_encode_returns_nonempty_bytes(self):
        ecdsa_sig = sign_digest(b"\x00" * 32, SESSION_PRIV)
        out = encode_install_signature(self._signed_grant(), ecdsa_sig)
        assert isinstance(out, bytes)
        # Header (offset to PermissionGrant tuple) + tuple body + offset to
        # ecdsa_sig bytes + length-prefix + padded 65 bytes ≈ several hundred.
        assert len(out) > 200

    def test_encode_roundtrip_decodes_to_struct(self):
        signed = self._signed_grant()
        ecdsa_sig = sign_digest(b"\x11" * 32, SESSION_PRIV)
        encoded = encode_install_signature(signed, ecdsa_sig)

        # The Solidity decode is `(PermissionGrant, bytes)` so the Python
        # decode mirror is the same tuple ABI plus a tail bytes.
        decoded_struct, decoded_sig = abi_decode(
            [PERMISSION_GRANT_TUPLE_ABI, "bytes"], encoded
        )

        (
            version, account, signer, target, selectors, value_max,
            nonce, issued_at, chain_id, verifying_contract, master_sig,
        ) = decoded_struct

        assert version == signed.version
        assert to_checksum_address(account) == to_checksum_address(SMART_ACCOUNT)
        assert to_checksum_address(signer) == to_checksum_address(SESSION_ADDR)
        assert to_checksum_address(target) == to_checksum_address(DATA_EDGE)
        assert tuple(bytes(s) for s in selectors) == (
            EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR,
        )
        assert value_max == 0
        assert nonce == 1
        assert issued_at == 1748275200
        assert chain_id == 84532
        assert to_checksum_address(verifying_contract) == to_checksum_address(
            MODULE_ADDR
        )
        assert bytes(master_sig) == signed.master_signature
        assert bytes(decoded_sig) == ecdsa_sig

    def test_decoded_master_signature_recovers_master(self):
        """The full chain: encode -> decode -> recover master from
        ``master_signature`` over the same EIP-712 digest."""
        signed = self._signed_grant()
        ecdsa_sig = sign_digest(b"\x22" * 32, SESSION_PRIV)
        encoded = encode_install_signature(signed, ecdsa_sig)
        decoded_struct, _ = abi_decode([PERMISSION_GRANT_TUPLE_ABI, "bytes"], encoded)
        decoded_master_sig = bytes(decoded_struct[10])
        assert (
            recover_address(signed.eip712_digest(), decoded_master_sig).lower()
            == MASTER_ADDR.lower()
        )

    def test_encode_rejects_wrong_length_session_sig(self):
        with pytest.raises(ValueError, match="65 bytes"):
            encode_install_signature(self._signed_grant(), b"\x00" * 64)

    def test_encode_rejects_unsigned_grant(self):
        unsigned = _make_grant()  # no .sign(...)
        ecdsa_sig = sign_digest(b"\x00" * 32, SESSION_PRIV)
        with pytest.raises(ValueError, match="master_signature must be set"):
            encode_install_signature(unsigned, ecdsa_sig)


# ---------------------------------------------------------------------------
# sign_userop_with_session_key
# ---------------------------------------------------------------------------


def _minimal_userop() -> dict:
    """Smallest userOp that ``compute_user_op_hash`` accepts. The Rust hash
    impl ignores most fields when computing the v0.7 hash structure but the
    serde shape requires them — provide defaults that round-trip cleanly."""
    return {
        "sender": SMART_ACCOUNT,
        "nonce": hex(0),
        "callData": "0x",
        "callGasLimit": hex(100_000),
        "verificationGasLimit": hex(100_000),
        "preVerificationGas": hex(21_000),
        "maxFeePerGas": hex(1_000_000_000),
        "maxPriorityFeePerGas": hex(1_000_000_000),
        "paymaster": "0x0000000000000000000000000000000000000000",
        "paymasterVerificationGasLimit": hex(0),
        "paymasterPostOpGasLimit": hex(0),
        "paymasterData": "0x",
    }


class TestSignUserOpWithSessionKey:
    def test_steady_state_returns_raw_65_byte_ecdsa(self):
        sig = sign_userop_with_session_key(
            user_op=_minimal_userop(),
            session_priv_key=SESSION_PRIV,
            entry_point=ENTRYPOINT_V07,
            chain_id=84532,
            include_grant=False,
        )
        assert isinstance(sig, bytes)
        assert len(sig) == 65
        # v in {27, 28}
        assert sig[64] in (27, 28)

    def test_steady_state_recovers_session_signer(self):
        """The raw ECDSA must recover to the session signer — the module
        does the exact same lookup with no prefix."""
        from totalreclaw.userop import compute_user_op_hash
        user_op = _minimal_userop()
        sig = sign_userop_with_session_key(
            user_op=user_op,
            session_priv_key=SESSION_PRIV,
            entry_point=ENTRYPOINT_V07,
            chain_id=84532,
            include_grant=False,
        )
        userop_hash = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        assert recover_address(userop_hash, sig).lower() == SESSION_ADDR.lower()

    def test_install_path_returns_abi_encoded_payload(self):
        signed = _make_grant().sign(MASTER_PRIV)
        sig = sign_userop_with_session_key(
            user_op=_minimal_userop(),
            session_priv_key=SESSION_PRIV,
            entry_point=ENTRYPOINT_V07,
            chain_id=84532,
            include_grant=True,
            grant=signed,
        )
        # Decode shape must be `(PermissionGrant, bytes)` per Solidity.
        decoded_struct, decoded_ecdsa = abi_decode(
            [PERMISSION_GRANT_TUPLE_ABI, "bytes"], sig
        )
        assert len(bytes(decoded_ecdsa)) == 65
        assert decoded_struct[0] == 1  # version
        assert to_checksum_address(decoded_struct[1]) == to_checksum_address(
            SMART_ACCOUNT
        )

    def test_install_payload_ecdsa_recovers_session_signer(self):
        """The ECDSA component of the install payload must validate against
        the session signer (the module checks signer == grant.signer)."""
        from totalreclaw.userop import compute_user_op_hash
        signed = _make_grant().sign(MASTER_PRIV)
        user_op = _minimal_userop()
        sig = sign_userop_with_session_key(
            user_op=user_op,
            session_priv_key=SESSION_PRIV,
            entry_point=ENTRYPOINT_V07,
            chain_id=84532,
            include_grant=True,
            grant=signed,
        )
        _, decoded_ecdsa = abi_decode([PERMISSION_GRANT_TUPLE_ABI, "bytes"], sig)
        userop_hash = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        recovered = recover_address(userop_hash, bytes(decoded_ecdsa))
        assert recovered.lower() == SESSION_ADDR.lower()

    def test_install_rejects_missing_grant(self):
        with pytest.raises(ValueError, match="grant is required"):
            sign_userop_with_session_key(
                user_op=_minimal_userop(),
                session_priv_key=SESSION_PRIV,
                entry_point=ENTRYPOINT_V07,
                chain_id=84532,
                include_grant=True,
            )

    def test_install_rejects_chain_id_mismatch(self):
        signed = _make_grant(chain_id=84532).sign(MASTER_PRIV)
        with pytest.raises(ValueError, match="chain_id"):
            sign_userop_with_session_key(
                user_op=_minimal_userop(),
                session_priv_key=SESSION_PRIV,
                entry_point=ENTRYPOINT_V07,
                chain_id=100,  # mismatch
                include_grant=True,
                grant=signed,
            )

    def test_install_rejects_unsigned_grant(self):
        unsigned = _make_grant()
        with pytest.raises(ValueError, match="master_signature"):
            sign_userop_with_session_key(
                user_op=_minimal_userop(),
                session_priv_key=SESSION_PRIV,
                entry_point=ENTRYPOINT_V07,
                chain_id=84532,
                include_grant=True,
                grant=unsigned,
            )

    def test_rejects_short_session_priv_key(self):
        with pytest.raises(ValueError, match="32 bytes"):
            sign_userop_with_session_key(
                user_op=_minimal_userop(),
                session_priv_key=b"\x00" * 16,
                entry_point=ENTRYPOINT_V07,
                chain_id=84532,
            )


# ---------------------------------------------------------------------------
# session_key_grant_was_installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionKeyGrantWasInstalled:
    async def test_returns_true_when_subgraph_has_entry(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock(
            return_value={
                "data": {"sessionKeyInstalleds": [{"id": "0xabc", "nonce": "1"}]}
            }
        )
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 84532, client
        )
        assert ok is True
        # Verify the query forwarded the right chain key.
        kwargs = client.query_subgraph.call_args.kwargs
        assert kwargs.get("chain") == "base-sepolia"

    async def test_returns_true_for_gnosis_chain(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock(
            return_value={"data": {"sessionKeyInstalleds": [{"id": "0xdef"}]}}
        )
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 100, client
        )
        assert ok is True
        assert client.query_subgraph.call_args.kwargs.get("chain") == "gnosis"

    async def test_returns_false_when_subgraph_empty(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock(
            return_value={"data": {"sessionKeyInstalleds": []}}
        )
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 84532, client
        )
        assert ok is False

    async def test_returns_false_when_subgraph_errors(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock(side_effect=RuntimeError("boom"))
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 84532, client
        )
        assert ok is False

    async def test_returns_false_for_unknown_chain_without_calling_subgraph(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock()
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 999, client
        )
        assert ok is False
        client.query_subgraph.assert_not_called()

    async def test_returns_false_when_data_key_missing(self):
        client = MagicMock()
        client.query_subgraph = AsyncMock(return_value={"errors": [{"message": "x"}]})
        ok = await session_key_grant_was_installed(
            SMART_ACCOUNT, SESSION_ADDR, 84532, client
        )
        assert ok is False
