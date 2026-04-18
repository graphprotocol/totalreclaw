//! ZeroClaw Memory Taxonomy v1 tests.
//!
//! Covers the v1 write path (`store_fact_v1` / `V1StoreInput` /
//! `build_memory_claim_v1`), v4 protobuf encoding, and v1 blob envelope
//! parsing on the recall side. Crypto-heavy end-to-end flows (real
//! relay + subgraph) are exercised by the separate
//! `tests/e2e_spec_validation.rs` suite.

use totalreclaw_core::claims::{
    MemoryClaimV1, MemoryScope, MemorySource, MemoryTypeV1, MemoryVolatility,
    MEMORY_CLAIM_V1_SCHEMA_VERSION,
};
use totalreclaw_core::protobuf::{
    encode_fact_protobuf, encode_tombstone_protobuf, FactPayload, DEFAULT_PROTOBUF_VERSION,
    PROTOBUF_VERSION_V4,
};
use totalreclaw_memory::store::{build_memory_claim_v1, V1StoreInput};

// ---------------------------------------------------------------------------
// V1StoreInput + build_memory_claim_v1
// ---------------------------------------------------------------------------

#[test]
fn v1_store_input_new_claim_populates_sensible_defaults() {
    let input = V1StoreInput::new_claim("User prefers PostgreSQL", 8);
    assert_eq!(input.text, "User prefers PostgreSQL");
    assert_eq!(input.memory_type, MemoryTypeV1::Claim);
    assert_eq!(input.source, MemorySource::UserInferred);
    assert_eq!(input.importance, 8);
    assert_eq!(input.scope, MemoryScope::Unspecified);
    assert_eq!(input.volatility, MemoryVolatility::Updatable);
    assert!(input.reasoning.is_none());
}

#[test]
fn build_memory_claim_v1_uuid_v7_and_iso8601() {
    let input = V1StoreInput {
        text: "x".into(),
        memory_type: MemoryTypeV1::Directive,
        source: MemorySource::User,
        importance: 9,
        scope: MemoryScope::Work,
        volatility: MemoryVolatility::Stable,
        reasoning: None,
    };
    let claim = build_memory_claim_v1(&input);

    // UUID v7 is 36 chars long with the standard dash layout
    assert_eq!(claim.id.len(), 36);
    assert_eq!(claim.id.chars().filter(|c| *c == '-').count(), 4);
    // version nibble is the 15th character (index 14) — should be '7' for v7
    assert_eq!(claim.id.chars().nth(14), Some('7'));

    // ISO8601 UTC with millisecond precision (ends in `Z` or has a `+` offset)
    assert!(claim.created_at.contains('T'));
    assert!(
        claim.created_at.ends_with('Z') || claim.created_at.contains('+'),
        "expected RFC 3339 UTC: {}",
        claim.created_at
    );

    // Provenance + schema wiring
    assert_eq!(claim.memory_type, MemoryTypeV1::Directive);
    assert_eq!(claim.source, MemorySource::User);
    assert_eq!(claim.scope, MemoryScope::Work);
    assert_eq!(claim.volatility, MemoryVolatility::Stable);
    assert_eq!(claim.schema_version, MEMORY_CLAIM_V1_SCHEMA_VERSION);
    assert_eq!(claim.importance, Some(9));
}

#[test]
fn build_memory_claim_v1_preserves_reasoning_for_decisions() {
    let input = V1StoreInput {
        text: "Chose PostgreSQL over MySQL".into(),
        memory_type: MemoryTypeV1::Claim,
        source: MemorySource::User,
        importance: 9,
        scope: MemoryScope::Work,
        volatility: MemoryVolatility::Stable,
        reasoning: Some("because data is relational and needs ACID".into()),
    };
    let claim = build_memory_claim_v1(&input);
    assert_eq!(
        claim.reasoning.as_deref(),
        Some("because data is relational and needs ACID")
    );
}

// ---------------------------------------------------------------------------
// v1 envelope round-trip (JSON canonical form)
// ---------------------------------------------------------------------------

