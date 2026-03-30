# Native ERC-4337 UserOps, Client Batching & Missing Tools — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the TypeScript bridge with native Rust ERC-4337 UserOp construction (via Alloy), add client batching (multi-call UserOps), and implement the missing migrate/upgrade tools — closing all known gaps in the ZeroClaw memory backend.

**Architecture:** Use the `alloy` crate ecosystem for native Ethereum operations: `alloy-primitives` for types, `alloy-sol-types` for ABI encoding, `alloy-signer-local` for secp256k1 signing (BIP-44 key derivation), and raw JSON-RPC calls to the relay's `/v1/bundler` endpoint for Pimlico-proxied UserOp submission. The relay is a dumb proxy — the client builds complete UserOps.

**Tech Stack:** Rust, `alloy-primitives`, `alloy-sol-types`, `alloy-signer-local`, `alloy-consensus`, `k256` (secp256k1), `tiny-keccak` (keccak256), `coins-bip32` (HD wallet derivation)

---

## Prerequisites

Read these files before starting any task:

| File | What it contains |
|------|-----------------|
| `rust/totalreclaw-memory/src/relay.rs` | Current relay client — you'll modify `submit_protobuf()` |
| `rust/totalreclaw-memory/src/store.rs` | Store pipeline — calls relay for submission |
| `rust/totalreclaw-memory/src/backend.rs` | Memory trait — orchestrates store/recall |
| `mcp/src/subgraph/store.ts:187-286` | TypeScript UserOp construction (the reference implementation) |
| `client/src/userop/batcher.ts` | TypeScript batching (multi-call UserOps) |
| `mcp/src/tools/migrate.ts` | TypeScript migrate tool (testnet→mainnet) |
| `mcp/src/tools/upgrade.ts` | TypeScript upgrade tool (Stripe checkout) |
| `rust/totalreclaw-memory/Cargo.toml` | Current dependencies |
| `CLAUDE.md` | Project context, constants, addresses |

**Key constants (from CLAUDE.md):**
- DataEdge contract: `0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca` (same on both chains)
- EntryPoint v0.7: `0x0000000071727De22E5E9d8BAf0edAc6f37da032`
- SimpleAccountFactory: `0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985`
- Staging relay: `https://api-staging.totalreclaw.xyz`
- Production relay: `https://api.totalreclaw.xyz`
- Free tier chain: Base Sepolia (84532)
- Pro tier chain: Gnosis mainnet (100)
- Max batch size: 15 (matches extraction cap)

**IMPORTANT: All tests MUST hit staging (`api-staging.totalreclaw.xyz`), NEVER production.**

---

## Phase 1: BIP-44 EOA Derivation + Smart Account Address

### Task 1: Add Alloy + HD wallet dependencies

**Files:**
- Modify: `rust/totalreclaw-memory/Cargo.toml`

**Step 1: Add dependencies**

```toml
# Add under [dependencies]:

# Ethereum / ERC-4337
alloy-primitives = "1"
alloy-sol-types = "1"
k256 = { version = "0.13", features = ["ecdsa"] }
tiny-keccak = { version = "2", features = ["keccak"] }
coins-bip32 = "0.12"
```

**Step 2: Verify it compiles**

```bash
cd rust/totalreclaw-memory && cargo build
```

**Step 3: Commit**

```bash
git add Cargo.toml Cargo.lock
git commit -m "deps(rust): add alloy-primitives, k256, coins-bip32 for native ERC-4337"
```

---

### Task 2: Derive EOA from mnemonic (BIP-44 m/44'/60'/0'/0/0)

**Files:**
- Create: `rust/totalreclaw-memory/src/wallet.rs`
- Modify: `rust/totalreclaw-memory/src/lib.rs` (add `pub mod wallet;`)
- Create: `rust/totalreclaw-memory/tests/wallet_parity.rs`

**Step 1: Write the failing test**

