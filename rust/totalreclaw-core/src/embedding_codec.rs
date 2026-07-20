//! Canonical cross-client embedding codec.
//!
//! Pure computation — no I/O, no crypto. Hoisted into core (internal#479 Part A)
//! so every client (TS plugin, MCP, Python) shares one byte-for-byte definition
//! of the *pre-encryption* embedding payload. The XChaCha20-Poly1305 wrapping
//! stays client-side; this module is only the packing/unpacking of the float
//! vector that gets encrypted.
//!
//! # Two formats, one reader
//!
//! On-chain data is immutable, so the decoder must read every format that was
//! ever written — forever:
//!
//! - **Canonical (new):** little-endian IEEE-754 binary16 (`f16`), base64-wrapped
//!   (standard alphabet, padded). Half the wire size of f32 with negligible
//!   cosine loss (~1e-7 on 640-d unit vectors). This is what
//!   [`encode_embedding_canonical`] produces for production embeddings.
//! - **Legacy TS plugin:** `JSON.stringify(number[])` — a JSON float array.
//!   `skill/plugin/index.ts:966` wrote this; a decrypted legacy payload begins
//!   with `[`.
//! - **Legacy Python (f32 binary):** little-endian f32, base64-wrapped. The old
//!   `python/.../crypto.py` `encrypt_embedding` wrote `<Nf`. The decoder infers
//!   f32 from the buffer length.
//!
//! # Length-inferred decode (must match the Python reference exactly)
//!
//! [`decode_embedding_universal`] base64-decodes then picks the element width
//! from the byte count, mirroring the Python reference's `len(buf)` dispatch:
//!
//! - `buf.len() == EMBEDDING_DIMS * 2` (1280) → `f16`, upcast each half to `f32`.
//! - otherwise, if `buf.len() % 4 == 0` → `f32`.
//! - anything else → error. **Never** a silently wrong-length vector.
//!
//! # The 640 guard on encode
//!
//! [`encode_embedding_canonical`] packs `f16` **only** when
//! `embedding.len() == EMBEDDING_DIMS`. For any other length it packs `f32`.
//! Rationale: the decoder infers width purely from buffer length, so a non-640
//! `f16` payload would be misread — e.g. a 1024-d `f16` buffer (2048 B) decodes
//! as 512 `f32`s. Packing non-640 vectors as `f32` keeps them round-trippable
//! through the universal decoder (1024-d → 4096 B → `f32` → 1024 values). The
//! Python reference packs `f16` unconditionally, which is a latent bug for
//! non-production dims; core fixes it and Python will adopt core.
//!
//! Residual ambiguity: a 320-d `f32` payload is 1280 B and is indistinguishable
//! from a 640-d `f16` payload under length inference. 320 is not a production
//! dim (production = 640; the test fixture uses 1024), so this is accepted.

use base64::Engine;
use half::f16;

use crate::{Error, Result};

/// Production embedding dimensionality. Harrier 640-d output is the canonical
/// vector width; this is the single constant every surface shares (previously
/// the literal `640` was scattered through store/search/lsh tests — grep found
/// no central constant, so it lives here now).
pub const EMBEDDING_DIMS: usize = 640;

/// Maximum finite value representable in IEEE-754 binary16 (f16 max normal).
/// Finite inputs beyond this would silently become ±inf under `f16::from_f32`;
/// the encoder refuses them instead (see [`encode_embedding_canonical`]).
pub const F16_MAX: f32 = 65504.0;

