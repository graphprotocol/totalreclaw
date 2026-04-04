//! Native ERC-4337 UserOperation construction and submission.
//!
//! Pure crypto/encoding (ABI encoding, hashing, signing) is provided by
//! `totalreclaw_core::userop`. This module re-exports those and adds the
//! I/O layer: JSON-RPC submission to the relay bundler proxy.
//!
//! Reference: mcp/src/subgraph/store.ts (TypeScript implementation)

// Re-export pure functions and types from core.
pub use totalreclaw_core::userop::{
    encode_batch_call, encode_single_call, hash_userop, sign_userop, UserOperationV7,
    DATA_EDGE_ADDRESS, ENTRYPOINT_ADDRESS, MAX_BATCH_SIZE, SIMPLE_ACCOUNT_FACTORY,
};

use crate::{Error, Result};

/// Result of a UserOp submission.
#[derive(Debug)]
pub struct SubmitResult {
    pub tx_hash: String,
    pub user_op_hash: String,
    pub success: bool,
}

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
    let userop_hash = hash_userop(&userop, ENTRYPOINT_ADDRESS, chain_id)?;
    let signature = sign_userop(&userop_hash, private_key)?;
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

    // 8. Poll for receipt
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
// I/O helpers (stay in this crate — they need reqwest)
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

/// Local keccak256 for submit_userop's EOA derivation.
/// (The core crate's keccak256 is private; this is only used for the
/// factory initCode computation in submit_userop.)
fn keccak256_hash(data: &[u8]) -> [u8; 32] {
    use tiny_keccak::{Hasher, Keccak};
    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(data);
    keccak.finalize(&mut hash);
    hash
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

    /// Verify that re-exports from core produce identical results.
    #[test]
    fn test_re_exported_encode_single_call() {
        let payload = b"test protobuf data";
        let encoded = encode_single_call(payload);
        // Should start with execute() selector 0xb61d27f6
        assert_eq!(&encoded[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
        assert!(encoded.len() > 100);
    }

    #[test]
    fn test_re_exported_encode_batch_call() {
        let payloads = vec![
            b"fact one".to_vec(),
            b"fact two".to_vec(),
            b"fact three".to_vec(),
        ];
        let encoded = encode_batch_call(&payloads).unwrap();
        assert_eq!(&encoded[..4], &[0x47, 0xe1, 0xda, 0x2a]);
    }

    #[test]
    fn test_re_exported_hash_and_sign() {
        // Verify the full hash+sign pipeline through re-exports
        let userop = UserOperationV7 {
            sender: "0x949bc374325a4f41e46e8e78a07d910332934542".to_string(),
            nonce: "0x0".to_string(),
            factory: Some("0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985".to_string()),
            factory_data: Some("0x5fbfb9cf0000000000000000000000008eb626f727e92a73435f2b85dd6fd0c6da5dbb720000000000000000000000000000000000000000000000000000000000000000".to_string()),
            call_data: "0xb61d27f6".to_string(),
            call_gas_limit: "0x186a0".to_string(),
            verification_gas_limit: "0x30d40".to_string(),
            pre_verification_gas: "0xc350".to_string(),
            max_fee_per_gas: "0xf4240".to_string(),
            max_priority_fee_per_gas: "0x7a120".to_string(),
            paymaster: Some("0x0000000000000039cd5e8ae05257ce51c473ddd1".to_string()),
            paymaster_verification_gas_limit: Some("0x186a0".to_string()),
            paymaster_post_op_gas_limit: Some("0xc350".to_string()),
            paymaster_data: Some("0xabcd".to_string()),
            signature: format!("0x{}", "00".repeat(65)),
        };

        let hash = hash_userop(
            &userop,
            "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            84532,
        )
        .unwrap();

        assert_eq!(
            format!("0x{}", hex::encode(hash)),
            "0x4525d2a8a555a1a56f6313735b83fe3ee55f81d504d905ea85613524973f97c2",
        );

        // Sign with Hardhat #0 key
        let pk_hex = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
        let mut pk = [0u8; 32];
        pk.copy_from_slice(&hex::decode(pk_hex).unwrap());

        let test_hash_hex = "1b25552f7901991cd4e2793945f694a09c9d0b9454a86cee16123ac9e84bd2de";
        let mut test_hash = [0u8; 32];
        test_hash.copy_from_slice(&hex::decode(test_hash_hex).unwrap());

        let sig = sign_userop(&test_hash, &pk).unwrap();
        assert_eq!(
            hex::encode(&sig),
            "24b6fabd386f1580aa1fc09b04dd274ea334a9bf63e4fc994e0bef9a505f618335cb2b7d20454a0526f5c66f52ed73b9e76e9696ab5959998e7fc3984fba91691c",
        );
    }

    #[test]
    fn test_constants_re_exported() {
        assert_eq!(DATA_EDGE_ADDRESS, "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca");
        assert_eq!(ENTRYPOINT_ADDRESS, "0x0000000071727De22E5E9d8BAf0edAc6f37da032");
        assert_eq!(SIMPLE_ACCOUNT_FACTORY, "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985");
        assert_eq!(MAX_BATCH_SIZE, 15);
    }
}