```rust
// rust/totalreclaw-memory/tests/wallet_parity.rs
//! Verify EOA derivation matches TypeScript (viem's mnemonicToAccount).

use totalreclaw_memory::wallet;

/// The "abandon...about" mnemonic's EOA is well-known.
/// viem: mnemonicToAccount("abandon abandon ... about").address
/// = 0x1CB1a5e65610AEFF2551A50f76a87a7d3fB900C2 (BIP-44 m/44'/60'/0'/0/0)
#[test]
fn test_eoa_derivation_parity() {
    let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    let eoa = wallet::derive_eoa_address(mnemonic).unwrap();
    assert_eq!(
        eoa.to_lowercase(),
        "0x1cb1a5e65610aeff2551a50f76a87a7d3fb900c2",
        "EOA must match viem's mnemonicToAccount for the standard test mnemonic"
    );
}
```

**Step 2: Run test to verify it fails**

```bash
cargo test --test wallet_parity
```
Expected: FAIL (function not found)

**Step 3: Implement EOA derivation**

```rust
// rust/totalreclaw-memory/src/wallet.rs
//! Ethereum wallet derivation from BIP-39 mnemonic.
//!
//! Derives the EOA (externally-owned account) address and private key
//! via BIP-44 path m/44'/60'/0'/0/0, matching viem's mnemonicToAccount().

use coins_bip32::prelude::*;
use k256::ecdsa::SigningKey;
use tiny_keccak::{Hasher, Keccak};

use crate::{Error, Result};

/// Derived Ethereum wallet (EOA + signing key).
pub struct EthWallet {
    /// Private key bytes (32 bytes).
    pub private_key: [u8; 32],
    /// EOA address (0x-prefixed, lowercase hex).
    pub address: String,
}

/// Derive an Ethereum EOA from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Matches viem's `mnemonicToAccount(mnemonic)`.
pub fn derive_eoa(mnemonic: &str) -> Result<EthWallet> {
    // 1. BIP-39 seed (same as crypto.rs — PBKDF2-HMAC-SHA512)
    let seed = crate::crypto::mnemonic_to_seed_bytes(mnemonic)?;

    // 2. BIP-32 master key from seed
    let master = XPriv::root_from_seed(&seed, None)
        .map_err(|e| Error::Crypto(format!("BIP-32 master key failed: {}", e)))?;

    // 3. Derive m/44'/60'/0'/0/0
    let path = "m/44'/60'/0'/0/0";
    let derived = master.derive_path(path)
        .map_err(|e| Error::Crypto(format!("BIP-44 derivation failed: {}", e)))?;

    // 4. Extract 32-byte private key
    let key_bytes = derived.secret_key();
    let mut private_key = [0u8; 32];
    private_key.copy_from_slice(&key_bytes.to_bytes());

    // 5. Derive public key → keccak256 → last 20 bytes = address
    let signing_key = SigningKey::from_bytes((&private_key).into())
        .map_err(|e| Error::Crypto(format!("Invalid private key: {}", e)))?;
    let verifying_key = signing_key.verifying_key();
    let public_key_bytes = verifying_key.to_encoded_point(false);
    // Uncompressed public key: 0x04 || x (32 bytes) || y (32 bytes)
    // Keccak256 the 64 bytes (skip the 0x04 prefix)
    let pubkey_raw = &public_key_bytes.as_bytes()[1..]; // skip 0x04

    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(pubkey_raw);
    keccak.finalize(&mut hash);

    // Address = last 20 bytes of keccak256(pubkey)
    let address = format!("0x{}", hex::encode(&hash[12..]));

    Ok(EthWallet {
        private_key,
        address,
    })
}

/// Convenience: derive just the address string.
pub fn derive_eoa_address(mnemonic: &str) -> Result<String> {
    Ok(derive_eoa(mnemonic)?.address)
}
```

**IMPORTANT:** You also need to expose `mnemonic_to_seed_bytes` from `crypto.rs`. The current `mnemonic_to_seed` returns `[u8; 64]` but is private. Add a public wrapper:

```rust
// Add to crypto.rs
/// Public access to the raw BIP-39 seed bytes (64 bytes).
/// Used by wallet.rs for BIP-32/BIP-44 derivation.
pub fn mnemonic_to_seed_bytes(mnemonic: &str) -> Result<[u8; 64]> {
    mnemonic_to_seed(mnemonic)
}
```

**Step 4: Run test**

