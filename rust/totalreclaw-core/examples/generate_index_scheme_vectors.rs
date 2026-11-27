//! Generates the shared outer-protobuf `index_scheme` (field 14) test vectors.
//!
//! Run:
//!   cd rust/totalreclaw-core
//!   cargo run --example generate_index_scheme_vectors            # write
//!   cargo run --example generate_index_scheme_vectors -- --check # verify, no write
//!
//! Writes `tests/parity/fixtures/protobuf-index-scheme-vectors.json` (path
//! relative to the repo root). The subgraph decoder tests consume it today;
//! the relay's UserOp decoder must load the same file so the two parsers
//! cannot disagree (internal#573, design v2.1 section 6).
//!
//! Vector shape:
//!   name, description, payloadHex,
//!   expected: the decoded fields a conforming decoder must report. For
//!     well-formed payloads this is the full field set; for malformed ones it
//!     lists only the keys that are pinned (indexScheme at minimum).
//!     `indexScheme` is the decoded varint of field 14, or 0 when field 14 is
//!     absent, has the wrong wire type, or is truncated.
//!   counts: the total rule. true only when indexScheme == 1.
//!   isTombstone: decayScore == 0 on a payload that counts.

use std::fs;
use std::path::PathBuf;

use serde_json::{json, Value};
use totalreclaw_core::protobuf::{
    encode_fact_protobuf, encode_fact_protobuf_keyed, encode_tombstone_protobuf_keyed_at, FactPayload,
};

const TS: &str = "2026-01-02T03:04:05Z";
const OWNER: &str = "0x1111111111111111111111111111111111111111";
const TAG_A: &str = "00000000000000000000000000000000000000000000000000000000000000aa";
const TAG_B: &str = "00000000000000000000000000000000000000000000000000000000000000bb";

fn fact(id: &str, decay: f64, version: u32) -> FactPayload {
    FactPayload {
        id: id.into(),
        timestamp: TS.into(),
        owner: OWNER.into(),
        encrypted_blob_hex: "deadbeef".into(),
        // Unsorted on purpose: the keyed encoder sorts, the legacy one does not.
        blind_indices: vec![TAG_B.into(), TAG_A.into()],
        decay_score: decay,
        source: "test".into(),
        content_fp: "fp-0001".into(),
        agent_id: "test".into(),
        encrypted_embedding: Some("abcd".into()),
        version,
    }
}

fn full(p: &FactPayload, tags: &[&str], scheme: u32) -> Value {
    json!({
        "id": p.id,
        "timestamp": p.timestamp,
        "owner": p.owner,
        "encryptedBlobHex": p.encrypted_blob_hex,
        "blindIndices": tags,
        "decayScore": p.decay_score,
        "version": if p.version == 0 { 3 } else { p.version },
        "contentFp": p.content_fp,
        "encryptedEmbedding": p.encrypted_embedding.clone().unwrap_or_default(),
        "indexScheme": scheme,
    })
}

fn vector(name: &str, description: &str, bytes: &[u8], expected: Value, tombstone: bool) -> Value {
    let scheme = expected["indexScheme"].as_u64().unwrap_or(0);
    let counts = scheme == 1;
    json!({
        "name": name,
        "description": description,
        "payloadHex": hex::encode(bytes),
        "expected": expected,
        "counts": counts,
        "isTombstone": counts && tombstone,
    })
}

fn tombstone_fields(id: &str, version: u32, scheme: u32) -> Value {
    json!({
        "id": id, "timestamp": TS, "owner": OWNER, "encryptedBlobHex": "",
        "blindIndices": [], "decayScore": 0.0, "version": version,
        "contentFp": "", "encryptedEmbedding": "", "indexScheme": scheme,
    })
}

