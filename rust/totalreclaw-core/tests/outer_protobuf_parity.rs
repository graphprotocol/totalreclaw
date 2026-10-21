//! PRD-04 F8 / DEP-6 — outer-protobuf calldata parity (Rust leg).
//!
//! Loads the shared fixture `tests/parity/fixtures/outer-protobuf-v1.json`
//! (repo root) and asserts that the Rust core — the source of truth the
//! Python (PyO3) and TS (WASM) bindings delegate to — produces byte-identical
//! protobuf and SimpleAccount calldata, and that ABI-decoding that calldata
//! yields outer protobufs WITHOUT field 9 (`source`) or field 11 (`agent_id`).
//! The legacy vectors (pre-DEP-6 MCP output) are a negative control.
//!
//! Siblings: tests/parity/outer-protobuf-parity.test.ts (TS: MCP + WASM),
//! tests/parity/test_outer_protobuf_parity.py (Python),
//! mcp/tests/protobuf-outer-fields.test.ts (MCP unit guard).
#![cfg(feature = "managed")]

use serde_json::Value;
use totalreclaw_core::protobuf::{encode_fact_protobuf, FactPayload};
use totalreclaw_core::userop::{encode_batch_call_to, encode_single_call_to};

const FIXTURE: &str = include_str!("../../../tests/parity/fixtures/outer-protobuf-v1.json");
const LEGACY: &str =
    include_str!("../../../tests/parity/fixtures/legacy/outer-protobuf-mcp-pre-dep6.json");

const SELECTOR_EXECUTE: [u8; 4] = [0xb6, 0x1d, 0x27, 0xf6];
const SELECTOR_EXECUTE_BATCH: [u8; 4] = [0x47, 0xe1, 0xda, 0x2a];

fn fixture() -> Value {
    serde_json::from_str(FIXTURE).expect("outer-protobuf-v1.json parses")
}

fn forbidden(fx: &Value) -> Vec<u64> {
    fx["meta"]["forbidden_fields"]
        .as_array()
        .expect("meta.forbidden_fields")
        .iter()
        .map(|v| v.as_u64().expect("field number"))
        .collect()
}

fn data_edge(fx: &Value) -> String {
    fx["meta"]["data_edge_address"]
        .as_str()
        .expect("meta.data_edge_address")
        .to_string()
}

fn read_varint(buf: &[u8], mut pos: usize) -> (u64, usize) {
    let mut value: u64 = 0;
    let mut shift = 0u32;
    loop {
        let byte = buf[pos];
        pos += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return (value, pos);
        }
        shift += 7;
    }
}

/// Outer protobuf field numbers of `buf`, in wire order.
fn field_numbers(buf: &[u8]) -> Vec<u64> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < buf.len() {
        let (key, next) = read_varint(buf, pos);
        pos = next;
        out.push(key >> 3);
        match key & 0x07 {
            0 => {
                let (_, next) = read_varint(buf, pos);
                pos = next;
            }
            1 => pos += 8,
            2 => {
                let (len, next) = read_varint(buf, pos);
                pos = next + len as usize;
            }
            5 => pos += 4,
            wire_type => panic!("unsupported wire type {wire_type}"),
        }
    }
    assert_eq!(pos, buf.len(), "protobuf walk overran the buffer");
    out
}

fn word(calldata: &[u8], offset: usize) -> &[u8] {
    &calldata[4 + offset..4 + offset + 32]
}

fn word_usize(calldata: &[u8], offset: usize) -> usize {
    let w = word(calldata, offset);
    assert!(w[..24].iter().all(|b| *b == 0), "ABI word does not fit in u64");
    let mut low = [0u8; 8];
    low.copy_from_slice(&w[24..32]);
    u64::from_be_bytes(low) as usize
}

fn word_address(calldata: &[u8], offset: usize) -> String {
    let w = word(calldata, offset);
    assert!(w[..12].iter().all(|b| *b == 0), "address word must be left-padded");
    format!("0x{}", hex::encode(&w[12..32]))
}

fn bytes_at(calldata: &[u8], offset: usize) -> Vec<u8> {
    let len = word_usize(calldata, offset);
    calldata[4 + offset + 32..4 + offset + 32 + len].to_vec()
}

/// Decode `execute(address dest, uint256 value, bytes func)` → (dest, func).
fn decode_execute(calldata: &[u8]) -> (String, Vec<u8>) {
    assert_eq!(&calldata[..4], &SELECTOR_EXECUTE[..], "execute() selector");
    assert_eq!(word_usize(calldata, 32), 0, "value must be 0");
    let func_offset = word_usize(calldata, 64);
    (word_address(calldata, 0), bytes_at(calldata, func_offset))
}

/// Decode `executeBatch(address[] dest, uint256[] value, bytes[] func)`.
fn decode_execute_batch(calldata: &[u8]) -> Vec<(String, Vec<u8>)> {
    assert_eq!(&calldata[..4], &SELECTOR_EXECUTE_BATCH[..], "executeBatch() selector");
    let dest_offset = word_usize(calldata, 0);
    let value_offset = word_usize(calldata, 32);
    let func_offset = word_usize(calldata, 64);
    let count = word_usize(calldata, dest_offset);
    assert_eq!(word_usize(calldata, value_offset), count);
    assert_eq!(word_usize(calldata, func_offset), count);
    (0..count)
        .map(|k| {
            assert_eq!(word_usize(calldata, value_offset + 32 + 32 * k), 0, "value must be 0");
            let element = word_usize(calldata, func_offset + 32 + 32 * k);
            (
                word_address(calldata, dest_offset + 32 + 32 * k),
                bytes_at(calldata, func_offset + 32 + element),
            )
        })
        .collect()
}