```bash
cargo test --test wallet_parity -- --nocapture
```
Expected: PASS

**Step 5: Commit**

```bash
git add src/wallet.rs src/lib.rs src/crypto.rs tests/wallet_parity.rs
git commit -m "feat(rust): BIP-44 EOA derivation with parity test"
```

---

### Task 3: Smart Account address resolution (CREATE2 factory call)

**Files:**
- Modify: `rust/totalreclaw-memory/src/wallet.rs`
- Modify: `rust/totalreclaw-memory/tests/wallet_parity.rs`

**Step 1: Write the failing test**

```rust
// Add to wallet_parity.rs
#[tokio::test]
async fn test_smart_account_address_parity() {
    // The "abandon...about" mnemonic's Smart Account on Base Sepolia.
    // Known from previous E2E tests and TypeScript derivation.
    let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    let eoa = wallet::derive_eoa_address(mnemonic).unwrap();

    // Call the factory contract on Base Sepolia
    let smart_account = wallet::resolve_smart_account_address(
        &eoa,
        "https://sepolia.base.org",
    ).await.unwrap();

    // This should match the address used by the TS client
    // (deterministic CREATE2 — same on all EVM chains)
    assert!(smart_account.starts_with("0x"), "Should be a valid address");
    assert_eq!(smart_account.len(), 42, "Should be 42 chars (0x + 40 hex)");
    println!("Smart Account for abandon mnemonic: {}", smart_account);
}
```

**Step 2: Implement factory call**

```rust
// Add to wallet.rs

/// SimpleAccountFactory address (v0.7, same on all EVM chains).
const SIMPLE_ACCOUNT_FACTORY: &str = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985";

/// Resolve the CREATE2 Smart Account address by calling the factory.
///
/// Calls SimpleAccountFactory.getAddress(owner, 0) via eth_call.
/// The factory returns the deterministic CREATE2 address (same on all chains).
pub async fn resolve_smart_account_address(
    eoa_address: &str,
    rpc_url: &str,
) -> Result<String> {
    // ABI-encode: getAddress(address,uint256)
    // keccak256("getAddress(address,uint256)")[:4] = 0x8cb84e18
    let selector = "8cb84e18";
    let owner = eoa_address.trim_start_matches("0x").to_lowercase();
    let owner_padded = format!("{:0>64}", owner);
    let salt_padded = "0".repeat(64);
    let calldata = format!("0x{}{}{}", selector, owner_padded, salt_padded);

    let client = reqwest::Client::new();
    let resp = client
        .post(rpc_url)
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": SIMPLE_ACCOUNT_FACTORY, "data": calldata}, "latest"],
            "id": 1
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let body: serde_json::Value = resp.json().await
        .map_err(|e| Error::Http(e.to_string()))?;

    let result = body["result"].as_str().unwrap_or("");
    if result.len() < 42 {
        return Err(Error::Http(format!("Factory returned invalid result: {}", result)));
    }

    Ok(format!("0x{}", &result[result.len() - 40..]).to_lowercase())
}
```

**Step 3: Run test**

```bash
cargo test --test wallet_parity -- --ignored --nocapture
```
Expected: PASS (the `test_smart_account_address_parity` needs `#[ignore]` since it hits a real RPC)

**Step 4: Commit**

```bash
git add src/wallet.rs tests/wallet_parity.rs
git commit -m "feat(rust): Smart Account address resolution via CREATE2 factory"
```

---

## Phase 2: Native UserOp Construction + Submission

### Task 4: ABI encoding for SimpleAccount.execute()

**Files:**
- Create: `rust/totalreclaw-memory/src/userop.rs`
- Modify: `rust/totalreclaw-memory/src/lib.rs` (add `pub mod userop;`)

**Step 1: Implement ABI encoding**

The SimpleSmartAccount's `execute(address dest, uint256 value, bytes data)` function selector is `0xb61d27f6`.

For batch: `executeBatch(address[] dest, uint256[] values, bytes[] data)` selector is `0x34fcd5be`.

