"""Tests for ERC-4337 UserOperation construction."""
import pytest
from eth_hash.auto import keccak

from totalreclaw.userop import (
    ENTRYPOINT_V07,
    SIMPLE_ACCOUNT_FACTORY,
    DATA_EDGE_ADDRESS,
    EXECUTE_SELECTOR,
    CREATE_ACCOUNT_SELECTOR,
    GET_NONCE_SELECTOR,
    encode_execute_calldata,
    encode_factory_data,
    compute_user_op_hash,
    _pad32,
    _encode_uint256,
    _encode_bytes_dynamic,
)


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

    def test_encode_bytes_dynamic(self):
        data = b"\x01\x02\x03"
        result = _encode_bytes_dynamic(data)
        # Should be: length (32 bytes) + data padded to 32 bytes
        assert result.startswith(_encode_uint256(3))
        # Data portion: 010203 padded to 64 hex chars
        data_hex = result[64:]
        assert data_hex.startswith("010203")
        assert len(data_hex) == 64  # padded to 32 bytes

    def test_encode_bytes_dynamic_empty(self):
        result = _encode_bytes_dynamic(b"")
        assert result == _encode_uint256(0)


class TestFunctionSelectors:
    def test_execute_selector(self):
        expected = keccak(b"execute(address,uint256,bytes)")[:4].hex()
        assert EXECUTE_SELECTOR == expected

    def test_create_account_selector(self):
        expected = keccak(b"createAccount(address,uint256)")[:4].hex()
        assert CREATE_ACCOUNT_SELECTOR == expected

    def test_get_nonce_selector(self):
        expected = keccak(b"getNonce(address,uint192)")[:4].hex()
        assert GET_NONCE_SELECTOR == expected


class TestEncodeExecuteCalldata:
    def test_basic_encoding(self):
        target = "0x1234567890abcdef1234567890abcdef12345678"
        data = b"\xde\xad\xbe\xef"
        result = encode_execute_calldata(target, 0, data)

        # Should start with 0x + execute selector
        assert result.startswith(f"0x{EXECUTE_SELECTOR}")
        # Should contain the target address (padded)
        assert _pad32(target) in result

    def test_empty_data(self):
        target = DATA_EDGE_ADDRESS
        result = encode_execute_calldata(target, 0, b"")
        assert result.startswith(f"0x{EXECUTE_SELECTOR}")

    def test_protobuf_payload(self):
        """Test with a realistic protobuf payload size."""
        target = DATA_EDGE_ADDRESS
        payload = b"\x0a\x24" + b"a" * 36  # ~38 bytes
        result = encode_execute_calldata(target, 0, payload)
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
        """Verify that signing a UserOp hash produces a valid 65-byte signature."""
        from eth_account import Account

        Account.enable_unaudited_hdwallet_features()
        acct = Account.from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
            account_path="m/44'/60'/0'/0/0",
        )

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
        signed = Account.unsafe_sign_hash(user_op_hash, acct.key)

        sig_hex = signed.signature.hex()
        # Should be 65 bytes (130 hex chars) -- r (32) + s (32) + v (1)
        assert len(bytes.fromhex(sig_hex)) == 65
        assert signed.v in (27, 28)
