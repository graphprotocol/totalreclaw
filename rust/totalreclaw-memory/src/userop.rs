//! Native ERC-4337 UserOperation construction and submission.
//!
//! Replaces the TypeScript bridge with pure Rust:
//! - ABI encoding for SimpleAccount execute/executeBatch
//! - v0.7 UserOp struct construction (nonce, gas, calldata)
//! - EIP-191 signing over UserOp hash
//! - JSON-RPC submission to relay bundler proxy
//!
//! Reference: mcp/src/subgraph/store.ts (TypeScript implementation)

use alloy_primitives::{Address, Bytes, U256};
use alloy_sol_types::{sol, SolCall};

use crate::{Error, Result};

/// DataEdge contract address (same on all chains).
pub const DATA_EDGE_ADDRESS: &str = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca";

/// EntryPoint v0.7 address.
pub const ENTRYPOINT_ADDRESS: &str = "0x0000000071727De22E5E9d8BAf0edAc6f37da032";

/// Max batch size (matches extraction cap).
pub const MAX_BATCH_SIZE: usize = 15;

// ABI definitions using alloy sol! macro
sol! {
    /// SimpleAccount.execute(address dest, uint256 value, bytes calldata)
    function execute(address dest, uint256 value, bytes data);

    /// SimpleAccount.executeBatch(address[] dest, uint256[] values, bytes[] data)
    function executeBatch(address[] dest, uint256[] values, bytes[] data);
}

/// Encode a single fact submission as SimpleAccount.execute() calldata.
///
/// The DataEdge contract has a fallback() that emits Log(bytes),
/// so the inner calldata IS the raw protobuf payload.
pub fn encode_single_call(protobuf_payload: &[u8]) -> Vec<u8> {
    let dest: Address = DATA_EDGE_ADDRESS.parse().unwrap();
    let value = U256::ZERO;
    let data = Bytes::copy_from_slice(protobuf_payload);

    let call = executeCall { dest, value, data };
    call.abi_encode()
}

/// Encode multiple fact submissions as SimpleAccount.executeBatch() calldata.
///
/// Each protobuf payload becomes one call to DataEdge's fallback().
/// All calls have value=0.
pub fn encode_batch_call(protobuf_payloads: &[Vec<u8>]) -> Result<Vec<u8>> {
    if protobuf_payloads.is_empty() {
        return Err(Error::Crypto("Batch must contain at least 1 payload".into()));
    }
    if protobuf_payloads.len() > MAX_BATCH_SIZE {
        return Err(Error::Crypto(format!(
            "Batch size {} exceeds maximum of {}",
            protobuf_payloads.len(),
            MAX_BATCH_SIZE
        )));
    }

    // Single payload -> use execute() (no batch overhead)
    if protobuf_payloads.len() == 1 {
        return Ok(encode_single_call(&protobuf_payloads[0]));
    }

    let dest: Address = DATA_EDGE_ADDRESS.parse().unwrap();
    let dests: Vec<Address> = vec![dest; protobuf_payloads.len()];
    let values: Vec<U256> = vec![U256::ZERO; protobuf_payloads.len()];
    let datas: Vec<Bytes> = protobuf_payloads
        .iter()
        .map(|p| Bytes::copy_from_slice(p))
        .collect();

    let call = executeBatchCall {
        dest: dests,
        values,
        data: datas,
    };
    Ok(call.abi_encode())
}

// ---------------------------------------------------------------------------
// ERC-4337 v0.7 UserOp struct + signing + submission
// ---------------------------------------------------------------------------

/// ERC-4337 v0.7 UserOperation.
///
/// Field names match the Pimlico bundler v0.7 API exactly.
/// Optional fields are serialized as null when absent.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UserOperationV7 {
    pub sender: String,
    pub nonce: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub factory: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub factory_data: Option<String>,
    pub call_data: String,
    pub call_gas_limit: String,
    pub verification_gas_limit: String,
    pub pre_verification_gas: String,
    pub max_fee_per_gas: String,
    pub max_priority_fee_per_gas: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub paymaster: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub paymaster_verification_gas_limit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub paymaster_post_op_gas_limit: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub paymaster_data: Option<String>,
    pub signature: String,
}