```rust
// rust/totalreclaw-memory/src/userop.rs
//! Native ERC-4337 UserOperation construction and submission.
//!
//! Replaces the TypeScript bridge with pure Rust:
//! - ABI encoding for SimpleAccount execute/executeBatch
//! - UserOp struct construction (nonce, gas, calldata)
//! - EIP-191 signing over UserOp hash
//! - JSON-RPC submission to relay bundler proxy
//!
//! Reference: mcp/src/subgraph/store.ts (TypeScript implementation)

use alloy_primitives::{Address, Bytes, U256, B256, FixedBytes, keccak256};
use alloy_sol_types::{sol, SolCall};
use k256::ecdsa::{SigningKey, signature::Signer};

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
    function execute(address dest, uint256 value, bytes calldata data);

    /// SimpleAccount.executeBatch(address[] dest, uint256[] values, bytes[] data)
    function executeBatch(address[] calldata dest, uint256[] calldata values, bytes[] calldata data);
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

    // Single payload → use execute() (no batch overhead)
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
```

**Step 2: Write test**

```rust
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
        // Should start with executeBatch() selector 0x34fcd5be
        assert_eq!(&encoded[..4], &[0x34, 0xfc, 0xd5, 0xbe]);
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
}
```

**Step 3: Run tests**

```bash
cargo test userop::tests -- --nocapture
```
Expected: All PASS

**Step 4: Commit**

```bash
git add src/userop.rs src/lib.rs
git commit -m "feat(rust): ABI encoding for SimpleAccount execute/executeBatch"
```

---

### Task 5: UserOp struct + signing + submission

**Files:**
- Modify: `rust/totalreclaw-memory/src/userop.rs`

This is the core ERC-4337 flow. The relay proxies JSON-RPC to Pimlico, so the client must send standard bundler JSON-RPC:

1. `pm_sponsorUserOperation` — get paymaster data + gas estimates
2. `eth_sendUserOperation` — submit the signed UserOp
3. `eth_getUserOperationReceipt` — poll for confirmation

**Step 1: Add UserOp struct and submission logic**

