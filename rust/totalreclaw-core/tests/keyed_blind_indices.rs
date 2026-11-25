//! Known-answer tests for keyed blind indices (index scheme 1, internal#573).
//!
//! Fixture: `tests/parity/fixtures/keyed-blind-indices-v1.json`, written by the
//! independent generator `generate-keyed-blind-indices-v1.py` (standard-library
//! Python). The WASM/TS and PyO3 legs read the same file.
//!
//! Key vectors (`indexKey`, `lshBucketKey`, `fpKeyV2`) are checked inside
//! `src/crypto.rs`, because those keys are `pub(crate)` and never leave core.

use std::collections::HashSet;

use serde_json::Value;
use sha2::{Digest, Sha256};
use totalreclaw_core::blind::{sort_dedup_tags, BlindIndexer};
use totalreclaw_core::fingerprint::{
    generate_content_fingerprint, generate_content_fingerprint_v2, normalize_text_v2,
};
use totalreclaw_core::lsh::LshHasher;
use totalreclaw_core::protobuf::{
    encode_fact_protobuf, encode_fact_protobuf_keyed, encode_tombstone_protobuf,
    encode_tombstone_protobuf_keyed_at, FactPayload,
};

const FIXTURE: &str = include_str!("../../../tests/parity/fixtures/keyed-blind-indices-v1.json");

fn fx() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture parses")
}

fn key32(v: &Value) -> [u8; 32] {
    hex::decode(v.as_str().unwrap()).unwrap().try_into().unwrap()
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array().unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect()
}

fn indexer() -> BlindIndexer {
    BlindIndexer::new(&key32(&fx()["meta"]["encryption_key_hex"])).unwrap()
}

fn lsh_hasher(l: &Value) -> LshHasher {
    let seed = hex::decode(fx()["meta"]["lsh_seed_hex"].as_str().unwrap()).unwrap();
    LshHasher::with_params(
        &seed,
        l["dims"].as_u64().unwrap() as usize,
        l["n_tables"].as_u64().unwrap() as usize,
        l["n_bits"].as_u64().unwrap() as usize,
    )
    .unwrap()
}

fn lsh_embedding(l: &Value) -> Vec<f64> {
    l["embedding"].as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect()
}

fn sha256_hex(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}

fn is_sorted_unique(tags: &[String]) -> bool {
    tags.windows(2).all(|w| w[0] < w[1])
}

#[test]
fn stem_tags_match_frozen_vectors() {
    let f = fx();
    let ix = indexer();
    for v in f["stem_vectors"].as_array().unwrap() {
        let name = v["name"].as_str().unwrap();
        let got = ix.stem_tags(v["text"].as_str().unwrap());
        assert_eq!(got, strings(&v["tags"]), "stem vector {name}");
        assert!(is_sorted_unique(&got), "{name}: sorted and deduplicated");
    }
}

#[test]
fn nfc_and_nfd_input_give_identical_tags() {
    let ix = indexer();
    assert_eq!(ix.stem_tags("caf\u{e9}"), ix.stem_tags("cafe\u{301}"));
    assert_eq!(ix.entity_tag("Caf\u{e9}"), ix.entity_tag("Cafe\u{301}"));
}

#[test]
fn quirks_are_pinned() {
    let ix = indexer();
    // U+0130 lowercases to "i" + U+0307; the combining dot becomes a space, so only "stanbul" survives.
    assert_eq!(ix.stem_tags("\u{130}stanbul"), ix.stem_tags("stanbul"));
    // "ies" stems to "i" (< 2 bytes): the token itself is tagged.
    assert_eq!(ix.stem_tags("ies"), ix.stem_tags("ies ies"));
    assert!(ix.stem_tags("").is_empty());
    assert!(ix.stem_tags("a \u{2764}\u{fe0f}").is_empty());
}

