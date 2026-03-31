//! Integration test: native Rust UserOp store + recall against staging.
//! Proves the full pipeline works without TypeScript.
//!
//! Run: cargo test --test native_userop_e2e -- --ignored --nocapture

use totalreclaw_memory::crypto;
use totalreclaw_memory::relay::{RelayClient, RelayConfig};
use totalreclaw_memory::search;
use totalreclaw_memory::setup;
use totalreclaw_memory::userop;
use totalreclaw_memory::wallet;

/// Tests MUST hit staging, NEVER production.
const RELAY_URL: &str = "https://api-staging.totalreclaw.xyz";
const INDEXING_WAIT_SECS: u64 = 45;

#[tokio::test]
#[ignore] // Requires staging relay
async fn test_native_store_and_recall() {
    println!("=== Native UserOp E2E Test ===\n");

    // 1. Generate fresh mnemonic
    let mnemonic = setup::generate_mnemonic();
    println!("1. Generated test mnemonic: {}...", &mnemonic[..25]);

    // 2. Derive EOA + Smart Account natively
    let eth_wallet = wallet::derive_eoa(&mnemonic).expect("EOA derivation failed");
    println!("2. EOA: {}", eth_wallet.address);

    let smart_account =
        wallet::resolve_smart_account_address(&eth_wallet.address, "https://sepolia.base.org")
            .await
            .expect("Smart Account resolution failed");
    println!("   Smart Account: {}", smart_account);

    // 3. Derive keys + register
    let keys = crypto::derive_keys_from_mnemonic(&mnemonic).expect("Key derivation failed");
    let auth_key_hex = hex::encode(keys.auth_key);
    let auth_key_hash = crypto::compute_auth_key_hash(&keys.auth_key);
    let salt_hex = hex::encode(keys.salt);

    let relay = RelayClient::new(RelayConfig {
        relay_url: RELAY_URL.to_string(),
        auth_key_hex: auth_key_hex.clone(),
        wallet_address: smart_account.clone(),
        is_test: true,
        chain_id: 84532,
    });

    println!("3. Registering with relay...");
    match relay.register(&auth_key_hash, &salt_hex).await {
        Ok(uid) => println!("   Registered: {}", uid),
        Err(e) => println!("   Registration warning (may already exist): {}", e),
    }

    // 4. Build a fact protobuf (minimal -- no embedding, just blind indices)
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let fact_text = format!("Native UserOp test fact at {}", ts);
    println!("\n4. Storing: '{}'", fact_text);

    let encrypted_b64 = crypto::encrypt(&fact_text, &keys.encryption_key).unwrap();
    let encrypted_bytes = base64::Engine::decode(
        &base64::engine::general_purpose::STANDARD,
        &encrypted_b64,
    )
    .unwrap();
    let encrypted_hex = hex::encode(&encrypted_bytes);

    let blind_indices = totalreclaw_memory::blind::generate_blind_indices(&fact_text);
    let content_fp =
        totalreclaw_memory::fingerprint::generate_content_fingerprint(&fact_text, &keys.dedup_key);

    let fact_id = uuid::Uuid::now_v7().to_string();
    let timestamp = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let payload = totalreclaw_memory::protobuf::FactPayload {
        id: fact_id.clone(),
        timestamp,
        owner: smart_account.clone(),
        encrypted_blob_hex: encrypted_hex,
        blind_indices,
        decay_score: 0.8,
        source: "zeroclaw:native-userop-e2e".to_string(),
        content_fp,
        agent_id: "native-userop-e2e".to_string(),
        encrypted_embedding: None,
    };

    let protobuf = totalreclaw_memory::protobuf::encode_fact_protobuf(&payload);

    // 5. Submit via NATIVE UserOp (no TS bridge!)
    let calldata = userop::encode_single_call(&protobuf);
    println!("   Calldata size: {} bytes", calldata.len());

    let result = userop::submit_userop(
        &calldata,
        &smart_account,
        &eth_wallet.private_key,
        RELAY_URL,
        &auth_key_hex,
        84532,
        true,
    )
    .await
    .expect("UserOp submission failed");

    println!(
        "   Submitted: txHash={} userOpHash={} success={}",
        result.tx_hash, result.user_op_hash, result.success
    );
    assert!(result.success, "UserOp should succeed");
    assert!(
        !result.tx_hash.is_empty(),
        "Should have a transaction hash"
    );

    // 6. Wait for subgraph indexing
    println!(
        "\n5. Waiting {}s for subgraph indexing...",
        INDEXING_WAIT_SECS
    );
    tokio::time::sleep(std::time::Duration::from_secs(INDEXING_WAIT_SECS)).await;

    // 7. Recall the fact
    println!("\n6. Recalling via blind index search...");
    let trapdoors =
        totalreclaw_memory::blind::generate_blind_indices("native userop test fact");

    let candidates =
        search::search_candidates(&relay, &smart_account, &trapdoors, 100)
            .await
            .unwrap_or_default();

    println!("   Found {} candidates", candidates.len());

    let mut found = false;
    for fact in &candidates {
        if let Some(b64) = search::hex_blob_to_base64(&fact.encrypted_blob) {
            if let Ok(text) = crypto::decrypt(&b64, &keys.encryption_key) {
                println!("   Decrypted: {}", text);
                if text.contains(&ts.to_string()) {
                    found = true;
                }
            }
        }
    }

    assert!(found, "Should find the fact we stored via native UserOp");
    println!("\nNative UserOp E2E PASSED!");
}