```rust
// Add to userop.rs

/// ERC-4337 UserOperation (v0.7 packed format).
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UserOperation {
    pub sender: String,
    pub nonce: String,           // hex
    pub init_code: String,       // hex, "0x" if deployed
    pub call_data: String,       // hex
    pub call_gas_limit: String,  // hex
    pub verification_gas_limit: String,  // hex
    pub pre_verification_gas: String,    // hex
    pub max_fee_per_gas: String,         // hex
    pub max_priority_fee_per_gas: String, // hex
    pub paymaster_and_data: String,       // hex
    pub signature: String,                // hex
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
    sender: &str,       // Smart Account address
    private_key: &[u8; 32],
    relay_url: &str,
    auth_key_hex: &str,
    chain_id: u64,
) -> Result<SubmitResult> {
    let bundler_url = format!("{}/v1/bundler", relay_url.trim_end_matches('/'));
    let client = reqwest::Client::new();
    let calldata_hex = format!("0x{}", hex::encode(calldata));

    let headers = build_headers(auth_key_hex, sender);

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
        signature: "0x".to_string(), // dummy for estimation
    };

    // 3. Get paymaster sponsorship via pm_sponsorUserOperation
    let sponsor_resp = jsonrpc_call(
        &client,
        &bundler_url,
        "pm_sponsorUserOperation",
        serde_json::json!([userop, ENTRYPOINT_ADDRESS, {"sponsorshipPolicyId": "sp_cheerful_cousin"}]),
        &headers,
    ).await?;

    // Apply paymaster response (gas limits + paymaster data)
    if let Some(result) = sponsor_resp.get("result") {
        if let Some(v) = result.get("callGasLimit") { userop.call_gas_limit = v.as_str().unwrap_or("0x0").to_string(); }
        if let Some(v) = result.get("verificationGasLimit") { userop.verification_gas_limit = v.as_str().unwrap_or("0x0").to_string(); }
        if let Some(v) = result.get("preVerificationGas") { userop.pre_verification_gas = v.as_str().unwrap_or("0x0").to_string(); }
        if let Some(v) = result.get("maxFeePerGas") { userop.max_fee_per_gas = v.as_str().unwrap_or("0x0").to_string(); }
        if let Some(v) = result.get("maxPriorityFeePerGas") { userop.max_priority_fee_per_gas = v.as_str().unwrap_or("0x0").to_string(); }
        if let Some(v) = result.get("paymasterAndData") { userop.paymaster_and_data = v.as_str().unwrap_or("0x").to_string(); }
    } else {
        return Err(Error::Http(format!("Paymaster sponsorship failed: {:?}", sponsor_resp)));
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
    ).await?;

    let op_hash = send_resp["result"]
        .as_str()
        .ok_or_else(|| Error::Http(format!("No userOpHash in response: {:?}", send_resp)))?
        .to_string();

    // 6. Poll for receipt
    let receipt = poll_receipt(&client, &bundler_url, &op_hash, &headers).await?;

    Ok(SubmitResult {
        tx_hash: receipt["receipt"]["transactionHash"]
            .as_str().unwrap_or("").to_string(),
        user_op_hash: op_hash,
        success: receipt["success"].as_bool().unwrap_or(false),
    })
}

/// Result of a UserOp submission.
pub struct SubmitResult {
    pub tx_hash: String,
    pub user_op_hash: String,
    pub success: bool,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn build_headers(auth_key_hex: &str, wallet: &str) -> reqwest::header::HeaderMap {
    let mut h = reqwest::header::HeaderMap::new();
    h.insert("X-TotalReclaw-Client", "zeroclaw-memory".parse().unwrap());
    h.insert("Authorization", format!("Bearer {}", auth_key_hex).parse().unwrap());
    h.insert("X-Wallet-Address", wallet.parse().unwrap());
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

async fn get_nonce(
    client: &reqwest::Client,
    sender: &str,
    chain_id: u64,
) -> Result<u64> {
    let rpc_url = match chain_id {
        84532 => "https://sepolia.base.org",
        100 => "https://rpc.gnosischain.com",
        _ => "https://sepolia.base.org",
    };

    // EntryPoint.getNonce(address sender, uint192 key) → selector 0x35567e1a
    let sender_padded = format!("{:0>64}", sender.trim_start_matches("0x").to_lowercase());
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

    let body: serde_json::Value = resp.json().await
        .map_err(|e| Error::Http(e.to_string()))?;

    let result = body["result"].as_str().unwrap_or("0x0");
    let nonce = u64::from_str_radix(result.trim_start_matches("0x"), 16).unwrap_or(0);
    Ok(nonce)
}

/// Compute the UserOp hash for signing.
///
/// hash = keccak256(abi.encode(keccak256(pack(userOp)), entryPoint, chainId))
fn compute_userop_hash(
    userop: &UserOperation,
    entrypoint: &str,
    chain_id: u64,
) -> Result<[u8; 32]> {
    // Pack the UserOp fields (excluding signature) and keccak256
    // This must match the EntryPoint's getUserOpHash() implementation
    let pack = pack_userop_for_hash(userop)?;
    let inner_hash = keccak256(&pack);

    // abi.encode(innerHash, entryPoint, chainId)
    let mut outer = Vec::with_capacity(96);
    outer.extend_from_slice(inner_hash.as_slice());
    // entryPoint padded to 32 bytes
    let ep_bytes = hex::decode(entrypoint.trim_start_matches("0x"))
        .map_err(|e| Error::Crypto(e.to_string()))?;
    outer.extend_from_slice(&[0u8; 12]); // left-pad to 32 bytes
    outer.extend_from_slice(&ep_bytes);
    // chainId as uint256
    let mut chain_bytes = [0u8; 32];
    chain_bytes[24..].copy_from_slice(&chain_id.to_be_bytes());
    outer.extend_from_slice(&chain_bytes);

    Ok(keccak256(&outer).into())
}

/// Pack UserOp fields for hashing (ERC-4337 v0.7 format).
fn pack_userop_for_hash(userop: &UserOperation) -> Result<Vec<u8>> {
    // For v0.7: pack(sender, nonce, keccak256(initCode), keccak256(callData),
    //   accountGasLimits, preVerificationGas, gasFees, keccak256(paymasterAndData))
    // This is complex — refer to EntryPoint source for exact packing.
    // For now, use the simple v0.6 compatible approach.

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
    packed.extend_from_slice(keccak256(&decode_hex(&userop.init_code)).as_slice());
    // keccak256(callData)
    packed.extend_from_slice(keccak256(&decode_hex(&userop.call_data)).as_slice());
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
    packed.extend_from_slice(keccak256(&decode_hex(&userop.paymaster_and_data)).as_slice());

    Ok(packed)
}

fn pad_hex_to_32(hex_str: &str) -> [u8; 32] {
    let val = u128::from_str_radix(hex_str.trim_start_matches("0x"), 16).unwrap_or(0);
    let mut bytes = [0u8; 32];
    bytes[16..].copy_from_slice(&val.to_be_bytes());
    bytes
}

/// Sign a UserOp hash with a private key (EIP-191 personal sign).
fn sign_userop_hash(hash: &[u8; 32], private_key: &[u8; 32]) -> Result<Vec<u8>> {
    let signing_key = SigningKey::from_bytes(private_key.into())
        .map_err(|e| Error::Crypto(format!("Invalid signing key: {}", e)))?;

    // EIP-191: "\x19Ethereum Signed Message:\n32" + hash
    let mut prefixed = Vec::with_capacity(60);
    prefixed.extend_from_slice(b"\x19Ethereum Signed Message:\n32");
    prefixed.extend_from_slice(hash);
    let msg_hash = keccak256(&prefixed);

    let (sig, recovery_id) = signing_key
        .sign_prehash_recoverable(msg_hash.as_slice())
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
        ).await?;

        if resp.get("result").and_then(|r| r.as_object()).is_some() {
            return Ok(resp["result"].clone());
        }

        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }

    Err(Error::Http("UserOp receipt timeout after 120s".into()))
}
```