#[test]
fn v1_claim_serializes_with_spec_field_names() {
    let input = V1StoreInput {
        text: "test".into(),
        memory_type: MemoryTypeV1::Preference,
        source: MemorySource::UserInferred,
        importance: 7,
        scope: MemoryScope::Personal,
        volatility: MemoryVolatility::Updatable,
        reasoning: None,
    };
    let claim = build_memory_claim_v1(&input);
    let json = serde_json::to_string(&claim).unwrap();

    // Required fields use the v1 wire names (`text`, `type`, `source`,
    // `created_at`, id). Avoid matching on short v0 keys (`t`, `s`, `a`).
    assert!(json.contains(r#""text":"test""#));
    assert!(json.contains(r#""type":"preference""#));
    assert!(json.contains(r#""source":"user-inferred""#));
    assert!(json.contains(r#""scope":"personal""#));
    // `scope:unspecified` + `volatility:updatable` + schema_version are default-skipped
    // but `personal` is non-default so it IS emitted.
    assert!(!json.contains(r#""a":"zeroclaw""#)); // v0 short key must not appear
}

#[test]
fn v1_claim_round_trips_through_json() {
    let input = V1StoreInput {
        text: "User prefers PostgreSQL over MySQL for OLTP".into(),
        memory_type: MemoryTypeV1::Preference,
        source: MemorySource::User,
        importance: 8,
        scope: MemoryScope::Work,
        volatility: MemoryVolatility::Stable,
        reasoning: None,
    };
    let claim = build_memory_claim_v1(&input);
    let json = serde_json::to_string(&claim).unwrap();
    let back: MemoryClaimV1 = serde_json::from_str(&json).unwrap();

    assert_eq!(claim, back);
}

// ---------------------------------------------------------------------------
// v4 protobuf — outer schema version tag
// ---------------------------------------------------------------------------

#[test]
fn v4_protobuf_carries_version_4_on_field_8() {
    let payload = FactPayload {
        id: "v1-test".into(),
        timestamp: "2026-04-18T12:00:00Z".into(),
        owner: "0xabcd".into(),
        encrypted_blob_hex: "deadbeef".into(),
        blind_indices: vec![],
        decay_score: 0.8,
        source: "zeroclaw_v1_user".into(),
        content_fp: "fp".into(),
        agent_id: "zeroclaw".into(),
        encrypted_embedding: None,
        version: PROTOBUF_VERSION_V4,
    };
    let bytes = encode_fact_protobuf(&payload);
    // Tag byte for field 8 (varint): (8<<3)|0 = 0x40.
    assert!(bytes.windows(2).any(|w| w == [0x40, 4]));
    assert!(!bytes.windows(2).any(|w| w == [0x40, 3]));
}

#[test]
fn v3_protobuf_still_valid_for_legacy_writes() {
    // ZeroClaw 2.0 keeps the v3 path for the legacy `Memory` trait writes
    // during the v0→v1 migration window. Confirm version tag is 3.
    let payload = FactPayload {
        id: "v3-test".into(),
        timestamp: "2026-04-18T12:00:00Z".into(),
        owner: "0xabcd".into(),
        encrypted_blob_hex: "deadbeef".into(),
        blind_indices: vec![],
        decay_score: 0.8,
        source: "zeroclaw_core".into(),
        content_fp: "fp".into(),
        agent_id: "zeroclaw".into(),
        encrypted_embedding: None,
        version: DEFAULT_PROTOBUF_VERSION,
    };
    let bytes = encode_fact_protobuf(&payload);
    assert!(bytes.windows(2).any(|w| w == [0x40, 3]));
    assert!(!bytes.windows(2).any(|w| w == [0x40, 4]));
}

#[test]
fn tombstone_v1_uses_version_4() {
    let v1_bytes = encode_tombstone_protobuf("fact-id-1", "0xabcd", PROTOBUF_VERSION_V4);
    let v3_bytes = encode_tombstone_protobuf("fact-id-1", "0xabcd", DEFAULT_PROTOBUF_VERSION);
    assert!(v1_bytes.windows(2).any(|w| w == [0x40, 4]));
    assert!(v3_bytes.windows(2).any(|w| w == [0x40, 3]));
}

#[test]
fn zero_version_defaults_to_v3() {
    // back-compat safety net — a caller that forgets to set `version` still
    // emits a valid v3 blob.
    let payload = FactPayload {
        id: "back-compat".into(),
        timestamp: "2026-04-18T12:00:00Z".into(),
        owner: "0xabcd".into(),
        encrypted_blob_hex: "de".into(),
        blind_indices: vec![],
        decay_score: 0.5,
        source: "zeroclaw_core".into(),
        content_fp: "fp".into(),
        agent_id: "zeroclaw".into(),
        encrypted_embedding: None,
        version: 0,
    };
    let bytes = encode_fact_protobuf(&payload);
    assert!(bytes.windows(2).any(|w| w == [0x40, 3]));
}

// ---------------------------------------------------------------------------
// v1 MemorySource enum — Retrieval v2 Tier 1 plumbing
// ---------------------------------------------------------------------------

#[test]
fn memory_source_serializes_as_kebab_case() {
    // ZeroClaw 2.0 parses the v1 blob's `source` field and feeds the value
    // into the reranker's Candidate struct so Retrieval v2 Tier 1 source
    // weights apply. Confirm the wire format is kebab-case per spec.
    let mk = |s: MemorySource| serde_json::to_string(&s).unwrap();
    assert_eq!(mk(MemorySource::User), r#""user""#);
    assert_eq!(mk(MemorySource::UserInferred), r#""user-inferred""#);
    assert_eq!(mk(MemorySource::Assistant), r#""assistant""#);
    assert_eq!(mk(MemorySource::External), r#""external""#);
    assert_eq!(mk(MemorySource::Derived), r#""derived""#);
}

#[test]
fn memory_source_deserializes_from_kebab_case() {
    let from = |s: &str| -> MemorySource { serde_json::from_str(s).unwrap() };
    assert_eq!(from(r#""user""#), MemorySource::User);
    assert_eq!(from(r#""user-inferred""#), MemorySource::UserInferred);
    assert_eq!(from(r#""assistant""#), MemorySource::Assistant);
    assert_eq!(from(r#""external""#), MemorySource::External);
    assert_eq!(from(r#""derived""#), MemorySource::Derived);
}

// ---------------------------------------------------------------------------
// v1 memory type semantics
// ---------------------------------------------------------------------------

#[test]
fn memory_type_v1_covers_6_canonical_values() {
    use std::collections::HashSet;
    let all: HashSet<MemoryTypeV1> = [
        MemoryTypeV1::Claim,
        MemoryTypeV1::Preference,
        MemoryTypeV1::Directive,
        MemoryTypeV1::Commitment,
        MemoryTypeV1::Episode,
        MemoryTypeV1::Summary,
    ]
    .into_iter()
    .collect();
    assert_eq!(all.len(), 6);
}

#[test]
fn memory_type_v1_from_str_lossy_is_case_insensitive() {
    assert_eq!(MemoryTypeV1::from_str_lossy("claim"), MemoryTypeV1::Claim);
    assert_eq!(MemoryTypeV1::from_str_lossy("CLAIM"), MemoryTypeV1::Claim);
    assert_eq!(MemoryTypeV1::from_str_lossy(" Directive "), MemoryTypeV1::Directive);
    assert_eq!(MemoryTypeV1::from_str_lossy("commitment"), MemoryTypeV1::Commitment);
    assert_eq!(MemoryTypeV1::from_str_lossy("episode"), MemoryTypeV1::Episode);
    assert_eq!(MemoryTypeV1::from_str_lossy("summary"), MemoryTypeV1::Summary);
    assert_eq!(MemoryTypeV1::from_str_lossy("preference"), MemoryTypeV1::Preference);
    // Unknown → Claim (lossy parse per v1 spec)
    assert_eq!(MemoryTypeV1::from_str_lossy("fact"), MemoryTypeV1::Claim);
    assert_eq!(MemoryTypeV1::from_str_lossy("rule"), MemoryTypeV1::Claim);
    assert_eq!(MemoryTypeV1::from_str_lossy(""), MemoryTypeV1::Claim);
}

// ---------------------------------------------------------------------------
// v1 memory scope enum
// ---------------------------------------------------------------------------

#[test]
fn memory_scope_covers_8_canonical_values() {
    use std::collections::HashSet;
    let all: HashSet<MemoryScope> = [
        MemoryScope::Work,
        MemoryScope::Personal,
        MemoryScope::Health,
        MemoryScope::Family,
        MemoryScope::Creative,
        MemoryScope::Finance,
        MemoryScope::Misc,
        MemoryScope::Unspecified,
    ]
    .into_iter()
    .collect();
    assert_eq!(all.len(), 8);
}

#[test]
fn memory_volatility_covers_3_canonical_values() {
    use std::collections::HashSet;
    let all: HashSet<MemoryVolatility> = [
        MemoryVolatility::Stable,
        MemoryVolatility::Updatable,
        MemoryVolatility::Ephemeral,
    ]
    .into_iter()
    .collect();
    assert_eq!(all.len(), 3);
}