#[test]
fn entity_marker_and_separation_vectors() {
    let f = fx();
    let ix = indexer();
    for v in f["entity_vectors"].as_array().unwrap() {
        // The RAW name goes in; normalisation happens in core.
        assert_eq!(ix.entity_tag(v["name"].as_str().unwrap()), v["tag"].as_str().unwrap());
    }
    // Whitespace and case variants of one entity share one tag.
    assert_eq!(ix.entity_tag("PostgreSQL"), ix.entity_tag("postgresql"));
    assert_eq!(ix.entity_tag("  postgre   sql "), ix.entity_tag("Postgre SQL"));
    assert_eq!(ix.digest_marker_tag(), f["marker"]["tag"].as_str().unwrap());

    // Same value under the three domains gives three different tags.
    let sep = &f["separation"];
    assert_eq!(sep["value"].as_str().unwrap(), "digest");
    assert_eq!(ix.stem_tags("digest")[0], sep["stem"].as_str().unwrap());
    assert_eq!(ix.entity_tag("digest"), sep["entity"].as_str().unwrap());
    assert_eq!(ix.digest_marker_tag(), sep["type"].as_str().unwrap());
    let all: HashSet<&str> =
        [sep["stem"].as_str().unwrap(), sep["entity"].as_str().unwrap(), sep["type"].as_str().unwrap()].into();
    assert_eq!(all.len(), 3);
}

#[test]
fn two_vaults_share_no_tag() {
    let f = fx();
    let other = BlindIndexer::new(&key32(&f["meta"]["second_encryption_key_hex"])).unwrap();
    let ix = indexer();
    let text = "prefers dark mode and PostgreSQL";
    let a: HashSet<String> = ix.stem_tags(text).into_iter().collect();
    let b: HashSet<String> = other.stem_tags(text).into_iter().collect();
    assert!(a.is_disjoint(&b));
    assert_ne!(ix.entity_tag("postgres"), other.entity_tag("postgres"));
    assert_ne!(ix.digest_marker_tag(), other.digest_marker_tag());
}

#[test]
fn lsh_keyed_tags_match_frozen_vectors() {
    let f = fx();
    let l = &f["lsh"];
    let h = lsh_hasher(l);
    let emb = lsh_embedding(l);
    let keyed = h.hash_keyed(&emb).unwrap();
    assert_eq!(keyed, strings(&l["keyed_tags"]));
    assert_eq!(keyed.len(), 20, "tables 0, 9, 10 and 19 are all covered");
    // Legacy hash() is unchanged and differs from the keyed tags.
    assert_eq!(h.hash(&emb).unwrap(), strings(&l["legacy_sha256_tags"]));
    let legacy: HashSet<String> = strings(&l["legacy_sha256_tags"]).into_iter().collect();
    assert!(keyed.iter().all(|t| !legacy.contains(t)));
}

#[test]
fn content_fingerprint_v2_vectors() {
    let f = fx();
    let key = key32(&f["meta"]["dedup_key_hex"]);
    for v in f["fp_v2"].as_array().unwrap() {
        let text = v["text"].as_str().unwrap();
        assert_eq!(normalize_text_v2(text), v["normalized"].as_str().unwrap());
        assert_eq!(generate_content_fingerprint_v2(text, &key).unwrap(), v["fp"].as_str().unwrap());
    }
    let a = generate_content_fingerprint_v2("User prefers dark mode", &key).unwrap();
    let b = generate_content_fingerprint_v2("user  prefers DARK mode ", &key).unwrap();
    assert_eq!(a, b);
    // v1 is untouched and is a different value.
    assert_ne!(a, generate_content_fingerprint("User prefers dark mode", &key));
}

fn payload_from(v: &Value) -> FactPayload {
    let s = |k: &str| v[k].as_str().unwrap().to_string();
    FactPayload {
        id: s("id"),
        timestamp: s("timestamp"),
        owner: s("owner"),
        encrypted_blob_hex: s("encrypted_blob_hex"),
        blind_indices: strings(&v["blind_indices"]),
        decay_score: v["decay_score"].as_f64().unwrap(),
        source: s("source"),
        content_fp: s("content_fp"),
        agent_id: s("agent_id"),
        encrypted_embedding: v["encrypted_embedding"].as_str().map(str::to_string),
        version: v["version"].as_u64().unwrap() as u32,
    }
}