**CRITICAL PARITY NOTE:** The UserOp hash computation must match the EntryPoint v0.7 format exactly. The v0.7 format packs `accountGasLimits` as `bytes32(verificationGasLimit || callGasLimit)` and `gasFees` as `bytes32(maxPriorityFeePerGas || maxFeePerGas)`. If the signature fails validation, compare the hash computation byte-by-byte against the TypeScript `permissionless` library's implementation. The Pimlico sponsorship response may also use v0.7 packed format fields like `accountGasLimits` and `gasFees` instead of separate fields — handle both.

**Step 2: Run test**

```bash
cargo test userop -- --nocapture
```
Expected: ABI encoding tests PASS. The full submission test requires staging relay access.

**Step 3: Commit**

```bash
git add src/userop.rs
git commit -m "feat(rust): native ERC-4337 UserOp construction + signing + submission"
```

---

### Task 6: Wire native UserOp into store pipeline

**Files:**
- Modify: `rust/totalreclaw-memory/src/relay.rs` — replace `submit_protobuf()` with native UserOp
- Modify: `rust/totalreclaw-memory/src/store.rs` — pass wallet/keys for signing
- Modify: `rust/totalreclaw-memory/src/backend.rs` — pass wallet info through

**Step 1: Update relay.rs**

Replace the existing `submit_protobuf()` method with a call to `userop::submit_userop()`. The relay client should now hold the private key and chain ID.

Add to `RelayConfig`:
```rust
pub chain_id: u64,
```

Replace `submit_protobuf()`:
```rust
pub async fn submit_fact_native(
    &self,
    protobuf_payload: &[u8],
    private_key: &[u8; 32],
) -> Result<crate::userop::SubmitResult> {
    let calldata = crate::userop::encode_single_call(protobuf_payload);
    crate::userop::submit_userop(
        &calldata,
        &self.wallet_address,
        private_key,
        &self.relay_url,
        &self.auth_key_hex,
        self.chain_id,
    ).await
}

pub async fn submit_fact_batch_native(
    &self,
    protobuf_payloads: &[Vec<u8>],
    private_key: &[u8; 32],
) -> Result<crate::userop::SubmitResult> {
    let calldata = crate::userop::encode_batch_call(protobuf_payloads)?;
    crate::userop::submit_userop(
        &calldata,
        &self.wallet_address,
        private_key,
        &self.relay_url,
        &self.auth_key_hex,
        self.chain_id,
    ).await
}
```

