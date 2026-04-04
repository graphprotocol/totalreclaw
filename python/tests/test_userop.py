"""Tests for ERC-4337 UserOperation construction."""
import pytest
from eth_hash.auto import keccak

from totalreclaw.userop import (
    ENTRYPOINT_V07,
    SIMPLE_ACCOUNT_FACTORY,
    DATA_EDGE_ADDRESS,
    CREATE_ACCOUNT_SELECTOR,
    GET_NONCE_SELECTOR,
    encode_execute_calldata_for_data_edge,
    encode_factory_data,
    compute_user_op_hash,
    sign_user_op_hash,
    _pad32,
    _encode_uint256,
)

# The execute selector is no longer exported (encoding delegated to Rust core),
# but we compute it here for validation in tests.
EXECUTE_SELECTOR = keccak(b"execute(address,uint256,bytes)")[:4].hex()


class TestABIEncoding:
    def test_pad32_short(self):
        assert _pad32("0x1234") == "0" * 60 + "1234"

    def test_pad32_full(self):
        val = "ab" * 32
        assert _pad32(val) == val

    def test_encode_uint256_zero(self):
        assert _encode_uint256(0) == "0" * 64

    def test_encode_uint256_one(self):
        result = _encode_uint256(1)
        assert len(result) == 64
        assert result == "0" * 63 + "1"


class TestFunctionSelectors:
    def test_create_account_selector(self):
        expected = keccak(b"createAccount(address,uint256)")[:4].hex()
        assert CREATE_ACCOUNT_SELECTOR == expected

    def test_get_nonce_selector(self):
        expected = keccak(b"getNonce(address,uint192)")[:4].hex()
        assert GET_NONCE_SELECTOR == expected


class TestEncodeExecuteCalldata:
    def test_basic_encoding(self):
        """Rust core encodes execute(dataEdge, 0, payload) — verify it starts with the selector."""
        data = b"\xde\xad\xbe\xef"
        result = encode_execute_calldata_for_data_edge(data)

        # Should start with 0x + execute selector
        assert result.startswith(f"0x{EXECUTE_SELECTOR}")
        # Should contain the DataEdge address (padded, lowercase)
        assert _pad32(DATA_EDGE_ADDRESS).lower() in result.lower()

    def test_empty_data(self):
        result = encode_execute_calldata_for_data_edge(b"")
        assert result.startswith(f"0x{EXECUTE_SELECTOR}")

    def test_protobuf_payload(self):
        """Test with a realistic protobuf payload size."""
        payload = b"\x0a\x24" + b"a" * 36  # ~38 bytes
        result = encode_execute_calldata_for_data_edge(payload)
        assert result.startswith(f"0x{EXECUTE_SELECTOR}")
        # Verify the hex is valid
        bytes.fromhex(result[2:])


class TestEncodeFactoryData:
    def test_basic_encoding(self):
        owner = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
        result = encode_factory_data(owner, 0)
        assert result.startswith(f"0x{CREATE_ACCOUNT_SELECTOR}")
        assert _pad32(owner) in result

    def test_with_salt(self):
        owner = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
        result = encode_factory_data(owner, 42)
        assert _encode_uint256(42) in result


class TestUserOpHash:
    def test_hash_deterministic(self):
        user_op = {
            "sender": "0x2c0cf74b2b76110708ca431796367779e3738250",
            "nonce": "0x0",
            "callData": "0xdeadbeef",
            "callGasLimit": hex(500_000),
            "verificationGasLimit": hex(500_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(2_000_000_000),
            "maxPriorityFeePerGas": hex(1_500_000_000),
            "signature": "0x" + "ff" * 65,
        }
        h1 = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        h2 = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        assert h1 == h2
        assert len(h1) == 32

    def test_hash_changes_with_chain_id(self):
        user_op = {
            "sender": "0x2c0cf74b2b76110708ca431796367779e3738250",
            "nonce": "0x0",
            "callData": "0xdeadbeef",
            "callGasLimit": hex(500_000),
            "verificationGasLimit": hex(500_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(2_000_000_000),
            "maxPriorityFeePerGas": hex(1_500_000_000),
        }
        h1 = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        h2 = compute_user_op_hash(user_op, ENTRYPOINT_V07, 100)
        assert h1 != h2

    def test_hash_with_factory(self):
        user_op = {
            "sender": "0x2c0cf74b2b76110708ca431796367779e3738250",
            "nonce": "0x0",
            "factory": SIMPLE_ACCOUNT_FACTORY,
            "factoryData": encode_factory_data(
                "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
            ),
            "callData": "0xdeadbeef",
            "callGasLimit": hex(500_000),
            "verificationGasLimit": hex(500_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(2_000_000_000),
            "maxPriorityFeePerGas": hex(1_500_000_000),
        }
        h = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        assert len(h) == 32

    def test_hash_with_paymaster(self):
        user_op = {
            "sender": "0x2c0cf74b2b76110708ca431796367779e3738250",
            "nonce": "0x0",
            "callData": "0xdeadbeef",
            "callGasLimit": hex(500_000),
            "verificationGasLimit": hex(500_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(2_000_000_000),
            "maxPriorityFeePerGas": hex(1_500_000_000),
            "paymaster": "0x0000000000325602a77416A16136FDafd04b299f",
            "paymasterData": "0xabcdef",
            "paymasterVerificationGasLimit": hex(100_000),
            "paymasterPostOpGasLimit": hex(50_000),
        }
        h = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        assert len(h) == 32


class TestSignature:
    def test_sign_userop_hash(self):
        """Verify that signing a UserOp hash via Rust core produces a valid 65-byte signature."""
        import totalreclaw_core

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        eoa_json = totalreclaw_core.derive_eoa(mnemonic)
        import json
        eoa = json.loads(eoa_json)
        private_key = bytes.fromhex(eoa["private_key"])

        user_op = {
            "sender": "0x2c0cf74b2b76110708ca431796367779e3738250",
            "nonce": "0x0",
            "callData": "0xdeadbeef",
            "callGasLimit": hex(500_000),
            "verificationGasLimit": hex(500_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(2_000_000_000),
            "maxPriorityFeePerGas": hex(1_500_000_000),
        }
        user_op_hash = compute_user_op_hash(user_op, ENTRYPOINT_V07, 84532)
        sig_hex = sign_user_op_hash(user_op_hash, private_key)

        # Should be 0x-prefixed, 65 bytes (130 hex chars)
        assert sig_hex.startswith("0x")
        sig_bytes = bytes.fromhex(sig_hex[2:])
        assert len(sig_bytes) == 65
        # v should be 27 or 28
        assert sig_bytes[-1] in (27, 28)