/// Walk the outer protobuf; returns (field number, payload bytes for length-delimited fields).
fn walk(buf: &[u8]) -> Vec<(u32, Vec<u8>)> {
    let (mut i, mut out) = (0, Vec::new());
    let varint = |i: &mut usize| {
        let (mut v, mut shift) = (0u64, 0);
        loop {
            let b = buf[*i];
            *i += 1;
            v |= u64::from(b & 0x7f) << shift;
            if b & 0x80 == 0 {
                return v;
            }
            shift += 7;
        }
    };
    while i < buf.len() {
        let key = varint(&mut i);
        let (field, wire) = ((key >> 3) as u32, key & 7);
        match wire {
            0 => {
                let v = varint(&mut i);
                out.push((field, v.to_le_bytes().to_vec()));
            }
            1 => {
                out.push((field, buf[i..i + 8].to_vec()));
                i += 8;
            }
            2 => {
                let len = varint(&mut i) as usize;
                out.push((field, buf[i..i + len].to_vec()));
                i += len;
            }
            _ => panic!("unexpected wire type"),
        }
    }
    out
}

#[test]
fn keyed_fact_wire_bytes_match_frozen_vector() {
    let f = fx();
    let w = &f["wire"]["fact"];
    let bytes = encode_fact_protobuf_keyed(&payload_from(&w["input"])).unwrap();
    assert_eq!(hex::encode(&bytes), w["expected_hex"].as_str().unwrap());
    let fields: Vec<u32> = walk(&bytes).iter().map(|(n, _)| *n).collect();
    assert_eq!(*fields.last().unwrap(), 14, "field 14 is written last");
}

#[test]
fn keyed_tombstone_wire_bytes_match_frozen_vector() {
    let f = fx();
    let t = &f["wire"]["tombstone"];
    let bytes = encode_tombstone_protobuf_keyed_at(
        t["fact_id"].as_str().unwrap(),
        t["owner"].as_str().unwrap(),
        t["timestamp"].as_str().unwrap(),
    );
    assert_eq!(hex::encode(&bytes), t["expected_hex"].as_str().unwrap());
    let field14: Vec<_> = walk(&bytes).into_iter().filter(|(n, _)| *n == 14).collect();
    assert_eq!(field14.len(), 1);
    assert_eq!(field14[0].1[0], 1);
}

#[test]
fn legacy_encoders_are_unchanged_and_carry_no_field_14() {
    let f = fx();
    let fact = payload_from(&f["wire"]["fact"]["input"]);
    let legacy = encode_fact_protobuf(&fact);
    assert!(walk(&legacy).iter().all(|(n, _)| *n != 14));
    // Legacy keeps the caller's order and duplicates (byte-identical to before).
    let order: Vec<String> =
        walk(&legacy).into_iter().filter(|(n, _)| *n == 5).map(|(_, v)| String::from_utf8(v).unwrap()).collect();
    assert_eq!(order, fact.blind_indices);
    let tomb = encode_tombstone_protobuf("id", "0xABCD", 4);
    assert!(walk(&tomb).iter().all(|(n, _)| *n != 14));
}

#[test]
fn keyed_encoder_sorts_and_dedups_field_5_for_any_input_order() {
    let f = fx();
    let base = payload_from(&f["wire"]["fact"]["input"]);
    let mut tags: Vec<String> = strings(&f["lsh"]["keyed_tags"]);
    tags.extend(strings(&f["stem_vectors"][0]["tags"]));
    tags.push(tags[0].clone()); // a duplicate
    let mut state = 0x9e37_79b9_7f4a_7c15u64; // deterministic xorshift shuffle
    for _ in 0..200 {
        for i in (1..tags.len()).rev() {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            tags.swap(i, (state % (i as u64 + 1)) as usize);
        }
        let mut p = base.clone();
        p.blind_indices = tags.clone();
        let out: Vec<String> = walk(&encode_fact_protobuf_keyed(&p).unwrap())
            .into_iter()
            .filter(|(n, _)| *n == 5)
            .map(|(_, v)| String::from_utf8(v).unwrap())
            .collect();
        assert!(is_sorted_unique(&out));
        assert_eq!(out, sort_dedup_tags(tags.clone()));
    }
}

