"""Tests for cred-8 Python EIP-712 session-key signing.

Two surfaces under test:
  - ``totalreclaw.grant.SessionKeyPermissionGrant`` and helpers
  - ``totalreclaw.userop.sign_userop_with_session_key`` +
    ``totalreclaw.userop.session_key_grant_was_installed``

The EIP-712 typehash constants are duplicated from Solidity
``SessionKeyModule.sol`` (cred-5 stage 2, PR #272). The cross-language
parity fixture (cred-9) consumes the same inputs from this file.

``TestKnownAnswerDigest`` below pins ``eip712_digest()`` against that
fixture as a fast, no-Node-required regression guard for #584 (Python was
previously hashing ``bytes4[]`` selectors as a tight 4-byte concat instead
of Solidity's ``abi.encodePacked`` 32-byte-per-element padding, producing a
digest that self-consistently verified in Python but never matched
Solidity/viem).
"""
from __future__ import annotations

import json
from pathlib import Path
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
# Known-answer digest — pins the #584 fix (encodePacked bytes4[] padding)
# ---------------------------------------------------------------------------


class TestKnownAnswerDigest:
    """Pins ``eip712_digest()`` for a fixed scope against a value that is
    independently known-correct, closing #584.

    Expected-value derivation (NOT copy-pasted blind from this codebase):

    1. Solidity's ``SessionKeyModule._grantDigest`` hashes selectors via
       ``keccak256(abi.encodePacked(g.selectors))`` where
       ``g.selectors`` is ``bytes4[] memory``. ``abi.encodePacked`` on an
       ARRAY argument does not tightly concatenate elements the way it
       does for top-level scalar arguments — only the array's length
       prefix is dropped. Each element keeps its normal ABI width, so
       every 4-byte selector is right-padded (left-aligned) inside a
       32-byte word before concatenation. Confirmed by compiling and
       running ``abi.encodePacked(bytes4[2])`` in Solidity: length is 64
       bytes, not 8 (see #584 + the header comment of
       ``contracts/contracts/SessionKeyModule.sol`` /
       ``tests/parity/fixtures/generate-session-key-grant-v1.ts``).
    2. ``EXPECTED_DIGEST_HEX`` below is ``eip712_hash`` from the
       cross-language parity fixture
       ``tests/parity/fixtures/session-key-grant-v1.json``, generated by
       the TS/viem side (``generate-session-key-grant-v1.ts``) applying
       exactly that padding rule.
    3. Independently re-derived for this fix (outside any client
       implementation) with a standalone script computing the EIP-712
       digest two ways — tight 4-byte concat vs. 32-byte-padded concat —
       over the fixture's exact scope/domain fields using raw
       ``keccak256``/ABI-word construction (no ``eth_abi``, no
       ``totalreclaw.grant``). Only the padded encoding reproduced
       ``EXPECTED_DIGEST_HEX``; the tight encoding produced
       ``0x67a37462b099aab8fcda5c711dd8515d2da9faed3810a3b06808d422e7961528``
       (the OLD, wrong, pre-#584 Python output for these inputs) — i.e.
       this test would have FAILED against the pre-fix implementation,
       proving it actually pins the fix rather than re-asserting
       whatever the code already does.
    4. ``test_matches_parity_fixture_file`` below loads the same JSON
       fixture at test time so a future fixture regen can't silently
       drift from the hardcoded value without failing CI.
    """

    # Verbatim from tests/parity/fixtures/session-key-grant-v1.json "grant"
    # + "domain" (Anvil well-known accounts; DATA_EDGE = prod DataEdge addr
    # used only as a fixture value here, not a live call).
    FIXTURE_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"
    FIXTURE_SIGNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    FIXTURE_TARGET = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca"
    FIXTURE_VERIFYING_CONTRACT = "0x0000000000000000000000000000000000001234"
    FIXTURE_CHAIN_ID = 84532
    FIXTURE_NONCE = 1
    FIXTURE_ISSUED_AT = 1748275200

    EXPECTED_DIGEST_HEX = (
        "7b9e7827d698e66fc94ac7f03438fa5680b84c0085391c29f925fd5ce66a9b7e"
    )

    def _fixture_grant(self) -> SessionKeyPermissionGrant:
        return SessionKeyPermissionGrant(
            account=self.FIXTURE_ACCOUNT,
            signer=self.FIXTURE_SIGNER,
            target=self.FIXTURE_TARGET,
            selectors=(EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR),
            value_max=0,
            nonce=self.FIXTURE_NONCE,
            issued_at=self.FIXTURE_ISSUED_AT,
            chain_id=self.FIXTURE_CHAIN_ID,
            verifying_contract=self.FIXTURE_VERIFYING_CONTRACT,
        )

    def test_digest_matches_known_answer(self):
        g = self._fixture_grant()
        assert g.eip712_digest().hex() == self.EXPECTED_DIGEST_HEX

    def test_scope_struct_hash_uses_padded_selectors(self):
        """Direct unit check on the previously-buggy inner hash: the
        keccak of the concatenated selectors must be over 32-byte-padded
        (64-byte total for 2 selectors) elements, not the tight 8-byte
        concat #584 used to compute."""
        padded = (
            EXECUTE_SELECTOR.ljust(32, b"\x00")
            + EXECUTE_BATCH_SELECTOR.ljust(32, b"\x00")
        )
        tight = EXECUTE_SELECTOR + EXECUTE_BATCH_SELECTOR
        assert len(padded) == 64
        assert len(tight) == 8

        expected_selectors_hash = keccak(padded)
        wrong_selectors_hash = keccak(tight)
        assert expected_selectors_hash != wrong_selectors_hash

        g = self._fixture_grant()
        # scope_struct_hash = keccak(abi.encode(SCOPE_TYPEHASH, target,
        # selectorsHash, valueMax)) — recompute the outer encode here with
        # the independently-derived selectorsHash and compare.
        from eth_abi import encode as abi_encode

        expected_scope_hash = keccak(
            abi_encode(
                ["bytes32", "address", "bytes32", "uint256"],
                [
                    SCOPE_TYPEHASH,
                    to_checksum_address(self.FIXTURE_TARGET),
                    expected_selectors_hash,
                    0,
                ],
            )
        )
        assert g.scope_struct_hash() == expected_scope_hash

    def test_matches_parity_fixture_file(self):
        """Load the actual cred-9 fixture JSON at test time (not just the
        hardcoded copy above) so a future ``npx tsx
        fixtures/generate-session-key-grant-v1.ts`` regen that silently
        changes the digest fails this test instead of drifting unnoticed."""
        fixture_path = (
            Path(__file__).resolve().parents[2]
            / "tests"
            / "parity"
            / "fixtures"
            / "session-key-grant-v1.json"
        )
        if not fixture_path.exists():
            pytest.skip(f"parity fixture not found at {fixture_path}")
        fixture = json.loads(fixture_path.read_text())

        g = SessionKeyPermissionGrant(
            account=fixture["grant"]["account"],
            signer=fixture["grant"]["signer"],
            target=fixture["grant"]["scope"]["target"],
            selectors=tuple(
                bytes.fromhex(s.removeprefix("0x"))
                for s in fixture["grant"]["scope"]["selectors"]
            ),
            value_max=int(fixture["grant"]["scope"]["valueMax"]),
            nonce=int(fixture["grant"]["nonce"]),
            issued_at=int(fixture["grant"]["issuedAt"]),
            chain_id=fixture["domain"]["chainId"],
            verifying_contract=fixture["domain"]["verifyingContract"],
        )
        assert "0x" + g.eip712_digest().hex() == fixture["eip712_hash"]


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