fn payload_from(input: &Value) -> FactPayload {
    let s = |key: &str| input[key].as_str().expect(key).to_string();
    FactPayload {
        id: s("id"),
        timestamp: s("timestamp"),
        owner: s("owner"),
        encrypted_blob_hex: s("encrypted_blob_hex"),
        blind_indices: input["blind_indices"]
            .as_array()
            .expect("blind_indices")
            .iter()
            .map(|v| v.as_str().expect("blind index").to_string())
            .collect(),
        decay_score: input["decay_score"].as_f64().expect("decay_score"),
        source: s("source"),
        content_fp: s("content_fp"),
        agent_id: s("agent_id"),
        encrypted_embedding: input["encrypted_embedding"].as_str().map(str::to_string),
        version: input["version"].as_u64().expect("version") as u32,
    }
}

fn expected_fields(vector: &Value) -> Vec<u64> {
    vector["expected_field_numbers"]
        .as_array()
        .expect("expected_field_numbers")
        .iter()
        .map(|v| v.as_u64().expect("field number"))
        .collect()
}

fn assert_no_forbidden(fields: &[u64], forbidden: &[u64], context: &str) {
    for f in forbidden {
        assert!(!fields.contains(f), "{context}: outer field {f} present in {fields:?}");
    }
}

#[test]
fn rust_encoder_matches_fixture_and_omits_fields_9_and_11() {
    let fx = fixture();
    let forbidden = forbidden(&fx);
    for vector in fx["vectors"].as_array().expect("vectors") {
        let name = vector["name"].as_str().expect("name");
        let encoded = encode_fact_protobuf(&payload_from(&vector["input"]));
        assert_eq!(
            hex::encode(&encoded),
            vector["expected_protobuf_hex"].as_str().expect("expected_protobuf_hex"),
            "{name}: Rust protobuf diverges from the shared fixture"
        );
        let fields = field_numbers(&encoded);
        assert_no_forbidden(&fields, &forbidden, name);
        assert_eq!(fields, expected_fields(vector), "{name}: field layout");
    }
}

#[test]
fn rust_execute_calldata_matches_fixture_and_decodes_clean() {
    let fx = fixture();
    let forbidden = forbidden(&fx);
    let edge = data_edge(&fx);
    for vector in fx["vectors"].as_array().expect("vectors") {
        let name = vector["name"].as_str().expect("name");
        let encoded = encode_fact_protobuf(&payload_from(&vector["input"]));
        let calldata = encode_single_call_to(&encoded, &edge).expect("valid DataEdge address");
        assert_eq!(
            hex::encode(&calldata),
            vector["expected_execute_calldata_hex"]
                .as_str()
                .expect("expected_execute_calldata_hex"),
            "{name}: Rust execute() calldata diverges from the shared fixture"
        );
        let (dest, inner) = decode_execute(&calldata);
        assert_eq!(dest, edge.to_lowercase(), "{name}: execute() destination");
        assert_eq!(inner, encoded, "{name}: decoded payload");
        assert_no_forbidden(&field_numbers(&inner), &forbidden, name);
    }
}

#[test]
fn rust_batch_calldata_matches_fixture_and_decodes_clean() {
    let fx = fixture();
    let forbidden = forbidden(&fx);
    let edge = data_edge(&fx);
    let vectors = fx["vectors"].as_array().expect("vectors");
    let payloads: Vec<Vec<u8>> = fx["batch"]["vector_names"]
        .as_array()
        .expect("batch.vector_names")
        .iter()
        .map(|n| {
            let name = n.as_str().expect("vector name");
            let vector = vectors
                .iter()
                .find(|v| v["name"].as_str() == Some(name))
                .expect("batch vector exists");
            encode_fact_protobuf(&payload_from(&vector["input"]))
        })
        .collect();
    let calldata = encode_batch_call_to(&payloads, &edge).expect("valid batch");
    assert_eq!(
        hex::encode(&calldata),
        fx["batch"]["expected_calldata_hex"]
            .as_str()
            .expect("batch.expected_calldata_hex"),
        "Rust executeBatch() calldata diverges from the shared fixture"
    );
    let calls = decode_execute_batch(&calldata);
    assert_eq!(calls.len(), payloads.len());
    for (k, (dest, inner)) in calls.iter().enumerate() {
        assert_eq!(dest, &edge.to_lowercase(), "batch call {k}: destination");
        assert_eq!(inner, &payloads[k], "batch call {k}: payload");
        assert_no_forbidden(&field_numbers(inner), &forbidden, "batch call");
    }
}

#[test]
fn legacy_pre_dep6_vectors_do_contain_fields_9_and_11() {
    let legacy: Value = serde_json::from_str(LEGACY).expect("legacy fixture parses");
    for vector in legacy["vectors"].as_array().expect("vectors") {
        let name = vector["name"].as_str().expect("name");
        let bytes = hex::decode(vector["protobuf_hex"].as_str().expect("protobuf_hex"))
            .expect("legacy hex decodes");
        let fields = field_numbers(&bytes);
        assert!(
            fields.contains(&9) && fields.contains(&11),
            "{name}: negative control must contain fields 9 and 11, got {fields:?}"
        );
        assert_eq!(fields, expected_fields(vector), "{name}: legacy field layout");
    }
}