#[test]
fn no_keyed_output_equals_a_legacy_sha256_value() {
    let f = fx();
    let legacy = &f["legacy_sha256"];
    let mut banned: HashSet<String> = HashSet::new();
    for k in ["tokens", "stems", "entities", "lsh"] {
        banned.extend(strings(&legacy[k]));
    }
    banned.insert(legacy["marker"].as_str().unwrap().to_string());
    let ix = indexer();
    let mut produced: Vec<String> = Vec::new();
    for v in f["stem_vectors"].as_array().unwrap() {
        produced.extend(ix.stem_tags(v["text"].as_str().unwrap()));
        // Independent legacy values: sha256 of every token and of "stem:"+stem.
        for tok in strings(&v["tokens"]) {
            assert!(banned.contains(&sha256_hex(&tok)) || tok.is_empty());
        }
    }
    for v in f["entity_vectors"].as_array().unwrap() {
        produced.push(ix.entity_tag(v["name"].as_str().unwrap()));
    }
    produced.push(ix.digest_marker_tag());
    produced.extend(lsh_hasher(&f["lsh"]).hash_keyed(&lsh_embedding(&f["lsh"])).unwrap());
    assert!(!produced.is_empty());
    for tag in &produced {
        assert_eq!(tag.len(), 64);
        assert!(tag.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
        assert!(!banned.contains(tag), "a keyed tag equals a legacy sha256 value");
    }
}

#[test]
fn stems_only_finds_every_candidate_that_word_plus_stem_found() {
    // Word hits are a subset of stem hits: any fact the legacy word+stem index
    // would return for a query is also returned by the stem tags alone.
    let ix = indexer();
    let facts = [
        "User prefers dark mode in all applications",
        "Project deadline is March 15th 2025",
        "User lives in Lisbon and likes running",
        "The ties were red",
    ];
    let queries = ["preferences", "deadlines march", "run", "lisbon", "tie", "dark", "ies", "2025"];
    let legacy = |t: &str| -> HashSet<String> { totalreclaw_core::blind::generate_blind_indices(t).into_iter().collect() };
    for q in queries {
        for fact in facts {
            let legacy_hit = !legacy(q).is_disjoint(&legacy(fact));
            let q_tags: HashSet<String> = ix.stem_tags(q).into_iter().collect();
            let f_tags: HashSet<String> = ix.stem_tags(fact).into_iter().collect();
            if legacy_hit {
                assert!(!q_tags.is_disjoint(&f_tags), "query {q:?} lost fact {fact:?}");
            }
        }
    }
}

#[test]
fn store_tags_meet_query_trapdoors_and_pipeline_is_keyed() {
    use totalreclaw_core::search::generate_search_trapdoors_keyed;
    use totalreclaw_core::store::{prepare_fact_v1_keyed, prepare_tombstone_v1_keyed};

    let f = fx();
    let enc = key32(&f["meta"]["encryption_key_hex"]);
    let ded = key32(&f["meta"]["dedup_key_hex"]);
    let seed = hex::decode(f["meta"]["lsh_seed_hex"].as_str().unwrap()).unwrap();
    let hasher = LshHasher::new(&seed, 640).unwrap();
    let embedding: Vec<f32> = (0..640).map(|i| ((i as f32) * 0.1).sin() * 0.5).collect();
    let ix = indexer();
    let entities = vec!["PostgreSQL".to_string()];

    let p = prepare_fact_v1_keyed(
        r#"{"t":"x"}"#, "User prefers dark mode", &entities, true, &enc, &ded, &hasher, &embedding, 8.0, "s", "0xabc", "a",
    )
    .unwrap();
    assert!(is_sorted_unique(&p.blind_indices));
    assert_eq!(p.blind_indices.len(), 4 + 20 + 2);
    assert_eq!(p.content_fp, generate_content_fingerprint_v2("User prefers dark mode", &ded).unwrap());

    // The protobuf carries exactly those tags in field 5, the v2 fp in field 10, scheme 1 in field 14, version 4.
    let fields = walk(&p.protobuf_bytes);
    let tags5: Vec<String> =
        fields.iter().filter(|(n, _)| *n == 5).map(|(_, v)| String::from_utf8(v.clone()).unwrap()).collect();
    assert_eq!(tags5, p.blind_indices);
    let get = |n: u32| fields.iter().find(|(k, _)| *k == n).unwrap().1.clone();
    assert_eq!(String::from_utf8(get(10)).unwrap(), p.content_fp);
    assert_eq!(get(14)[0], 1);
    assert_eq!(get(8)[0], 4);

    // A query for the same words meets the stored tags, and no tag is a legacy hash.
    let q = generate_search_trapdoors_keyed("dark mode preferences", &[], &embedding, &ix, &hasher).unwrap();
    assert!(is_sorted_unique(&q));
    let stored: HashSet<&String> = p.blind_indices.iter().collect();
    // "dark", "mode", "prefer" (from "preferences") and the 20 LSH tags (same embedding).
    assert_eq!(q.iter().filter(|t| stored.contains(t)).count(), 3 + 20);
    let legacy: HashSet<String> = totalreclaw_core::blind::generate_blind_indices("User prefers dark mode")
        .into_iter()
        .chain(hasher.hash(&embedding.iter().map(|&x| f64::from(x)).collect::<Vec<_>>()).unwrap())
        .collect();
    assert!(p.blind_indices.iter().chain(q.iter()).all(|t| !legacy.contains(t)));

    // Entity and marker tags are derived inside prepare, not passed in.
    assert!(p.blind_indices.contains(&ix.entity_tag("postgresql")));
    assert!(p.blind_indices.contains(&ix.digest_marker_tag()));
    // A query naming the entity meets the stored entity tag.
    let qe = generate_search_trapdoors_keyed("x", &["POSTGRESQL".to_string()], &embedding, &ix, &hasher).unwrap();
    assert!(qe.contains(&ix.entity_tag("postgresql")) && is_sorted_unique(&qe));

    let tomb = walk(&prepare_tombstone_v1_keyed("id", "0xabc"));
    assert_eq!(tomb.iter().find(|(k, _)| *k == 14).unwrap().1[0], 1);
    assert_eq!(tomb.iter().find(|(k, _)| *k == 8).unwrap().1[0], 4);
}

#[test]
fn keyed_encoder_always_writes_version_4() {
    let f = fx();
    let mut p = payload_from(&f["wire"]["fact"]["input"]);
    for v in [0u32, 3, 4, 9] {
        p.version = v;
        let fields = walk(&encode_fact_protobuf_keyed(&p).unwrap());
        assert_eq!(fields.iter().find(|(k, _)| *k == 8).unwrap().1[0], 4, "input version {v}");
    }
    let t = walk(&totalreclaw_core::protobuf::encode_tombstone_protobuf_keyed("id", "0xabc"));
    assert_eq!(t.iter().find(|(k, _)| *k == 8).unwrap().1[0], 4);
}

#[test]
fn keyed_encoder_rejects_tags_that_are_not_64_lowercase_hex() {
    let f = fx();
    let good = "a".repeat(64);
    for bad in ["prefers", "", &"A".repeat(64), &"a".repeat(63), &"a".repeat(65), &format!("{}g", "a".repeat(63))] {
        let mut p = payload_from(&f["wire"]["fact"]["input"]);
        p.blind_indices = vec![good.clone(), bad.to_string()];
        assert!(encode_fact_protobuf_keyed(&p).is_err(), "accepted {bad:?}");
    }
    let mut p = payload_from(&f["wire"]["fact"]["input"]);
    p.blind_indices = vec![good];
    assert!(encode_fact_protobuf_keyed(&p).is_ok());
}