/// Pack an embedding into the canonical pre-encryption payload string.
///
/// - `embedding.len() == EMBEDDING_DIMS` (640) → little-endian `f16`.
/// - any other length → little-endian `f32` (see [the 640 guard](#the-640-guard-on-encode)).
///
/// The packed bytes are base64-encoded with the standard alphabet (padded) and
/// returned as an ASCII string — the exact pre-encryption payload every client
/// encrypts before storage. Matches the Python `struct '<Ne'` reference
/// byte-for-byte for 640-d input.
///
/// # Fail-closed input validation (#479 review)
///
/// The payload is written into an **immutable on-chain blob**: a NaN or ±inf
/// component would silently poison cosine similarity for that fact forever.
/// The encoder therefore rejects, with [`Error::InvalidInput`]:
///
/// - any non-finite component (NaN / ±inf), on BOTH the f16 and f32 paths;
/// - any finite component with `|x| > F16_MAX` (65504) on the f16 path, which
///   `f16::from_f32` would silently saturate to ±inf.
///
/// This is deliberately STRICTER than the Python `struct '<e'` reference
/// (which raises only on finite overflow and happily packs NaN/±inf):
/// fail-closed beats parity for permanent data, and Python adopts core.
///
/// # Precision precondition (byte-for-byte parity)
///
/// Inputs must be **f32-precision-exact** for the byte-for-byte Python-parity
/// claim to hold: the bindings convert incoming f64 (JS numbers / Python
/// floats) to `f32` first, so a genuinely double-precision value rounds
/// f64→f32→f16 (double rounding) where Python's `struct '<e'` rounds f64→f16
/// directly — measured divergence ~1/15k–20k random components (single-ULP,
/// invisible to cosine, but a byte difference). Real Harrier embeddings are
/// produced as f32, so the precondition holds for all production writes.
pub fn encode_embedding_canonical(embedding: &[f32]) -> Result<String> {
    let use_f16 = embedding.len() == EMBEDDING_DIMS;
    for (i, &v) in embedding.iter().enumerate() {
        if !v.is_finite() {
            return Err(Error::InvalidInput(format!(
                "embedding[{i}] is {v} — non-finite components are not encodable \
                 (they would permanently poison cosine similarity on-chain)"
            )));
        }
        if use_f16 && v.abs() > F16_MAX {
            return Err(Error::InvalidInput(format!(
                "embedding[{i}] = {v} exceeds the f16 range (|x| <= {F16_MAX}); \
                 refusing to silently saturate to ±inf"
            )));
        }
    }
    let bytes_per_elem = if use_f16 { 2 } else { 4 };
    let mut buf = Vec::with_capacity(embedding.len().saturating_mul(bytes_per_elem));
    if use_f16 {
        for &v in embedding {
            buf.extend_from_slice(&f16::from_f32(v).to_le_bytes());
        }
    } else {
        for &v in embedding {
            buf.extend_from_slice(&v.to_le_bytes());
        }
    }
    Ok(base64::engine::general_purpose::STANDARD.encode(&buf))
}

