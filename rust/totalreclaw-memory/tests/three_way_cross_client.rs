//! Three-Way Cross-Client E2E Test: Rust ↔ TypeScript ↔ Python
//!
//! Proves facts stored by ANY implementation can be recalled by ANY other.
//! Uses a fresh BIP-39 mnemonic so each test run starts clean.
//!
//! Flow:
//!   1. Generate fresh BIP-39 mnemonic
//!   2. Register with relay (all clients use same mnemonic = same wallet)
//!   3. Rust stores fact A on-chain
//!   4. TypeScript stores fact B on-chain
//!   5. Python stores fact C on-chain
//!   6. Wait for subgraph indexing (45s)
//!   7. Rust recalls all 3 facts ← proves Rust reads TS + Python data
//!   8. TypeScript recalls all 3 facts ← proves TS reads Rust + Python data
//!   9. Python recalls all 3 facts ← proves Python reads Rust + TS data
//!
//! All requests include X-TotalReclaw-Test: true header.
//!
//! Run: cargo test --test three_way_cross_client -- --ignored --nocapture

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use totalreclaw_memory::crypto;
use totalreclaw_memory::search;
use totalreclaw_memory::setup;

const RELAY_URL: &str = "https://api.totalreclaw.xyz";
const MCP_DIR: &str = "../../mcp";
const INDEXING_WAIT_SECS: u64 = 45;

#[tokio::test]
#[ignore] // Requires staging relay + Node.js + Python
async fn test_three_way_cross_client() {
    println!("=== Three-Way Cross-Client E2E Test ===\n");

    // 1. Generate fresh mnemonic
    let mnemonic = setup::generate_mnemonic();
    println!("1. Generated test mnemonic: {}...", &mnemonic[..25]);

    // 2. Derive keys and register
    let keys = crypto::derive_keys_from_mnemonic(&mnemonic).expect("Key derivation failed");
    let auth_key_hex = hex::encode(keys.auth_key);
    let auth_key_hash = crypto::compute_auth_key_hash(&keys.auth_key);
    let salt_hex = hex::encode(keys.salt);

    let relay = totalreclaw_memory::relay::RelayClient::new(
        totalreclaw_memory::relay::RelayConfig {
            relay_url: RELAY_URL.to_string(),
            auth_key_hex: auth_key_hex.clone(),
            wallet_address: String::new(),
            is_test: true,
        },
    );

    println!("2. Registering with relay...");
    let user_id = relay
        .register(&auth_key_hash, &salt_hex)
        .await
        .expect("Registration failed");
    println!("   Registered: {}", user_id);

    let wallet = relay
        .resolve_address(&auth_key_hex)
        .await
        .expect("Address resolution failed");
    println!("   Smart Account: {}", wallet);

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();

    // 3. Rust stores fact A
    let fact_a = format!("Three-way test: Rust stored this fact at {}", ts);
    println!("\n3. Rust storing: '{}'", fact_a);
    let rust_store_result = store_via_rust(&mnemonic, &fact_a).await;
    println!(
        "   Rust store: {}",
        if rust_store_result { "OK" } else { "FAILED" }
    );

    // 4. TypeScript stores fact B
    let fact_b = format!("Three-way test: TypeScript stored this fact at {}", ts + 1);
    println!("\n4. TypeScript storing: '{}'", fact_b);
    let ts_output = run_ts_store(&mnemonic, &wallet, &fact_b);
    let ts_store_ok = ts_output.contains("success=true") || ts_output.contains("STORED:");
    println!(
        "   TS store: {} (output: {})",
        if ts_store_ok { "OK" } else { "FAILED" },
        ts_output.lines().last().unwrap_or("(empty)")
    );

    // 5. Python stores fact C
    let fact_c = format!("Three-way test: Python stored this fact at {}", ts + 2);
    println!("\n5. Python storing: '{}'", fact_c);
    let py_output = run_python_store(&mnemonic, &fact_c);
    let py_store_ok = py_output.contains("STORED");
    println!(
        "   Python store: {} (output: {})",
        if py_store_ok { "OK" } else { "FAILED" },
        py_output.lines().last().unwrap_or("(empty)")
    );

    // 6. Wait for indexing
    println!(
        "\n6. Waiting {}s for subgraph indexing...",
        INDEXING_WAIT_SECS
    );
    tokio::time::sleep(std::time::Duration::from_secs(INDEXING_WAIT_SECS)).await;

    // 7. Rust recalls
    println!("\n7. Rust recalling all facts...");
    let rust_recall = recall_via_rust(&mnemonic, &wallet, &auth_key_hex).await;
    let rust_a = rust_recall.iter().any(|t| t.contains(&ts.to_string()));
    let rust_b = rust_recall
        .iter()
        .any(|t| t.contains(&(ts + 1).to_string()));
    let rust_c = rust_recall
        .iter()
        .any(|t| t.contains(&(ts + 2).to_string()));
    println!("   Found {} results", rust_recall.len());
    for r in &rust_recall {
        println!("     {}", &r[..r.len().min(80)]);
    }
    println!(
        "   Rust reads own (A): {}",
        if rust_a { "PASS" } else { "FAIL" }
    );
    println!(
        "   Rust reads TS (B):  {}",
        if rust_b { "PASS" } else { "FAIL" }
    );
    println!(
        "   Rust reads Py (C):  {}",
        if rust_c { "PASS" } else { "FAIL" }
    );

    // 8. TypeScript recalls
    println!("\n8. TypeScript recalling...");
    let ts_recall = run_ts_recall(&mnemonic, &wallet);
    let ts_a = ts_recall.contains(&ts.to_string());
    let ts_b = ts_recall.contains(&(ts + 1).to_string());
    let ts_c = ts_recall.contains(&(ts + 2).to_string());
    println!(
        "   TS reads Rust (A): {}",
        if ts_a { "PASS" } else { "FAIL" }
    );
    println!(
        "   TS reads own (B):  {}",
        if ts_b { "PASS" } else { "FAIL" }
    );
    println!(
        "   TS reads Py (C):   {}",
        if ts_c { "PASS" } else { "FAIL" }
    );

    // 9. Python recalls
    println!("\n9. Python recalling...");
    let py_recall = run_python_recall(&mnemonic);
    let py_a = py_recall.contains(&ts.to_string());
    let py_b = py_recall.contains(&(ts + 1).to_string());
    let py_c = py_recall.contains(&(ts + 2).to_string());
    println!(
        "   Py reads Rust (A): {}",
        if py_a { "PASS" } else { "FAIL" }
    );
    println!(
        "   Py reads TS (B):   {}",
        if py_b { "PASS" } else { "FAIL" }
    );
    println!(
        "   Py reads own (C):  {}",
        if py_c { "PASS" } else { "FAIL" }
    );

    // Summary
    println!("\n{}", "=".repeat(60));
    println!("THREE-WAY CROSS-CLIENT RESULTS:");
    println!(
        "  Rust   reads: A={} B={} C={}",
        p(rust_a),
        p(rust_b),
        p(rust_c)
    );
    println!("  TS     reads: A={} B={} C={}", p(ts_a), p(ts_b), p(ts_c));
    println!("  Python reads: A={} B={} C={}", p(py_a), p(py_b), p(py_c));
    println!("{}", "=".repeat(60));

    let all_pass = rust_a && rust_b && rust_c && ts_a && ts_b && ts_c && py_a && py_b && py_c;

    if all_pass {
        println!("\nAll 9 cross-client assertions PASSED!");
    } else {
        panic!("Some cross-client tests FAILED — see results above");
    }
}