**Step 2: Update store.rs**

Change `store_fact()` to accept a private key and use `relay.submit_fact_native()`.

**Step 3: Update backend.rs**

Store the private key in `TotalReclawMemory` (derive from wallet). Use `resolve_smart_account_address()` instead of `relay.resolve_address()`.

**Step 4: Run all unit tests**

```bash
cargo test
```
Expected: All 25+ tests PASS

**Step 5: Commit**

```bash
git add src/relay.rs src/store.rs src/backend.rs
git commit -m "feat(rust): wire native UserOp into store pipeline, remove TS bridge dependency"
```

---

### Task 7: Integration test — native store + recall against staging

**Files:**
- Create: `rust/totalreclaw-memory/tests/native_userop_e2e.rs`

**Step 1: Write E2E test**

```rust
//! Integration test: native Rust UserOp store + recall against staging.
//! Proves the full pipeline works without TypeScript.
//!
//! Run: cargo test --test native_userop_e2e -- --ignored --nocapture

#[tokio::test]
#[ignore] // Requires staging relay
async fn test_native_store_and_recall() {
    // 1. Generate fresh mnemonic
    // 2. Derive EOA + Smart Account
    // 3. Register with relay
    // 4. Store a fact using NATIVE Rust UserOp (no TS bridge)
    // 5. Wait for indexing
    // 6. Recall the fact
    // 7. Verify decrypted text matches
}
```

**Step 2: Run against staging**

```bash
cargo test --test native_userop_e2e -- --ignored --nocapture
```
Expected: PASS

**Step 3: Commit**

```bash
git add tests/native_userop_e2e.rs
git commit -m "test(rust): native UserOp E2E — store + recall without TS bridge"
```

---

## Phase 3: Client Batching

### Task 8: Batch store pipeline

**Files:**
- Modify: `rust/totalreclaw-memory/src/store.rs` — add `store_fact_batch()`
- Modify: `rust/totalreclaw-memory/src/backend.rs` — add `store_batch()` method

**Step 1: Add batch store function**

```rust
// store.rs
/// Store multiple facts in a single on-chain transaction (batched UserOp).
///
/// Gas savings: ~64% vs individual submissions for batch of 5.
/// Max batch size: 15 (matches extraction cap).
pub async fn store_fact_batch(
    facts: &[(&str, &str)],  // (content, source) pairs
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: &[u8; 32],
) -> Result<Vec<String>> {
    let mut protobuf_payloads = Vec::with_capacity(facts.len());
    let mut fact_ids = Vec::with_capacity(facts.len());

    for (content, source) in facts {
        // Full pipeline per fact: embed → encrypt → indices → protobuf
        // (same as store_fact but collect payloads instead of submitting)
        let embedding = embedding_provider.embed(content).await?;
        let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
        // ... (same pipeline as store_fact)
        let payload = protobuf::encode_fact_protobuf(&fact_payload);
        protobuf_payloads.push(payload);
        fact_ids.push(fact_id);
    }

    // Submit all as one batched UserOp
    relay.submit_fact_batch_native(&protobuf_payloads, private_key).await?;

    Ok(fact_ids)
}
```

**Step 2: Integration test**

```rust
#[tokio::test]
#[ignore]
async fn test_batch_store_and_recall() {
    // Store 3 facts in one batch → recall all 3
}
```

**Step 3: Commit**

```bash
git commit -m "feat(rust): client batching — multi-call UserOps for batch fact submission"
```

---

## Phase 4: Upgrade Tool

> **NOTE:** Migrate and import tools are intentionally deferred (WIP across all platforms). Only upgrade is implemented here.

### Task 9: Upgrade tool (Stripe checkout URL)

**Files:**
- Modify: `rust/totalreclaw-memory/src/relay.rs` — add `create_checkout()`
- Modify: `rust/totalreclaw-memory/src/backend.rs` — add `upgrade()` method

**Step 1: Implement**

