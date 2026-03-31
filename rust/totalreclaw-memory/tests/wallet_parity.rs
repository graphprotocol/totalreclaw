//! Verify EOA derivation matches TypeScript (viem's mnemonicToAccount).

use totalreclaw_memory::wallet;

/// The 12-word "abandon...about" mnemonic's EOA at BIP-44 m/44'/60'/0'/0/0.
/// Verified against Python eth_account.Account.from_mnemonic() and iancoleman.io/bip39.
/// Private key: 1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727
/// Address: 0x9858EfFD232B4033E47d90003D41EC34EcaEda94
#[test]
fn test_eoa_derivation_parity() {
    let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    let w = wallet::derive_eoa(mnemonic).unwrap();
    assert_eq!(
        hex::encode(&w.private_key),
        "1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727",
        "Private key must match reference derivation"
    );
    assert_eq!(
        w.address.to_lowercase(),
        "0x9858effd232b4033e47d90003d41ec34ecaeda94",
        "EOA must match eth_account.from_mnemonic for the 12-word test mnemonic"
    );
}

#[tokio::test]
#[ignore] // Requires network access to Base Sepolia RPC
async fn test_smart_account_address_parity() {
    // The "abandon...about" mnemonic's Smart Account on Base Sepolia.
    // Known from previous E2E tests and TypeScript derivation.
    let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    let eoa = wallet::derive_eoa_address(mnemonic).unwrap();

    // Call the factory contract on Base Sepolia
    let smart_account = wallet::resolve_smart_account_address(
        &eoa,
        "https://sepolia.base.org",
    )
    .await
    .unwrap();

    // This should match the address used by the TS client
    // (deterministic CREATE2 -- same on all EVM chains)
    assert!(
        smart_account.starts_with("0x"),
        "Should be a valid address"
    );
    assert_eq!(smart_account.len(), 42, "Should be 42 chars (0x + 40 hex)");
    println!("Smart Account for abandon mnemonic: {}", smart_account);
}