fn p(ok: bool) -> &'static str {
    if ok {
        "PASS"
    } else {
        "FAIL"
    }
}

/// Store a fact using the Rust crypto pipeline + relay GraphQL.
async fn store_via_rust(mnemonic: &str, fact_text: &str) -> bool {
    // For this test, we need an embedding. Use a dummy zero-vector since
    // the cross-client recall test uses blind indices (word search), not LSH.
    let keys = crypto::derive_keys_from_mnemonic(mnemonic).unwrap();
    let auth_key_hex = hex::encode(keys.auth_key);
    let auth_key_hash = crypto::compute_auth_key_hash(&keys.auth_key);
    let salt_hex = hex::encode(keys.salt);

    let relay = totalreclaw_memory::relay::RelayClient::new(
        totalreclaw_memory::relay::RelayConfig {
            relay_url: RELAY_URL.to_string(),
            auth_key_hex: auth_key_hex.clone(),
            wallet_address: String::new(),
            is_test: true,
        },
    );

    // Register + resolve
    let _ = relay.register(&auth_key_hash, &salt_hex).await;
    let wallet = relay.resolve_address(&auth_key_hex).await.unwrap_or_default();

    // Encrypt
    let encrypted_b64 = crypto::encrypt(fact_text, &keys.encryption_key).unwrap();
    let encrypted_bytes = base64::Engine::decode(
        &base64::engine::general_purpose::STANDARD,
        &encrypted_b64,
    )
    .unwrap();
    let encrypted_hex = hex::encode(&encrypted_bytes);

    // Blind indices (word-based, no LSH for this test)
    let blind_indices = totalreclaw_memory::blind::generate_blind_indices(fact_text);

    // Content fingerprint
    let content_fp =
        totalreclaw_memory::fingerprint::generate_content_fingerprint(fact_text, &keys.dedup_key);

    // Build protobuf
    let fact_id = uuid::Uuid::now_v7().to_string();
    let timestamp = chrono::Utc::now()
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let payload = totalreclaw_memory::protobuf::FactPayload {
        id: fact_id,
        timestamp,
        owner: wallet,
        encrypted_blob_hex: encrypted_hex,
        blind_indices,
        decay_score: 0.8,
        source: "zeroclaw:cross-client-e2e".to_string(),
        content_fp,
        agent_id: "cross-client-e2e".to_string(),
        encrypted_embedding: None,
    };

    let protobuf = totalreclaw_memory::protobuf::encode_fact_protobuf(&payload);
    relay.submit_protobuf(&protobuf).await.is_ok()
}