/// Decode any embedding payload — canonical or legacy — into `Vec<f32>`.
///
/// Forever-reader for immutable on-chain data. Dispatch:
///
/// 1. Payload beginning with `[` (after trimming ASCII whitespace) is parsed as
///    a JSON float array (legacy TS plugin format).
/// 2. Otherwise base64-decode, then infer width from buffer length:
///    `== EMBEDDING_DIMS*2` → `f16` upcast to `f32`; else (len divisible by 4)
///    → `f32`; else → [`Error::InvalidInput`] (never a silent wrong-dim vector).
pub fn decode_embedding_universal(payload: &str) -> Result<Vec<f32>> {
    let trimmed = payload.trim();

    // Legacy TS plugin format: a decrypted payload beginning with '[' is a
    // JSON float array (skill/plugin/index.ts:966 wrote JSON.stringify(vec)).
    if trimmed.starts_with('[') {
        let v: Vec<f32> = serde_json::from_str(trimmed)
            .map_err(|e| Error::Parse(format!("invalid JSON embedding array: {}", e)))?;
        return Ok(v);
    }

    // Otherwise: base64-decode, then infer element width from byte count.
    let buf = base64::engine::general_purpose::STANDARD
        .decode(trimmed)
        .map_err(|e| Error::Parse(format!("base64 decode failed: {}", e)))?;

    if buf.len() == EMBEDDING_DIMS * 2 {
        // Canonical f16 payload (640 * 2 bytes).
        let mut out = Vec::with_capacity(EMBEDDING_DIMS);
        for chunk in buf.chunks_exact(2) {
            out.push(f16::from_le_bytes([chunk[0], chunk[1]]).to_f32());
        }
        Ok(out)
    } else if buf.len() % 4 == 0 {
        // Legacy f32 binary payload (old Python encrypt_embedding, '<Nf').
        let mut out = Vec::with_capacity(buf.len() / 4);
        for chunk in buf.chunks_exact(4) {
            let mut arr = [0u8; 4];
            arr.copy_from_slice(chunk);
            out.push(f32::from_le_bytes(arr));
        }
        Ok(out)
    } else {
        // Buffer length is neither 640*2 nor a multiple of 4 — there is no
        // valid interpretation, so refuse rather than return a wrong-dim vector.
        Err(Error::InvalidInput(format!(
            "decoded embedding buffer has length {} (expected {} for f16, or a multiple of 4 for f32)",
            buf.len(),
            EMBEDDING_DIMS * 2
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Golden cross-format vectors committed to disk. Regenerated by
    /// `/tmp/gen_emb_fixtures.py` (a 15-line struct/base64 script); the
    /// canonical base64 is independently reproducible by the one-liner below.
    fn fixture() -> serde_json::Value {
        serde_json::from_str(include_str!("../tests/fixtures/embedding_codec_vectors.json"))
            .expect("embedding_codec_vectors.json fixture must parse")
    }

    fn unit_vector() -> Vec<f32> {
        serde_json::from_value(fixture()["unit_vector"].clone()).unwrap()
    }

    /// Cosine similarity between two equal-length slices (f64 accumulator).
    fn cosine(a: &[f32], b: &[f32]) -> f64 {
        assert_eq!(a.len(), b.len());
        let mut dot = 0.0f64;
        let mut na = 0.0f64;
        let mut nb = 0.0f64;
        for (x, y) in a.iter().zip(b.iter()) {
            dot += (*x as f64) * (*y as f64);
            na += (*x as f64) * (*x as f64);
            nb += (*y as f64) * (*y as f64);
        }
        dot / (na.sqrt() * nb.sqrt())
    }

    // -------------------------------------------------------------------------
    // PYTHON PARITY — the canonical base64 must match struct 'e' output.
    // -------------------------------------------------------------------------

    /// `encode_embedding_canonical` of the 640-d unit vector MUST equal the
    /// base64 produced by the Python `struct '<e'` reference. Regenerate the
    /// expected string with this one-liner (output committed as
    /// `canonical_f16_base64` in the fixture):
    ///
    ///   python3 -c "import struct,base64,math; raw=[math.sin(i*0.1)*0.5 for i in range(640)]; n=math.sqrt(sum(x*x for x in raw)); v=[struct.unpack('<f',struct.pack('<f',x/n))[0] for x in raw]; print(base64.b64encode(struct.pack('<640e',*v)).decode())"
    ///
    /// Every committed float is snapped to exact f32 first (struct.pack '<f'),
    /// so Rust f16::from_f32 and Python struct '<e' agree byte-for-byte
    /// (no f64->f32->f16 double-rounding: the f64 IS the f32).
    #[test]
    fn encode_canonical_640_matches_python_f16_base64() {
        let expected = fixture()["canonical_f16_base64"]
            .as_str()
            .unwrap()
            .to_string();
        let got = encode_embedding_canonical(&unit_vector()).unwrap();
        assert_eq!(got, expected, "core f16 base64 must match Python struct '<e'");
    }

    // -------------------------------------------------------------------------
    // Decode: canonical f16 round-trip (high cosine; f16 is lossy by ~1e-7).
    // -------------------------------------------------------------------------

    #[test]
    fn decode_canonical_f16_roundtrips_at_high_cosine() {
        let f = fixture();
        let b64 = f["canonical_f16_base64"].as_str().unwrap();
        let decoded = decode_embedding_universal(b64).unwrap();
        let input = unit_vector();
        assert_eq!(decoded.len(), EMBEDDING_DIMS);
        let cos = cosine(&decoded, &input);
        assert!(
            cos >= 0.9999,
            "canonical f16 round-trip cosine {cos} below 0.9999"
        );
    }

    // -------------------------------------------------------------------------
    // Decode: legacy JSON array path (TS plugin) — exact recovery.
    // -------------------------------------------------------------------------

    #[test]
    fn decode_legacy_json_array_is_exact() {
        let f = fixture();
        let json = f["legacy_json_640"].as_str().unwrap();
        assert!(json.trim_start().starts_with('['));
        let decoded = decode_embedding_universal(json).unwrap();
        assert_eq!(decoded, unit_vector(), "JSON legacy path must recover input exactly");
    }

    // -------------------------------------------------------------------------
    // Decode: legacy f32 binary path (old Python) — exact recovery.
    // -------------------------------------------------------------------------

    #[test]
    fn decode_legacy_f32_640_is_exact() {
        let f = fixture();
        let b64 = f["legacy_f32_base64_640"].as_str().unwrap();
        let decoded = decode_embedding_universal(b64).unwrap();
        // 640*4 = 2560 B != 1280, so the length-inference picks f32.
        assert_eq!(decoded.len(), EMBEDDING_DIMS);
        assert_eq!(decoded, unit_vector(), "f32 legacy path must recover input exactly");
    }

    // -------------------------------------------------------------------------
    // Encode -> decode round trip (640-d canonical).
    // -------------------------------------------------------------------------

    #[test]
    fn encode_then_decode_640_round_trips_at_high_cosine() {
        let input = unit_vector();
        let payload = encode_embedding_canonical(&input).unwrap();
        assert!(payload.starts_with('[') == false, "canonical payload is base64, not JSON");
        let back = decode_embedding_universal(&payload).unwrap();
        let cos = cosine(&back, &input);
        assert!(cos >= 0.9999, "640-d round-trip cosine {cos} below 0.9999");
    }

    // -------------------------------------------------------------------------
    // Non-canonical dim: encode must use f32 (the 640 guard), round-trip exact.
    // -------------------------------------------------------------------------

    #[test]
    fn encode_non_canonical_dim_uses_f32_and_round_trips_exact() {
        // 1024 != 640, so encode packs f32 (4096 B). 4096 != 1280 and %4 == 0,
        // so decode reads f32 — a lossless round trip.
        let f = fixture();
        let nc = &f["non_canonical"];
        let input: Vec<f32> = serde_json::from_value(nc["vector"].clone()).unwrap();
        assert_eq!(input.len(), 1024);
        let payload = encode_embedding_canonical(&input).unwrap();
        let back = decode_embedding_universal(&payload).unwrap();
        assert_eq!(back, input, "1024-d f32 round trip must be lossless");

        // Small non-640 vector also takes the f32 path and round-trips exactly.
        let small = vec![0.125, -0.75, 3.5, 0.0, 1.0];
        let s_payload = encode_embedding_canonical(&small).unwrap();
        assert_ne!(s_payload, "");
        assert_eq!(decode_embedding_universal(&s_payload).unwrap(), small);
    }

    #[test]
    fn decode_1024_f32_payload_matches_fixture() {
        let f = fixture();
        let nc = &f["non_canonical"];
        let b64 = nc["f32_base64"].as_str().unwrap();
        let expected: Vec<f32> = serde_json::from_value(nc["vector"].clone()).unwrap();
        assert_eq!(decode_embedding_universal(b64).unwrap(), expected);
    }

    // -------------------------------------------------------------------------
    // Error paths — never a silent wrong-dim vector.
    // -------------------------------------------------------------------------

    #[test]
    fn decode_rejects_buffer_of_bad_length() {
        // 1 byte: not 1280 (f16) and not divisible by 4 (f32) -> error.
        let one_byte_b64 = base64::engine::general_purpose::STANDARD.encode([0xAAu8]);
        let err = decode_embedding_universal(&one_byte_b64).unwrap_err();
        assert!(
            matches!(err, Error::InvalidInput(_)),
            "bad-length buffer must be InvalidInput, got {err:?}"
        );
    }

    #[test]
    fn decode_rejects_malformed_json_array() {
        let err = decode_embedding_universal("[1.0, not-json").unwrap_err();
        assert!(matches!(err, Error::Parse(_)), "malformed JSON must be Parse, got {err:?}");
    }

    #[test]
    fn decode_rejects_invalid_base64() {
        let err = decode_embedding_universal("!!!! not base64 &&&").unwrap_err();
        assert!(matches!(err, Error::Parse(_)), "bad base64 must be Parse, got {err:?}");
    }

    // -------------------------------------------------------------------------
    // Whitespace / empty edge cases.
    // -------------------------------------------------------------------------

    #[test]
    fn decode_trims_whitespace_then_parses_json() {
        let decoded = decode_embedding_universal("   [1.5, 2.5, -3.25]\n").unwrap();
        assert_eq!(decoded, vec![1.5, 2.5, -3.25]);
    }

    #[test]
    fn decode_empty_json_array_returns_empty_vec() {
        assert_eq!(decode_embedding_universal("[]").unwrap(), Vec::<f32>::new());
    }

    // -------------------------------------------------------------------------
    // Fail-closed encode validation (#479 review finding 1) — permanent data
    // must never carry NaN/inf, and finite f16 overflow must not saturate.
    // -------------------------------------------------------------------------

    #[test]
    fn encode_rejects_non_finite_on_f16_path() {
        let mut v = unit_vector();
        v[7] = f32::NAN;
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
        v[7] = f32::INFINITY;
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
        v[7] = f32::NEG_INFINITY;
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
    }

    #[test]
    fn encode_rejects_non_finite_on_f32_path() {
        // Non-640 dim takes the f32 path; NaN is representable there but still
        // rejected — a NaN component is an invalid embedding, period.
        let v = vec![0.5, f32::NAN, 0.25];
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
    }

    #[test]
    fn encode_rejects_finite_f16_overflow_and_accepts_f16_max() {
        // |x| > 65504 would silently saturate to ±inf under f16::from_f32 —
        // the exact case Python's struct '<e' raises OverflowError on.
        let mut v = unit_vector();
        v[0] = 65505.0;
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
        v[0] = -1.0e10;
        assert!(matches!(encode_embedding_canonical(&v).unwrap_err(), Error::InvalidInput(_)));
        // Boundary: exactly F16_MAX is representable and must encode.
        v[0] = F16_MAX;
        let payload = encode_embedding_canonical(&v).unwrap();
        let back = decode_embedding_universal(&payload).unwrap();
        assert_eq!(back[0], F16_MAX);
    }

    #[test]
    fn encode_allows_large_finite_on_f32_path() {
        // The f32 path has no f16 range limit — 1e10 is finite and packs
        // losslessly as f32 for non-canonical dims.
        let v = vec![1.0e10, -2.5];
        let payload = encode_embedding_canonical(&v).unwrap();
        assert_eq!(decode_embedding_universal(&payload).unwrap(), v);
    }

    #[test]
    fn encode_empty_returns_empty_string() {
        // 0 bytes -> empty base64. 0 != 640 so the f32 path is taken, packing
        // nothing. The decoder reads the empty payload back as an empty vector.
        assert_eq!(encode_embedding_canonical(&[]).unwrap(), "");
        assert_eq!(decode_embedding_universal("").unwrap(), Vec::<f32>::new());
    }
}
