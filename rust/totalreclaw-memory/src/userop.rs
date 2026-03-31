//! Native ERC-4337 UserOperation construction and submission.
//!
//! Replaces the TypeScript bridge with pure Rust:
//! - ABI encoding for SimpleAccount execute/executeBatch
//! - UserOp struct construction (nonce, gas, calldata)
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
// UserOp struct + signing + submission (Task 5)
// ---------------------------------------------------------------------------

/// ERC-4337 UserOperation (v0.7 packed format).
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UserOperation {
    pub sender: String,
    pub nonce: String,
    pub init_code: String,
    pub call_data: String,
    pub call_gas_limit: String,
    pub verification_gas_limit: String,
    pub pre_verification_gas: String,
    pub max_fee_per_gas: String,
    pub max_priority_fee_per_gas: String,
    pub paymaster_and_data: String,
    pub signature: String,
}

/// Result of a UserOp submission.
#[derive(Debug)]
pub struct SubmitResult {
    pub tx_hash: String,
    pub user_op_hash: String,
    pub success: bool,
}

/// Submit a UserOp to the relay bundler endpoint.
///
/// Full flow:
/// 1. Get nonce from EntryPoint
/// 2. Build unsigned UserOp
/// 3. Get paymaster sponsorship (gas estimates + paymaster data)
/// 4. Sign the UserOp
/// 5. Submit via eth_sendUserOperation
/// 6. Wait for receipt
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

    // 1. Get nonce: eth_call to EntryPoint.getNonce(sender, 0)
    let nonce = get_nonce(&client, sender, chain_id).await?;
    let nonce_hex = format!("0x{:x}", nonce);

    // 2. Build unsigned UserOp (gas fields will be filled by paymaster)
    let mut userop = UserOperation {
        sender: sender.to_string(),
        nonce: nonce_hex,
        init_code: "0x".to_string(),
        call_data: calldata_hex,
        call_gas_limit: "0x0".to_string(),
        verification_gas_limit: "0x0".to_string(),
        pre_verification_gas: "0x0".to_string(),
        max_fee_per_gas: "0x0".to_string(),
        max_priority_fee_per_gas: "0x0".to_string(),
        paymaster_and_data: "0x".to_string(),
        signature: "0x".to_string(),
    };

    // 3. Get paymaster sponsorship via pm_sponsorUserOperation
    let sponsor_resp = jsonrpc_call(
        &client,
        &bundler_url,
        "pm_sponsorUserOperation",
        serde_json::json!([userop, ENTRYPOINT_ADDRESS, {"sponsorshipPolicyId": "sp_cheerful_cousin"}]),
        &headers,
    )
    .await?;

    // Apply paymaster response (handle both v0.6 separate fields and v0.7 packed fields)
    if let Some(result) = sponsor_resp.get("result") {
        // v0.6 separate fields
        if let Some(v) = result.get("callGasLimit") {
            userop.call_gas_limit = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("verificationGasLimit") {
            userop.verification_gas_limit = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("preVerificationGas") {
            userop.pre_verification_gas = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("maxFeePerGas") {
            userop.max_fee_per_gas = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("maxPriorityFeePerGas") {
            userop.max_priority_fee_per_gas = v.as_str().unwrap_or("0x0").to_string();
        }
        if let Some(v) = result.get("paymasterAndData") {
            userop.paymaster_and_data = v.as_str().unwrap_or("0x").to_string();
        }

        // v0.7 packed fields (Pimlico may return these instead)
        if let Some(v) = result.get("accountGasLimits") {
            // bytes32 = verificationGasLimit (16 bytes) || callGasLimit (16 bytes)
            let packed = v.as_str().unwrap_or("0x");
            let packed = packed.trim_start_matches("0x");
            if packed.len() == 64 {
                userop.verification_gas_limit = format!("0x{}", &packed[..32].trim_start_matches('0'));
                userop.call_gas_limit = format!("0x{}", &packed[32..].trim_start_matches('0'));
                if userop.verification_gas_limit == "0x" { userop.verification_gas_limit = "0x0".to_string(); }
                if userop.call_gas_limit == "0x" { userop.call_gas_limit = "0x0".to_string(); }
            }
        }
        if let Some(v) = result.get("gasFees") {
            // bytes32 = maxPriorityFeePerGas (16 bytes) || maxFeePerGas (16 bytes)
            let packed = v.as_str().unwrap_or("0x");
            let packed = packed.trim_start_matches("0x");
            if packed.len() == 64 {
                userop.max_priority_fee_per_gas = format!("0x{}", &packed[..32].trim_start_matches('0'));
                userop.max_fee_per_gas = format!("0x{}", &packed[32..].trim_start_matches('0'));
                if userop.max_priority_fee_per_gas == "0x" { userop.max_priority_fee_per_gas = "0x0".to_string(); }
                if userop.max_fee_per_gas == "0x" { userop.max_fee_per_gas = "0x0".to_string(); }
            }
        }

        // v0.7 separate paymaster fields
        if let Some(v) = result.get("paymaster") {
            let pm = v.as_str().unwrap_or("");
            if !pm.is_empty() && pm != "0x" {
                // Reconstruct paymasterAndData from separate fields
                let pm_vgl = result.get("paymasterVerificationGasLimit")
                    .and_then(|v| v.as_str())
                    .unwrap_or("0x0");
                let pm_pgl = result.get("paymasterPostOpGasLimit")
                    .and_then(|v| v.as_str())
                    .unwrap_or("0x0");
                let pm_data = result.get("paymasterData")
                    .and_then(|v| v.as_str())
                    .unwrap_or("0x");

                // Pack: paymaster(20) + verificationGasLimit(16) + postOpGasLimit(16) + data
                let pm_addr = pm.trim_start_matches("0x");
                let vgl_padded = format!("{:0>32}", pm_vgl.trim_start_matches("0x"));
                let pgl_padded = format!("{:0>32}", pm_pgl.trim_start_matches("0x"));
                let data = pm_data.trim_start_matches("0x");
                userop.paymaster_and_data = format!("0x{}{}{}{}", pm_addr, vgl_padded, pgl_padded, data);
            }
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

    // 4. Sign the UserOp
    let userop_hash = compute_userop_hash(&userop, ENTRYPOINT_ADDRESS, chain_id)?;
    let signature = sign_userop_hash(&userop_hash, private_key)?;
    userop.signature = format!("0x{}", hex::encode(&signature));

    // 5. Submit via eth_sendUserOperation
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

async fn get_nonce(client: &reqwest::Client, sender: &str, chain_id: u64) -> Result<u64> {
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
    let nonce = u64::from_str_radix(result.trim_start_matches("0x"), 16).unwrap_or(0);
    Ok(nonce)
}

/// Compute the UserOp hash for signing (ERC-4337 v0.6 format).
///
/// hash = keccak256(abi.encode(keccak256(pack(userOp)), entryPoint, chainId))
fn compute_userop_hash(
    userop: &UserOperation,
    entrypoint: &str,
    chain_id: u64,
) -> Result<[u8; 32]> {
    use tiny_keccak::{Hasher, Keccak};

    // Pack the UserOp fields (excluding signature) and keccak256
    let pack = pack_userop_for_hash(userop)?;
    let inner_hash = keccak256_hash(&pack);

    // abi.encode(innerHash, entryPoint, chainId)
    let mut outer = Vec::with_capacity(96);
    outer.extend_from_slice(&inner_hash);
    // entryPoint padded to 32 bytes
    let ep_bytes = hex::decode(entrypoint.trim_start_matches("0x"))
        .map_err(|e| Error::Crypto(e.to_string()))?;
    outer.extend_from_slice(&[0u8; 12]); // left-pad to 32 bytes
    outer.extend_from_slice(&ep_bytes);
    // chainId as uint256
    let mut chain_bytes = [0u8; 32];
    chain_bytes[24..].copy_from_slice(&chain_id.to_be_bytes());
    outer.extend_from_slice(&chain_bytes);

    Ok(keccak256_hash(&outer))
}

/// Pack UserOp fields for hashing (ERC-4337 v0.6 format).
///
/// pack(sender, nonce, keccak256(initCode), keccak256(callData),
///   callGasLimit, verificationGasLimit, preVerificationGas,
///   maxFeePerGas, maxPriorityFeePerGas, keccak256(paymasterAndData))
fn pack_userop_for_hash(userop: &UserOperation) -> Result<Vec<u8>> {
    let decode_hex = |s: &str| -> Vec<u8> {
        hex::decode(s.trim_start_matches("0x")).unwrap_or_default()
    };

    let mut packed = Vec::new();
    // sender (address, 32 bytes padded)
    packed.extend_from_slice(&[0u8; 12]);
    packed.extend_from_slice(&decode_hex(&userop.sender));
    // nonce (uint256)
    let nonce = u64::from_str_radix(userop.nonce.trim_start_matches("0x"), 16).unwrap_or(0);
    let mut nonce_bytes = [0u8; 32];
    nonce_bytes[24..].copy_from_slice(&nonce.to_be_bytes());
    packed.extend_from_slice(&nonce_bytes);
    // keccak256(initCode)
    packed.extend_from_slice(&keccak256_hash(&decode_hex(&userop.init_code)));
    // keccak256(callData)
    packed.extend_from_slice(&keccak256_hash(&decode_hex(&userop.call_data)));
    // callGasLimit (uint256)
    packed.extend_from_slice(&pad_hex_to_32(&userop.call_gas_limit));
    // verificationGasLimit (uint256)
    packed.extend_from_slice(&pad_hex_to_32(&userop.verification_gas_limit));
    // preVerificationGas (uint256)
    packed.extend_from_slice(&pad_hex_to_32(&userop.pre_verification_gas));
    // maxFeePerGas (uint256)
    packed.extend_from_slice(&pad_hex_to_32(&userop.max_fee_per_gas));
    // maxPriorityFeePerGas (uint256)
    packed.extend_from_slice(&pad_hex_to_32(&userop.max_priority_fee_per_gas));
    // keccak256(paymasterAndData)
    packed.extend_from_slice(&keccak256_hash(&decode_hex(&userop.paymaster_and_data)));

    Ok(packed)
}

fn pad_hex_to_32(hex_str: &str) -> [u8; 32] {
    let val = u128::from_str_radix(hex_str.trim_start_matches("0x"), 16).unwrap_or(0);
    let mut bytes = [0u8; 32];
    bytes[16..].copy_from_slice(&val.to_be_bytes());
    bytes
}

fn keccak256_hash(data: &[u8]) -> [u8; 32] {
    use tiny_keccak::{Hasher, Keccak};
    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(data);
    keccak.finalize(&mut hash);
    hash
}

/// Sign a UserOp hash with a private key (EIP-191 personal sign).
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
    fn test_keccak256_hash() {
        let hash = keccak256_hash(b"hello");
        assert_eq!(
            hex::encode(hash),
            "1c8aff950685c2ed4bc3174f3472287b56d9517b9c948127319a09a7a36deac8"
        );
    }
}