/// Recall facts via Rust — query subgraph and decrypt.
async fn recall_via_rust(mnemonic: &str, wallet: &str, auth_key_hex: &str) -> Vec<String> {
    let keys = crypto::derive_keys_from_mnemonic(mnemonic).unwrap();

    let relay = totalreclaw_memory::relay::RelayClient::new(
        totalreclaw_memory::relay::RelayConfig {
            relay_url: RELAY_URL.to_string(),
            auth_key_hex: auth_key_hex.to_string(),
            wallet_address: wallet.to_string(),
            is_test: true,
        },
    );

    // Use word trapdoors for the query
    let trapdoors =
        totalreclaw_memory::blind::generate_blind_indices("three-way test stored fact");

    let candidates = search::search_candidates(&relay, wallet, &trapdoors, 100)
        .await
        .unwrap_or_default();

    let mut texts = Vec::new();
    for fact in &candidates {
        if let Some(b64) = search::hex_blob_to_base64(&fact.encrypted_blob) {
            if let Ok(text) = crypto::decrypt(&b64, &keys.encryption_key) {
                texts.push(text);
            }
        }
    }
    texts
}

// ---------------------------------------------------------------------------
// TypeScript helpers (shell out to npx tsx, same pattern as cross_client_e2e.py)
// ---------------------------------------------------------------------------

fn run_ts_store(mnemonic: &str, wallet: &str, fact_text: &str) -> String {
    let script = format!(
        r#"
import {{ deriveKeysFromMnemonic, encrypt, generateBlindIndices, generateContentFingerprint }} from './src/subgraph/crypto.ts';
import {{ encodeFactProtobuf, submitFactOnChain }} from './src/subgraph/store.ts';
import {{ randomUUID }} from 'node:crypto';

const mnemonic = process.env.TEST_MNEMONIC!;

async function main() {{
    const keys = deriveKeysFromMnemonic(mnemonic);
    const authKeyHex = Buffer.from(keys.authKey).toString('hex');
    const owner = "{}";
    const factText = {};

    const encryptedB64 = encrypt(factText, keys.encryptionKey);
    const encryptedHex = Buffer.from(encryptedB64, "base64").toString("hex");
    const blindIndices = generateBlindIndices(factText);
    const contentFp = generateContentFingerprint(factText, keys.dedupKey);

    const factId = randomUUID();
    const timestamp = new Date().toISOString();

    const payload = {{
        id: factId, timestamp, owner,
        encryptedBlob: encryptedHex,
        blindIndices, decayScore: 0.8,
        source: "mcp-server:cross-client-e2e",
        contentFp, agentId: "cross-client-e2e",
    }};

    const protobuf = encodeFactProtobuf(payload);
    const config = {{
        relayUrl: "{RELAY_URL}",
        mnemonic, cachePath: "/tmp/tr-cross-test.enc",
        chainId: 84532,
        dataEdgeAddress: "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca",
        entryPointAddress: "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
        authKeyHex, walletAddress: owner,
    }};

    console.log(`SUBMITTING: ${{factId}}`);
    const result = await submitFactOnChain(protobuf, config);
    console.log(`STORED: txHash=${{result.txHash}} success=${{result.success}}`);
}}

main().catch(e => {{ console.error(e.message || e); process.exit(1); }});
"#,
        wallet,
        serde_json::to_string(fact_text).unwrap(),
        RELAY_URL = RELAY_URL,
    );

    run_ts_script(&script, mnemonic)
}