#[tokio::test]
#[ignore] // Requires staging relay
async fn test_batch_store_and_recall() {
    println!("=== Batch UserOp E2E Test ===\n");

    // 1. Generate fresh mnemonic
    let mnemonic = setup::generate_mnemonic();
    println!("1. Generated test mnemonic: {}...", &mnemonic[..25]);

    // 2. Derive wallet
    let eth_wallet = wallet::derive_eoa(&mnemonic).expect("EOA derivation failed");
    let smart_account =
        wallet::resolve_smart_account_address(&eth_wallet.address, "https://sepolia.base.org")
            .await
            .expect("Smart Account resolution failed");
    println!("   Smart Account: {}", smart_account);

    // 3. Derive keys + register
    let keys = crypto::derive_keys_from_mnemonic(&mnemonic).expect("Key derivation failed");
    let auth_key_hex = hex::encode(keys.auth_key);
    let auth_key_hash = crypto::compute_auth_key_hash(&keys.auth_key);
    let salt_hex = hex::encode(keys.salt);

    let relay = RelayClient::new(RelayConfig {
        relay_url: RELAY_URL.to_string(),
        auth_key_hex: auth_key_hex.clone(),
        wallet_address: smart_account.clone(),
        is_test: true,
        chain_id: 84532,
    });

    relay
        .register(&auth_key_hash, &salt_hex)
        .await
        .expect("Registration failed");

    // 4. Build 3 fact protobufs
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();

    let facts = vec![
        format!("Batch test fact one at {}", ts),
        format!("Batch test fact two at {}", ts + 1),
        format!("Batch test fact three at {}", ts + 2),
    ];

    let mut protobuf_payloads = Vec::new();
    for (i, fact_text) in facts.iter().enumerate() {
        let encrypted_b64 = crypto::encrypt(fact_text, &keys.encryption_key).unwrap();
        let encrypted_bytes = base64::Engine::decode(
            &base64::engine::general_purpose::STANDARD,
            &encrypted_b64,
        )
        .unwrap();
        let encrypted_hex = hex::encode(&encrypted_bytes);
        let blind_indices = totalreclaw_memory::blind::generate_blind_indices(fact_text);
        let content_fp = totalreclaw_memory::fingerprint::generate_content_fingerprint(
            fact_text,
            &keys.dedup_key,
        );

        let payload = totalreclaw_memory::protobuf::FactPayload {
            id: uuid::Uuid::now_v7().to_string(),
            timestamp: chrono::Utc::now()
                .to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            owner: smart_account.clone(),
            encrypted_blob_hex: encrypted_hex,
            blind_indices,
            decay_score: 0.8,
            source: "zeroclaw:batch-e2e".to_string(),
            content_fp,
            agent_id: "batch-e2e".to_string(),
            encrypted_embedding: None,
        };

        protobuf_payloads.push(totalreclaw_memory::protobuf::encode_fact_protobuf(&payload));
        println!("   Fact {}: '{}'", i + 1, fact_text);
    }

    // 5. Submit as BATCH UserOp
    println!("\n2. Submitting {} facts as batch UserOp...", facts.len());
    let result = relay
        .submit_fact_batch_native(&protobuf_payloads, &eth_wallet.private_key)
        .await
        .expect("Batch UserOp submission failed");

    println!(
        "   Batch submitted: txHash={} success={}",
        result.tx_hash, result.success
    );
    assert!(result.success, "Batch UserOp should succeed");

    // 6. Wait for indexing
    println!(
        "\n3. Waiting {}s for subgraph indexing...",
        INDEXING_WAIT_SECS
    );
    tokio::time::sleep(std::time::Duration::from_secs(INDEXING_WAIT_SECS)).await;

    // 7. Recall all 3 facts
    println!("\n4. Recalling via blind index search...");
    let trapdoors =
        totalreclaw_memory::blind::generate_blind_indices("batch test fact");

    let candidates =
        search::search_candidates(&relay, &smart_account, &trapdoors, 100)
            .await
            .unwrap_or_default();

    println!("   Found {} candidates", candidates.len());

    let mut found_count = 0;
    for fact in &candidates {
        if let Some(b64) = search::hex_blob_to_base64(&fact.encrypted_blob) {
            if let Ok(text) = crypto::decrypt(&b64, &keys.encryption_key) {
                println!("   Decrypted: {}", text);
                if text.contains(&ts.to_string())
                    || text.contains(&(ts + 1).to_string())
                    || text.contains(&(ts + 2).to_string())
                {
                    found_count += 1;
                }
            }
        }
    }

    assert!(
        found_count >= 3,
        "Should find all 3 batch-stored facts, found {}",
        found_count
    );
    println!("\nBatch UserOp E2E PASSED! Found {}/3 facts.", found_count);
}