/// Result of a UserOp submission.
#[derive(Debug)]
pub struct SubmitResult {
    pub tx_hash: String,
    pub user_op_hash: String,
    pub success: bool,
}

/// SimpleAccountFactory address (v0.7, same on all EVM chains).
const SIMPLE_ACCOUNT_FACTORY: &str = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985";

/// Submit a UserOp to the relay bundler endpoint.
///
/// Full flow (matches viem/permissionless ordering):
/// 1. Get gas prices from bundler
/// 2. Get nonce from EntryPoint
/// 3. Check if Smart Account is deployed; if not, include factory initCode
/// 4. Build unsigned v0.7 UserOp (with gas prices already set)
/// 5. Get paymaster sponsorship (gas estimates + paymaster data)
/// 6. Sign the UserOp
/// 7. Submit via eth_sendUserOperation
/// 8. Wait for receipt
pub async fn submit_userop(
    calldata: &[u8],
    sender: &str,
    private_key: &[u8; 32],
    relay_url: &str,
    auth_key_hex: &str,
    chain_id: u64,
    is_test: bool,
) -> Result<SubmitResult> {
    let bundler_url = format!("{}/v1/bundler", relay_url.trim_end_matches('/'));
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(|e| Error::Http(e.to_string()))?;
    let calldata_hex = format!("0x{}", hex::encode(calldata));

    let headers = build_headers(auth_key_hex, sender, is_test);

    // 1. Get gas prices FIRST (matches viem/permissionless flow)
    let gas_price_resp = jsonrpc_call(
        &client,
        &bundler_url,
        "pimlico_getUserOperationGasPrice",
        serde_json::json!([]),
        &headers,
    )
    .await?;

    let mut max_fee = "0x0".to_string();
    let mut max_priority_fee = "0x0".to_string();
    if let Some(fast) = gas_price_resp.get("result").and_then(|r| r.get("fast")) {
        if let Some(v) = fast.get("maxFeePerGas") {
            max_fee = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = fast.get("maxPriorityFeePerGas") {
            max_priority_fee = v.as_str().unwrap_or("0x0").to_string();
        }
    }

    // 2. Get nonce: eth_call to EntryPoint.getNonce(sender, 0)
    let nonce_hex = get_nonce(&client, sender, chain_id).await?;

    // 3. Check if Smart Account is deployed; include factory if not
    let deployed = is_account_deployed(&client, sender, chain_id).await?;
    let (factory, factory_data) = if deployed {
        (None, None)
    } else {
        // Factory: SimpleAccountFactory.createAccount(owner, salt)
        // We need the EOA address (owner). Derive it from the private key.
        let signing_key = k256::ecdsa::SigningKey::from_bytes(private_key.into())
            .map_err(|e| Error::Crypto(format!("Invalid signing key: {}", e)))?;
        let verifying_key = signing_key.verifying_key();
        let public_key = verifying_key.to_encoded_point(false);
        let pubkey_raw = &public_key.as_bytes()[1..];
        let eoa_hash = keccak256_hash(pubkey_raw);
        let eoa_addr = format!("0x{}", hex::encode(&eoa_hash[12..]));

        // ABI-encode: createAccount(address owner, uint256 salt)
        // selector: keccak256("createAccount(address,uint256)")[:4] = 0x5fbfb9cf
        let owner_padded = format!("{:0>64}", eoa_addr.trim_start_matches("0x").to_lowercase());
        let salt_padded = "0".repeat(64); // salt = 0
        let factory_data_hex = format!("0x5fbfb9cf{}{}", owner_padded, salt_padded);
        (
            Some(SIMPLE_ACCOUNT_FACTORY.to_string()),
            Some(factory_data_hex),
        )
    };

    // 4. Build unsigned v0.7 UserOp with gas prices already set
    //    Use the same stub signature as viem/permissionless for gas estimation.
    let mut userop = UserOperationV7 {
        sender: sender.to_string(),
        nonce: nonce_hex,
        factory,
        factory_data,
        call_data: calldata_hex,
        call_gas_limit: "0x0".to_string(),
        verification_gas_limit: "0x0".to_string(),
        pre_verification_gas: "0x0".to_string(),
        max_fee_per_gas: max_fee,
        max_priority_fee_per_gas: max_priority_fee,
        paymaster: None,
        paymaster_verification_gas_limit: None,
        paymaster_post_op_gas_limit: None,
        paymaster_data: None,
        // Stub signature matching viem/permissionless SimpleAccount default.
        // Uses max r and s-curve values that won't revert ecrecover.
        signature: "0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c".to_string(),
    };

    // 5. Get paymaster sponsorship via pm_sponsorUserOperation
    let sponsor_resp = jsonrpc_call(
        &client,
        &bundler_url,
        "pm_sponsorUserOperation",
        serde_json::json!([userop, ENTRYPOINT_ADDRESS]),
        &headers,
    )
    .await?;

    // Apply paymaster response (v0.7 format)
    if let Some(result) = sponsor_resp.get("result") {
        if let Some(v) = result.get("callGasLimit") {
            userop.call_gas_limit = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("verificationGasLimit") {
            userop.verification_gas_limit = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("preVerificationGas") {
            userop.pre_verification_gas = v.as_str().unwrap_or("0x0").to_string();
        }
        // Only update gas prices if sponsor explicitly provides them
        if let Some(v) = result.get("maxFeePerGas") {
            if let Some(s) = v.as_str() {
                userop.max_fee_per_gas = s.to_string();
            }
        }
        if let Some(v) = result.get("maxPriorityFeePerGas") {
            if let Some(s) = v.as_str() {
                userop.max_priority_fee_per_gas = s.to_string();
            }
        }
        // v0.7 paymaster fields
        if let Some(v) = result.get("paymaster") {
            userop.paymaster = v.as_str().map(|s| s.to_string());
        }
        if let Some(v) = result.get("paymasterVerificationGasLimit") {
            userop.paymaster_verification_gas_limit = v.as_str().map(|s| s.to_string());
        }
        if let Some(v) = result.get("paymasterPostOpGasLimit") {
            userop.paymaster_post_op_gas_limit = v.as_str().map(|s| s.to_string());
        }
        if let Some(v) = result.get("paymasterData") {
            userop.paymaster_data = v.as_str().map(|s| s.to_string());
        }
    } else {
        let err_msg = sponsor_resp
            .get("error")
            .map(|e| format!("{}", e))
            .unwrap_or_else(|| format!("{:?}", sponsor_resp));
        return Err(Error::Http(format!(
            "Paymaster sponsorship failed: {}",
            err_msg
        )));
    }

    // 6. Sign the UserOp
    let userop_hash = compute_userop_hash_v7(&userop, ENTRYPOINT_ADDRESS, chain_id)?;
    let signature = sign_userop_hash(&userop_hash, private_key)?;
    userop.signature = format!("0x{}", hex::encode(&signature));

    // 7. Submit via eth_sendUserOperation
    let send_resp = jsonrpc_call(
        &client,
        &bundler_url,
        "eth_sendUserOperation",
        serde_json::json!([userop, ENTRYPOINT_ADDRESS]),
        &headers,
    )
    .await?;

    let op_hash = send_resp["result"]
        .as_str()
        .ok_or_else(|| {
            let err_msg = send_resp
                .get("error")
                .map(|e| format!("{}", e))
                .unwrap_or_else(|| format!("{:?}", send_resp));
            Error::Http(format!("No userOpHash in response: {}", err_msg))
        })?
        .to_string();

    // 6. Poll for receipt
    let receipt = poll_receipt(&client, &bundler_url, &op_hash, &headers).await?;

    Ok(SubmitResult {
        tx_hash: receipt["receipt"]["transactionHash"]
            .as_str()
            .unwrap_or("")
            .to_string(),
        user_op_hash: op_hash,
        success: receipt["success"].as_bool().unwrap_or(false),
    })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn build_headers(auth_key_hex: &str, wallet: &str, is_test: bool) -> reqwest::header::HeaderMap {
    let mut h = reqwest::header::HeaderMap::new();
    h.insert("X-TotalReclaw-Client", "zeroclaw-memory".parse().unwrap());
    h.insert(
        "Authorization",
        format!("Bearer {}", auth_key_hex).parse().unwrap(),
    );
    h.insert("X-Wallet-Address", wallet.parse().unwrap());
    if is_test {
        h.insert("X-TotalReclaw-Test", "true".parse().unwrap());
    }
    h
}

async fn jsonrpc_call(
    client: &reqwest::Client,
    url: &str,
    method: &str,
    params: serde_json::Value,
    headers: &reqwest::header::HeaderMap,
) -> Result<serde_json::Value> {
    let resp = client
        .post(url)
        .headers(headers.clone())
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    resp.json().await.map_err(|e| Error::Http(e.to_string()))
}

/// Check if a Smart Account is deployed by checking its code size.
async fn is_account_deployed(client: &reqwest::Client, address: &str, chain_id: u64) -> Result<bool> {
    let rpc_url = match chain_id {
        84532 => "https://sepolia.base.org",
        100 => "https://rpc.gnosischain.com",
        _ => "https://sepolia.base.org",
    };

    let resp = client
        .post(rpc_url)
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [address, "latest"],
            "id": 1,
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let code = body["result"].as_str().unwrap_or("0x");
    // Account is deployed if it has code (more than just "0x")
    Ok(code.len() > 2)
}

/// Get the nonce for a sender from the EntryPoint.
/// Returns a hex string (e.g. "0x0", "0x1a") that can be used directly
/// in the UserOp. Handles uint256 nonces (not just u64).
async fn get_nonce(client: &reqwest::Client, sender: &str, chain_id: u64) -> Result<String> {
    let rpc_url = match chain_id {
        84532 => "https://sepolia.base.org",
        100 => "https://rpc.gnosischain.com",
        _ => "https://sepolia.base.org",
    };

    // EntryPoint.getNonce(address sender, uint192 key) -> selector 0x35567e1a
    let sender_padded = format!(
        "{:0>64}",
        sender.trim_start_matches("0x").to_lowercase()
    );
    let key_padded = "0".repeat(64);
    let calldata = format!("0x35567e1a{}{}", sender_padded, key_padded);

    let resp = client
        .post(rpc_url)
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": ENTRYPOINT_ADDRESS, "data": calldata}, "latest"],
            "id": 1,
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let result = body["result"].as_str().unwrap_or("0x0");
    // Strip leading zeros but keep at least one digit after 0x
    let trimmed = result.trim_start_matches("0x").trim_start_matches('0');
    if trimmed.is_empty() {
        Ok("0x0".to_string())
    } else {
        Ok(format!("0x{}", trimmed))
    }
}

/// Compute the UserOp hash for signing (ERC-4337 v0.7 format).
///
/// v0.7 packing:
///   hashStruct = keccak256(abi.encode(
///     sender, nonce, keccak256(initCode), keccak256(callData),
///     accountGasLimits, preVerificationGas, gasFees,
///     keccak256(paymasterAndData)
///   ))
///   hash = keccak256(abi.encode(hashStruct, entryPoint, chainId))
///
/// Where:
///   initCode = factory ? (factory + factoryData) : bytes(0)
///   accountGasLimits = bytes32(verificationGasLimit << 128 | callGasLimit)
///   gasFees = bytes32(maxPriorityFeePerGas << 128 | maxFeePerGas)
///   paymasterAndData = paymaster ? (paymaster + pmVerificationGasLimit(16) + pmPostOpGasLimit(16) + paymasterData) : bytes(0)
fn compute_userop_hash_v7(
    userop: &UserOperationV7,
    entrypoint: &str,
    chain_id: u64,
) -> Result<[u8; 32]> {
    let decode_hex = |s: &str| -> Vec<u8> {
        hex::decode(s.trim_start_matches("0x")).unwrap_or_default()
    };

    // Build initCode: factory(20) + factoryData
    let init_code = if let Some(ref factory) = userop.factory {
        let factory_bytes = decode_hex(factory);
        let factory_data = userop
            .factory_data
            .as_deref()
            .map(|s| decode_hex(s))
            .unwrap_or_default();
        let mut ic = factory_bytes;
        ic.extend_from_slice(&factory_data);
        ic
    } else {
        vec![]
    };

    // Build accountGasLimits: bytes32(verificationGasLimit(16) || callGasLimit(16))
    let vgl = parse_hex_u128(&userop.verification_gas_limit);
    let cgl = parse_hex_u128(&userop.call_gas_limit);
    let mut account_gas_limits = [0u8; 32];
    account_gas_limits[..16].copy_from_slice(&vgl.to_be_bytes());
    account_gas_limits[16..].copy_from_slice(&cgl.to_be_bytes());

    // preVerificationGas as uint256
    let pvg = parse_hex_u128(&userop.pre_verification_gas);

    // Build gasFees: bytes32(maxPriorityFeePerGas(16) || maxFeePerGas(16))
    let mpfpg = parse_hex_u128(&userop.max_priority_fee_per_gas);
    let mfpg = parse_hex_u128(&userop.max_fee_per_gas);
    let mut gas_fees = [0u8; 32];
    gas_fees[..16].copy_from_slice(&mpfpg.to_be_bytes());
    gas_fees[16..].copy_from_slice(&mfpg.to_be_bytes());

    // Build paymasterAndData: paymaster(20) + pmVerificationGasLimit(16) + pmPostOpGasLimit(16) + paymasterData
    let paymaster_and_data = if let Some(ref pm) = userop.paymaster {
        let pm_bytes = decode_hex(pm);
        let pm_vgl = parse_hex_u128(
            userop
                .paymaster_verification_gas_limit
                .as_deref()
                .unwrap_or("0x0"),
        );
        let pm_pgl = parse_hex_u128(
            userop
                .paymaster_post_op_gas_limit
                .as_deref()
                .unwrap_or("0x0"),
        );
        let pm_data = userop
            .paymaster_data
            .as_deref()
            .map(|s| decode_hex(s))
            .unwrap_or_default();

        let mut pad = pm_bytes;
        pad.extend_from_slice(&pm_vgl.to_be_bytes());
        pad.extend_from_slice(&pm_pgl.to_be_bytes());
        pad.extend_from_slice(&pm_data);
        pad
    } else {
        vec![]
    };

    // Pack: sender + nonce + keccak256(initCode) + keccak256(callData) +
    //        accountGasLimits + preVerificationGas + gasFees + keccak256(paymasterAndData)
    let mut packed = Vec::new();
    // sender (address, 32 bytes padded)
    packed.extend_from_slice(&[0u8; 12]);
    packed.extend_from_slice(&decode_hex(&userop.sender));
    // nonce (uint256 -- can exceed u64, e.g. with non-zero nonce keys)
    let nonce_hex_str = userop.nonce.trim_start_matches("0x");
    // Pad to even length for hex decoding
    let nonce_padded = if nonce_hex_str.len() % 2 != 0 {
        format!("0{}", nonce_hex_str)
    } else {
        nonce_hex_str.to_string()
    };
    let nonce_raw = hex::decode(&nonce_padded).unwrap_or_default();
    let mut nonce_bytes = [0u8; 32];
    if !nonce_raw.is_empty() && nonce_raw.len() <= 32 {
        nonce_bytes[32 - nonce_raw.len()..].copy_from_slice(&nonce_raw);
    }
    packed.extend_from_slice(&nonce_bytes);
    // keccak256(initCode)
    packed.extend_from_slice(&keccak256_hash(&init_code));
    // keccak256(callData)
    packed.extend_from_slice(&keccak256_hash(&decode_hex(&userop.call_data)));
    // accountGasLimits (bytes32)
    packed.extend_from_slice(&account_gas_limits);
    // preVerificationGas (uint256)
    let mut pvg_bytes = [0u8; 32];
    pvg_bytes[16..].copy_from_slice(&pvg.to_be_bytes());
    packed.extend_from_slice(&pvg_bytes);
    // gasFees (bytes32)
    packed.extend_from_slice(&gas_fees);
    // keccak256(paymasterAndData)
    packed.extend_from_slice(&keccak256_hash(&paymaster_and_data));

    let inner_hash = keccak256_hash(&packed);

    // abi.encode(innerHash, entryPoint, chainId)
    let mut outer = Vec::with_capacity(96);
    outer.extend_from_slice(&inner_hash);
    let ep_bytes = hex::decode(entrypoint.trim_start_matches("0x"))
        .map_err(|e| Error::Crypto(e.to_string()))?;
    outer.extend_from_slice(&[0u8; 12]);
    outer.extend_from_slice(&ep_bytes);
    let mut chain_bytes = [0u8; 32];
    chain_bytes[24..].copy_from_slice(&chain_id.to_be_bytes());
    outer.extend_from_slice(&chain_bytes);

    Ok(keccak256_hash(&outer))
}

fn parse_hex_u128(hex_str: &str) -> u128 {
    u128::from_str_radix(hex_str.trim_start_matches("0x"), 16).unwrap_or(0)
}

fn keccak256_hash(data: &[u8]) -> [u8; 32] {
    use tiny_keccak::{Hasher, Keccak};
    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(data);
    keccak.finalize(&mut hash);
    hash
}

/// Sign a UserOp hash with a private key.
///
/// The SimpleAccount's _validateSignature applies toEthSignedMessageHash
/// internally, so we sign the RAW hash and let the contract handle the wrapping.
///
/// Wait -- no. The SimpleAccount does:
///   hash = toEthSignedMessageHash(userOpHash)
///   ECDSA.recover(hash, signature)
///
/// This means the signer must sign the EIP-191 prefixed hash,
/// because ecrecover is called on the EIP-191 hash.
///
/// So: we keccak256("\x19Ethereum Signed Message:\n32" + userOpHash)
///     then sign_prehash_recoverable on THAT.
fn sign_userop_hash(hash: &[u8; 32], private_key: &[u8; 32]) -> Result<Vec<u8>> {
    use k256::ecdsa::SigningKey;

    let signing_key = SigningKey::from_bytes(private_key.into())
        .map_err(|e| Error::Crypto(format!("Invalid signing key: {}", e)))?;

    // EIP-191: "\x19Ethereum Signed Message:\n32" + hash
    let mut prefixed = Vec::with_capacity(60);
    prefixed.extend_from_slice(b"\x19Ethereum Signed Message:\n32");
    prefixed.extend_from_slice(hash);
    let msg_hash = keccak256_hash(&prefixed);

    let (sig, recovery_id) = signing_key
        .sign_prehash_recoverable(&msg_hash)
        .map_err(|e| Error::Crypto(format!("Signing failed: {}", e)))?;

    // Encode as r (32) + s (32) + v (1)
    let mut signature = Vec::with_capacity(65);
    signature.extend_from_slice(&sig.to_bytes());
    signature.push(recovery_id.to_byte() + 27); // v = 27 or 28

    Ok(signature)
}

async fn poll_receipt(
    client: &reqwest::Client,
    bundler_url: &str,
    op_hash: &str,
    headers: &reqwest::header::HeaderMap,
) -> Result<serde_json::Value> {
    for _ in 0..60 {
        let resp = jsonrpc_call(
            client,
            bundler_url,
            "eth_getUserOperationReceipt",
            serde_json::json!([op_hash]),
            headers,
        )
        .await?;

        if resp.get("result").and_then(|r| r.as_object()).is_some() {
            return Ok(resp["result"].clone());
        }

        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    Err(Error::Http("UserOp receipt timeout after 120s".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_single_call() {
        let payload = b"test protobuf data";
        let encoded = encode_single_call(payload);
        // Should start with execute() selector 0xb61d27f6
        assert_eq!(&encoded[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
        assert!(encoded.len() > 100); // ABI encoding adds padding
    }

    #[test]
    fn test_encode_batch_call() {
        let payloads = vec![
            b"fact one".to_vec(),
            b"fact two".to_vec(),
            b"fact three".to_vec(),
        ];
        let encoded = encode_batch_call(&payloads).unwrap();
        // Should start with executeBatch(address[],uint256[],bytes[]) selector 0x47e1da2a
        assert_eq!(&encoded[..4], &[0x47, 0xe1, 0xda, 0x2a]);
    }

    #[test]
    fn test_single_payload_uses_execute() {
        let payloads = vec![b"single fact".to_vec()];
        let encoded = encode_batch_call(&payloads).unwrap();
        // Single payload should use execute(), not executeBatch()
        assert_eq!(&encoded[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
    }

    #[test]
    fn test_empty_batch_rejected() {
        let result = encode_batch_call(&[]);
        assert!(result.is_err());
    }

    #[test]
    fn test_oversized_batch_rejected() {
        let payloads: Vec<Vec<u8>> = (0..16).map(|i| vec![i as u8]).collect();
        let result = encode_batch_call(&payloads);
        assert!(result.is_err());
    }

    #[test]
    fn test_userop_hash_v7_parity() {
        // Same UserOp as the viem reference test
        let userop = UserOperationV7 {
            sender: "0x949bc374325a4f41e46e8e78a07d910332934542".to_string(),
            nonce: "0x0".to_string(),
            factory: Some("0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985".to_string()),
            factory_data: Some("0x5fbfb9cf0000000000000000000000008eb626f727e92a73435f2b85dd6fd0c6da5dbb720000000000000000000000000000000000000000000000000000000000000000".to_string()),
            call_data: "0xb61d27f6".to_string(),
            call_gas_limit: "0x186a0".to_string(),     // 100000
            verification_gas_limit: "0x30d40".to_string(), // 200000
            pre_verification_gas: "0xc350".to_string(),    // 50000
            max_fee_per_gas: "0xf4240".to_string(),        // 1000000
            max_priority_fee_per_gas: "0x7a120".to_string(), // 500000
            paymaster: Some("0x0000000000000039cd5e8ae05257ce51c473ddd1".to_string()),
            paymaster_verification_gas_limit: Some("0x186a0".to_string()), // 100000
            paymaster_post_op_gas_limit: Some("0xc350".to_string()),       // 50000
            paymaster_data: Some("0xabcd".to_string()),
            signature: format!("0x{}", "00".repeat(65)),
        };

        let hash = compute_userop_hash_v7(
            &userop,
            "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            84532,
        )
        .unwrap();

        assert_eq!(
            format!("0x{}", hex::encode(hash)),
            "0x4525d2a8a555a1a56f6313735b83fe3ee55f81d504d905ea85613524973f97c2",
            "v0.7 UserOp hash must match viem's getUserOperationHash"
        );
    }

    #[test]
    fn test_signing_parity() {
        // Use the same test key as viem test above (Hardhat account #0)
        let private_key_hex = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
        let mut private_key = [0u8; 32];
        private_key.copy_from_slice(&hex::decode(private_key_hex).unwrap());

        let hash_hex = "1b25552f7901991cd4e2793945f694a09c9d0b9454a86cee16123ac9e84bd2de";
        let mut hash = [0u8; 32];
        hash.copy_from_slice(&hex::decode(hash_hex).unwrap());

        let sig = sign_userop_hash(&hash, &private_key).unwrap();
        let sig_hex = hex::encode(&sig);

        // viem signature for this key + hash
        assert_eq!(
            sig_hex,
            "24b6fabd386f1580aa1fc09b04dd274ea334a9bf63e4fc994e0bef9a505f618335cb2b7d20454a0526f5c66f52ed73b9e76e9696ab5959998e7fc3984fba91691c",
            "Signature must match viem's signMessage({{message: {{raw: hash}}}})"
        );
    }

    #[test]
    fn test_signing_parity_abandon_mnemonic() {
        // Private key for the "abandon...about" mnemonic
        let private_key_hex = "1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727";
        let mut private_key = [0u8; 32];
        private_key.copy_from_slice(&hex::decode(private_key_hex).unwrap());

        // Real UserOp hash from the E2E test
        let hash_hex = "6de60c2ca586227294ffce39e30a3c6ec8ddf6ae01d0d579344e8d2e2dbf8b26";
        let mut hash = [0u8; 32];
        hash.copy_from_slice(&hex::decode(hash_hex).unwrap());

        let sig = sign_userop_hash(&hash, &private_key).unwrap();
        let sig_hex = hex::encode(&sig);

        // viem signature: a5ad7388dd018236a6cfc25556f35d0d05fff7a9a59ef29fef65b1855298f767107418521a5ca48e56a4d5de67e954df5d6dd49fe98eba3d1c45ad22eeae3fd11c
        assert_eq!(
            sig_hex,
            "a5ad7388dd018236a6cfc25556f35d0d05fff7a9a59ef29fef65b1855298f767107418521a5ca48e56a4d5de67e954df5d6dd49fe98eba3d1c45ad22eeae3fd11c",
            "Signature must match viem's signMessage for abandon mnemonic"
        );
    }

    #[test]
    fn test_keccak256_hash() {
        let hash = keccak256_hash(b"hello");
        assert_eq!(
            hex::encode(hash),
            "1c8aff950685c2ed4bc3174f3472287b56d9517b9c948127319a09a7a36deac8"
        );
    }

    #[test]
    fn test_userop_hash_v7_real_paymaster() {
        // Real UserOp from a successful viem/permissionless submission
        // against staging (Base Sepolia, chain 84532).
        // This tests with real paymaster data (longer than the synthetic test above).
        let userop = UserOperationV7 {
            sender: "0x695241674733a452a5373b16baf2dc2d9435be8e".to_string(),
            nonce: "0x0".to_string(),
            factory: Some("0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985".to_string()),
            factory_data: Some("0x5fbfb9cf000000000000000000000000cd894ed607b25d52e9ac776cf48e9407d3a263d30000000000000000000000000000000000000000000000000000000000000000".to_string()),
            call_data: "0xb61d27f6000000000000000000000000c445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000000000000000000004deadbeef00000000000000000000000000000000000000000000000000000000".to_string(),
            call_gas_limit: "0x4623".to_string(),
            verification_gas_limit: "0x41bab".to_string(),
            pre_verification_gas: "0xc9c9".to_string(),
            max_fee_per_gas: "0x757e20".to_string(),
            max_priority_fee_per_gas: "0x10c8e0".to_string(),
            paymaster: Some("0x777777777777AeC03fd955926DbF81597e66834C".to_string()),
            paymaster_verification_gas_limit: Some("0x8a8e".to_string()),
            paymaster_post_op_gas_limit: Some("0x1".to_string()),
            paymaster_data: Some("0x01000069cb37390000000000006568f8cf98f823f68c4fedbde90b241f30e9323b436eeb3cddeb688e0859b23565005402808774472237b1808f0006721bd065729a72a84710fdceaa465737f61c".to_string()),
            signature: format!("0x{}", "00".repeat(65)),
        };

        let hash = compute_userop_hash_v7(
            &userop,
            "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            84532,
        )
        .unwrap();

        // Verified against viem's getUserOperationHash with identical inputs.
        assert_eq!(
            format!("0x{}", hex::encode(hash)),
            "0x3d4467d9a3c070eea659ef9a7cf42f1b4e87e14cd91792d869faf031b6fea3e8",
            "v0.7 hash with real paymaster data must match viem"
        );
    }

    #[test]
    fn test_nonce_uint256_parsing() {
        // Test that large nonces (used by viem with non-zero nonce keys) parse correctly
        let userop = UserOperationV7 {
            sender: "0x949bc374325a4f41e46e8e78a07d910332934542".to_string(),
            nonce: "0x19d41c68d5e0000000000000000".to_string(), // Large nonce key
            factory: None,
            factory_data: None,
            call_data: "0xb61d27f6".to_string(),
            call_gas_limit: "0x186a0".to_string(),
            verification_gas_limit: "0x30d40".to_string(),
            pre_verification_gas: "0xc350".to_string(),
            max_fee_per_gas: "0xf4240".to_string(),
            max_priority_fee_per_gas: "0x7a120".to_string(),
            paymaster: None,
            paymaster_verification_gas_limit: None,
            paymaster_post_op_gas_limit: None,
            paymaster_data: None,
            signature: format!("0x{}", "00".repeat(65)),
        };

        // Should not panic or produce zero hash
        let hash = compute_userop_hash_v7(
            &userop,
            "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            84532,
        )
        .unwrap();
        assert_ne!(hex::encode(hash), "0".repeat(64));
    }
}