fn build() -> Value {
    let mut v: Vec<Value> = Vec::new();

    // 1. Marked fact (scheme 1). Field 14 is the last field written.
    let p = fact("fact-marked", 1.0, 4);
    let marked = encode_fact_protobuf_keyed(&p).expect("valid tags");
    v.push(vector(
        "marked_fact",
        "Keyed fact: version 4, field 5 sorted, field 14 = 1.",
        &marked,
        full(&p, &[TAG_A, TAG_B], 1),
        false,
    ));

    // 2. Marked tombstone.
    let t = encode_tombstone_protobuf_keyed_at("fact-marked", OWNER, TS);
    v.push(vector(
        "marked_tombstone",
        "Keyed tombstone: decay 0, empty blob, version 4, field 14 = 1.",
        &t,
        tombstone_fields("fact-marked", 4, 1),
        true,
    ));

    // 3. Legacy unmarked fact (pre-cutover layout, byte for byte).
    let lp = fact("fact-legacy", 1.0, 4);
    let legacy = encode_fact_protobuf(&lp);
    v.push(vector(
        "legacy_unmarked_fact",
        "Legacy fact, version 4, no field 14. Must never count.",
        &legacy,
        full(&lp, &[TAG_B, TAG_A], 0),
        false,
    ));

    // 3b. Legacy low-liveness fact: guards against any liveness shortcut.
    let ll = fact("fact-legacy-low", 0.2, 4);
    v.push(vector(
        "legacy_unmarked_low_decay_fact",
        "Legacy fact with decay 0.2, no field 14. Must never count.",
        &encode_fact_protobuf(&ll),
        full(&ll, &[TAG_B, TAG_A], 0),
        false,
    ));

    // 4. Legacy tombstone.
    // The legacy encoder stamps wall-clock time; pin it by taking the
    // fixed-timestamp keyed tombstone minus field 14 (its last two bytes).
    let lt_fixed = {
        let mut b = encode_tombstone_protobuf_keyed_at("fact-legacy", OWNER, TS);
        b.truncate(b.len() - 2);
        b
    };
    v.push(vector(
        "legacy_unmarked_tombstone",
        "Legacy tombstone, version 4, no field 14. Must have no effect.",
        &lt_fixed,
        tombstone_fields("fact-legacy", 4, 0),
        true,
    ));

    // 5. Field 14 = 2 (a future scheme this decoder does not know).
    let mut two = marked.clone();
    let n = two.len();
    assert_eq!(&two[n - 2..], &[0x70, 0x01], "field 14 varint is the last field");
    two[n - 1] = 0x02;
    v.push(vector(
        "field14_equals_2",
        "Field 14 = 2. Only 1 counts.",
        &two,
        full(&p, &[TAG_A, TAG_B], 2),
        false,
    ));

    // 6. Field 14 present with the wrong wire type (length-delimited, 1 byte 0x01).
    let mut wrong = marked[..n - 2].to_vec();
    wrong.extend_from_slice(&[0x72, 0x01, 0x01]);
    v.push(vector(
        "field14_wrong_wire_type",
        "Field 14 sent as length-delimited. Treated as absent.",
        &wrong,
        json!({ "id": "fact-marked", "version": 4, "indexScheme": 0 }),
        false,
    ));

    // 7. Truncated mid-blob: field 14 never reached.
    let cut = &marked[..30];
    v.push(vector(
        "truncated_mid_field",
        "Payload cut inside an earlier field. Field 14 never arrives.",
        cut,
        json!({ "indexScheme": 0 }),
        false,
    ));

    // 8. Truncated inside the field-14 varint: continuation bit set, no next byte.
    // A naive decoder would read 0x81 as 1; it must not.
    let mut trunc14 = marked[..n - 2].to_vec();
    trunc14.extend_from_slice(&[0x70, 0x81]);
    v.push(vector(
        "truncated_field14_varint",
        "Field 14 varint cut after a continuation byte (0x81). Must not decode as 1.",
        &trunc14,
        json!({ "indexScheme": 0 }),
        false,
    ));

    json!({
        "description": "Outer protobuf index_scheme (field 14) vectors. Generated by rust/totalreclaw-core/examples/generate_index_scheme_vectors.rs. Do not edit by hand.",
        "version": 1,
        "countsRule": "A payload counts only when field 14 is a varint equal to 1.",
        "vectors": v,
    })
}

fn main() {
    let check = std::env::args().any(|a| a == "--check");
    let out = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/parity/fixtures/protobuf-index-scheme-vectors.json");
    let text = serde_json::to_string_pretty(&build()).unwrap() + "\n";
    if check {
        let cur = fs::read_to_string(&out).expect("fixture missing");
        if cur != text {
            eprintln!("{} is stale: re-run the generator", out.display());
            std::process::exit(1);
        }
        println!("ok: {} is up to date", out.display());
    } else {
        fs::write(&out, text).unwrap();
        println!("wrote {}", out.display());
    }
}