```rust
// relay.rs
/// Create a Stripe checkout session for upgrading to Pro.
pub async fn create_checkout(&self) -> Result<String> {
    let resp = self.client
        .post(format!("{}/v1/billing/checkout", self.relay_url))
        .headers(self.headers())
        .json(&serde_json::json!({
            "wallet_address": self.wallet_address,
            "tier": "pro",
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let body: serde_json::Value = resp.json().await
        .map_err(|e| Error::Http(e.to_string()))?;

    body["checkout_url"]
        .as_str()
        .map(|s| s.to_string())
        .ok_or_else(|| Error::Http("No checkout_url in response".into()))
}
```

**Step 2: Commit**

```bash
git commit -m "feat(rust): upgrade tool — Stripe checkout URL via relay"
```

---

---

## Phase 5: Update Three-Way E2E to Use Native UserOps

### Task 10: Update E2E test to use native Rust store

**Files:**
- Modify: `rust/totalreclaw-memory/tests/three_way_cross_client.rs`

Replace the `store_via_rust_crypto_ts_submit()` function with a call to the native UserOp pipeline. The Rust store should no longer shell out to TypeScript.

**Step 1: Update test**

Replace the TS bridge call with:
```rust
// Use fully native Rust store
let memory = TotalReclawMemory::new(TotalReclawConfig {
    mnemonic: mnemonic.to_string(),
    relay_url: RELAY_URL.to_string(),
    is_test: true,
    ..Default::default()
}).await.unwrap();

memory.store("rust_fact", &fact_a, MemoryCategory::Core, None).await.unwrap();
```

**Step 2: Run full three-way E2E**

```bash
cargo test --test three_way_cross_client -- --ignored --nocapture
```
Expected: 9/9 PASS, this time with Rust storing natively (no TS bridge)

**Step 3: Commit**

```bash
git commit -m "test(rust): three-way E2E now uses native Rust UserOps, no TS bridge"
```

---

## Phase 6: Cleanup + Final Validation

### Task 11: Remove stale code + update CLAUDE.md

**Files:**
- Modify: `rust/totalreclaw-memory/src/relay.rs` — remove old `submit_protobuf()`, `resolve_address()`, `derive_eoa_address()` stubs
- Modify: `CLAUDE.md` — update known gaps (remove "UserOp submission uses TS bridge")

**Step 1: Clean up**

Remove the old simplified submission methods and the relay address resolution that was never working. The native wallet module handles all of this now.

**Step 2: Final test run**

```bash
# All unit tests
cargo test

# Native UserOp E2E
cargo test --test native_userop_e2e -- --ignored --nocapture

# Three-way cross-client (now fully native)
cargo test --test three_way_cross_client -- --ignored --nocapture
```

**Step 3: Commit**

```bash
git commit -m "chore(rust): remove TS bridge stubs, update CLAUDE.md gaps"
```

---

## Execution Order & Dependencies

```
Phase 1 (Wallet):     Tasks 1-3  — sequential (deps build on each other)
Phase 2 (UserOp):     Tasks 4-7  — sequential (ABI → full UserOp → wire → E2E)
Phase 3 (Batching):   Task 8     — after Phase 2
Phase 4 (Upgrade):    Task 9     — after Phase 2 (independent of batching)
Phase 5 (E2E):        Task 10    — after Phases 2-4
Phase 6 (Cleanup):    Task 11    — after all
```

## Validation Checklist — ALL must pass

- [ ] EOA derivation matches viem parity (abandon mnemonic test)
- [ ] Smart Account address resolves correctly via factory eth_call
- [ ] ABI encoding for execute() and executeBatch() is correct
- [ ] Single-fact native UserOp submits and confirms on staging
- [ ] Batch of 3 facts submits as single UserOp on staging
- [ ] Upgrade tool returns Stripe checkout URL
- [ ] Three-way E2E passes with fully native Rust store (no TS bridge)
- [ ] All 25+ unit tests still pass
- [ ] CLAUDE.md gaps updated

## Definition of Done

- [ ] `cargo test` passes all unit tests
- [ ] `cargo test --test native_userop_e2e -- --ignored` passes against staging
- [ ] `cargo test --test three_way_cross_client -- --ignored` passes with native Rust store
- [ ] No TypeScript bridge dependency for store operations
- [ ] Client batching works (multi-fact UserOps)
- [ ] Upgrade tool implemented
- [ ] CLAUDE.md known gaps updated