fn run_ts_recall(mnemonic: &str, wallet: &str) -> String {
    let script = format!(
        r#"
import {{ deriveKeysFromMnemonic, decrypt, generateBlindIndices }} from './src/subgraph/crypto.ts';

const mnemonic = process.env.TEST_MNEMONIC!;

async function main() {{
    const keys = deriveKeysFromMnemonic(mnemonic);
    const authKeyHex = Buffer.from(keys.authKey).toString('hex');
    const trapdoors = generateBlindIndices("three-way test stored fact");

    const resp = await fetch("{RELAY_URL}/v1/subgraph", {{
        method: "POST",
        headers: {{
            "Content-Type": "application/json",
            "Authorization": `Bearer ${{authKeyHex}}`,
            "X-TotalReclaw-Client": "mcp-server:cross-client-e2e",
            "X-TotalReclaw-Test": "true",
        }},
        body: JSON.stringify({{
            query: `query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {{
                blindIndexes(
                    where: {{ hash_in: $trapdoors, owner: $owner, fact_: {{ isActive: true }} }}
                    first: $first orderBy: id orderDirection: desc
                ) {{ id fact {{ id encryptedBlob isActive }} }}
            }}`,
            variables: {{ trapdoors: trapdoors.slice(0, 20), owner: "{wallet}", first: 100 }},
        }}),
    }});

    const json = await resp.json() as any;
    const entries = json?.data?.blindIndexes || [];
    const seen = new Set<string>();

    for (const entry of entries) {{
        const fact = entry.fact;
        if (fact && fact.isActive && !seen.has(fact.id)) {{
            seen.add(fact.id);
            let blob = fact.encryptedBlob;
            if (blob.startsWith("0x")) blob = blob.slice(2);
            try {{
                const b64 = Buffer.from(blob, "hex").toString("base64");
                const text = decrypt(b64, keys.encryptionKey);
                console.log(`DECRYPTED: ${{text}}`);
            }} catch {{}}
        }}
    }}
}}

main().catch(e => {{ console.error(e); process.exit(1); }});
"#,
        RELAY_URL = RELAY_URL,
        wallet = wallet,
    );

    run_ts_script(&script, mnemonic)
}

fn run_ts_script(script: &str, mnemonic: &str) -> String {
    let script_path = format!("{MCP_DIR}/_cross_client_e2e.ts");
    std::fs::write(&script_path, script).expect("Failed to write TS script");

    let output = Command::new("npx")
        .args(["tsx", &script_path])
        .env("TEST_MNEMONIC", mnemonic)
        .current_dir(MCP_DIR)
        .output();

    let _ = std::fs::remove_file(&script_path);

    match output {
        Ok(o) => {
            if !o.status.success() {
                let stderr = String::from_utf8_lossy(&o.stderr);
                eprintln!("TS stderr: {}", &stderr[..stderr.len().min(500)]);
            }
            String::from_utf8_lossy(&o.stdout).to_string()
        }
        Err(e) => format!("ERROR: {}", e),
    }
}

// ---------------------------------------------------------------------------
// Python helpers
// ---------------------------------------------------------------------------

fn run_python_store(mnemonic: &str, fact_text: &str) -> String {
    let script = format!(
        r#"
import asyncio, sys
sys.path.insert(0, '../../python/src')
from totalreclaw.client import TotalReclaw
async def main():
    c = TotalReclaw(mnemonic="{mnemonic}", relay_url="{RELAY_URL}", is_test=True)
    await c.resolve_address()
    await c.register()
    fid = await c.remember("{fact}", importance=0.8)
    print(f"STORED: {{fid}}")
    await c.close()
asyncio.run(main())
"#,
        mnemonic = mnemonic,
        RELAY_URL = RELAY_URL,
        fact = fact_text.replace('"', r#"\""#),
    );

    run_python(&script)
}

fn run_python_recall(mnemonic: &str) -> String {
    let script = format!(
        r#"
import asyncio, sys
sys.path.insert(0, '../../python/src')
from totalreclaw.client import TotalReclaw
async def main():
    c = TotalReclaw(mnemonic="{mnemonic}", relay_url="{RELAY_URL}", is_test=True)
    await c.resolve_address()
    results = await c.recall("three-way test stored fact")
    for r in results:
        print(f"RECALLED: {{r.text}}")
    await c.close()
asyncio.run(main())
"#,
        mnemonic = mnemonic,
        RELAY_URL = RELAY_URL,
    );

    run_python(&script)
}

fn run_python(script: &str) -> String {
    let output = Command::new("python3")
        .args(["-c", script])
        .output();

    match output {
        Ok(o) => {
            if !o.status.success() {
                let stderr = String::from_utf8_lossy(&o.stderr);
                eprintln!("Python stderr: {}", &stderr[..stderr.len().min(500)]);
            }
            String::from_utf8_lossy(&o.stdout).to_string()
        }
        Err(e) => format!("ERROR: {}", e),
    }
}
