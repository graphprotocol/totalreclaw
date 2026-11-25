//! graphprotocol/totalreclaw#789: the on-chain decayScore rule (Rust leg).
//!
//! Loads `tests/parity/fixtures/decay-score-v1.json` (repo root): `0` =
//! tombstone, `1.0` = live, no importance. Siblings:
//! tests/parity/decay-score-parity.test.ts (TS: WASM, MCP, plugin),
//! tests/parity/test_decay_score_parity.py (Python).
use serde_json::Value;
use totalreclaw_core::{crypto, lsh::LshHasher, store};

const FIXTURE: &str = include_str!("../../../tests/parity/fixtures/decay-score-v1.json");
const MAPPING_TS: &str = include_str!("../../../subgraph/src/mapping.ts");

const MNEMONIC: &str = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";

fn fixture() -> Value {
    serde_json::from_str(FIXTURE).expect("decay-score-v1.json parses")
}

fn importance_of(v: &Value) -> f64 {
    match v {
        Value::Number(n) => n.as_f64().unwrap(),
        Value::String(s) if s == "NaN" => f64::NAN,
        Value::String(s) if s == "Infinity" => f64::INFINITY,
        Value::String(s) if s == "-Infinity" => f64::NEG_INFINITY,
        other => panic!("bad fixture importance {other}"),
    }
}

#[test]
fn live_value_matches_fixture() {
    let fx = fixture();
    assert_eq!(store::live_decay_score(), fx["live"].as_f64().unwrap());
    assert_eq!(store::LIVE_DECAY_SCORE, 1.0);
}

#[test]
fn tombstone_is_zero() {
    let fx = fixture();
    assert_eq!(fx["tombstone"].as_f64().unwrap(), 0.0);
}

#[test]
fn prepare_fact_ignores_importance() {
    let fx = fixture();
    let keys = crypto::derive_keys_from_mnemonic(MNEMONIC).unwrap();
    let seed = crypto::derive_lsh_seed(MNEMONIC, &keys.salt).unwrap();
    let hasher = LshHasher::new(&seed, 640).unwrap();
    let mut emb = vec![0.0f32; 640];
    emb[0] = 1.0;
    let envelope = r#"{"v":1,"t":"x"}"#;
    for v in fx["importances_ignored"].as_array().unwrap() {
        let imp = importance_of(v);
        let a = store::prepare_fact("x", &keys.encryption_key, &keys.dedup_key, &hasher, &emb, imp, "t", "0xABCD", "t").unwrap();
        assert_eq!(a.decay_score, 1.0, "prepare_fact importance {v}");
        let b = store::prepare_fact_v1(envelope, "x", &keys.encryption_key, &keys.dedup_key, &hasher, &emb, imp, "t", "0xABCD", "t").unwrap();
        assert_eq!(b.decay_score, 1.0, "prepare_fact_v1 importance {v}");
    }
}

/// Drift guard against the subgraph rule (`isActive = decayScore >= 0.3`).
#[test]
fn threshold_matches_subgraph() {
    let fx = fixture();
    assert!(
        MAPPING_TS.contains("INACTIVE_THRESHOLD = BigDecimal.fromString(\"0.3\")"),
        "subgraph INACTIVE_THRESHOLD changed; update the decay rule"
    );
    assert!(MAPPING_TS.contains("decoded.decayScore.ge(INACTIVE_THRESHOLD)"), "subgraph comparison is no longer `ge`");
    assert_eq!(store::SUBGRAPH_INACTIVE_THRESHOLD, fx["subgraph_threshold"].as_f64().unwrap());
    assert!(store::LIVE_DECAY_SCORE >= store::SUBGRAPH_INACTIVE_THRESHOLD);
}
