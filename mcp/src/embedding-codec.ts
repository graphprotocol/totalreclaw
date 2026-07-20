/**
 * Universal embedding codec — cross-client format parity (internal#479 Part B).
 *
 * MCP-side mirror of `skill/plugin/embedding/embedding-codec.ts`. The pure
 * packing/unpacking of the *pre-encryption* embedding payload; the
 * XChaCha20-Poly1305 wrapping stays in `subgraph/crypto.ts`
 * (`encrypt` / `decrypt`).
 *
 * # The parity bug (#479)
 *
 * Three clients wrote three different pre-encryption payloads:
 *   - Hermes / canonical core: base64(little-endian f16) — 1280 B for 640-d.
 *   - TS plugin (legacy):      `JSON.stringify(number[])` — a JSON array.
 *   - Old Python / MCP:         base64(little-endian f32) — 2560 B for 640-d.
 *
 * Each client only read its OWN format, so in a mixed-client vault a foreign
 * fact's embedding decrypted to garbage and that fact silently degraded to
 * word-index-only matching. The READ side below fixes that: one UNIVERSAL
 * decoder, preferring `@totalreclaw/core.decodeEmbeddingUniversal` when the
 * installed core exposes it, else a local fallback implementing the exact
 * dispatch of core's `decode_embedding_universal`
 * (rust/totalreclaw-core/src/embedding_codec.rs):
 *   - payload beginning with `[` → JSON float array;
 *   - else base64-decode → `len == EMBEDDING_DIMS*2` → f16 upcast to f32;
 *   - else `len % 4 == 0` → LE f32;
 *   - else → throw. Never a silently wrong-length vector.
 *
 * WRITE: {@link encodeEmbeddingPayload} delegates to
 * `@totalreclaw/core.encodeEmbeddingCanonical` when present (canonical f16,
 * fail-closed on NaN/±inf/overflow — the throw aborts the store). When the
 * installed core predates the codec it keeps the legacy f32-binary packing the
 * MCP server has always used (see `index.ts` `encryptEmbedding`). f32→f16
 * rounding is NOT reimplemented here — it stays in core; writes flip to
 * canonical automatically when the core dep bumps.
 */

/** Production embedding dimensionality. Mirrors core's `EMBEDDING_DIMS`. */
export const EMBEDDING_DIMS = 640;

/**
 * The core binding shape this module prefers. Both ship in `@totalreclaw/core`
 * once the embedding codec lands; older cores omit them and we fall back to the
 * local implementation. Structural (no `instanceof`) so a stale type
 * declaration never blocks the build — same style as `consolidation.ts`.
 */
export interface CoreEmbeddingCodec {
  /** Pack a vector into the canonical pre-encryption payload. Throws on
   * non-finite / f16-overflow inputs (fail-closed). */
  encodeEmbeddingCanonical(embedding: number[]): string;
  /** Decode any payload (canonical or legacy) into a vector. */
  decodeEmbeddingUniversal(payload: string): number[] | Float32Array;
}

// MCP uses bare `require('@totalreclaw/core')` (see consolidation.ts,
// contradiction-sync.ts) — match that pattern rather than `createRequire`.
let _coreCodec: CoreEmbeddingCodec | null | undefined;

/**
 * Probe the installed `@totalreclaw/core` for the embedding codec. Returns the
 * binding when BOTH functions are present, `null` when either is missing (or
 * the module cannot be loaded), and caches the result. The require is wrapped
 * so a partial install of the dependency tree does not crash module init.
 */
function resolveCoreCodec(): CoreEmbeddingCodec | null {
  if (_coreCodec !== undefined) return _coreCodec;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const core = require('@totalreclaw/core') as Record<string, unknown>;
    if (
      typeof core.encodeEmbeddingCanonical === 'function' &&
      typeof core.decodeEmbeddingUniversal === 'function'
    ) {
      _coreCodec = core as unknown as CoreEmbeddingCodec;
      return _coreCodec;
    }
  } catch {
    // Core not installed / unloadable — fall back to the local decoder.
  }
  _coreCodec = null;
  return _coreCodec;
}

// ---------------------------------------------------------------------------
// f16 → f32 upcast (exact bit math)
// ---------------------------------------------------------------------------

const _f32View = new DataView(new ArrayBuffer(4));

/**
 * Convert one IEEE-754 binary16 (little-endian bytes `lo`,`hi`) into a JS
 * number, widening to f32 first. Mirrors Rust `half::f16::to_f32` exactly:
 * sign/exponent/mantissa widening, subnormals normalized, inf/NaN preserved.
 * Every f32 is exactly representable as a JS number (f64), so this is
 * bit-exact with core's `f16.to_f32()` exposed to JS.
 */
function f16LeBytesToNumber(lo: number, hi: number): number {
  const half = lo | (hi << 8); // little-endian u16
  const sign = (half & 0x8000) << 16; // sign bit moved to f32 position
  const exp = (half & 0x7c00) >> 10; // 5-bit exponent
  const mant = half & 0x03ff; // 10-bit mantissa

  let bits: number;
  if (exp === 0) {
    if (mant === 0) {
      bits = sign; // signed zero
    } else {
      // Subnormal f16: normalize into a normal f32.
      let m = mant;
      let e = -14;
      while ((m & 0x0400) === 0) {
        m <<= 1;
        e -= 1;
      }
      m &= 0x03ff; // drop the implicit leading 1
      e += 127; // rebias to f32
      bits = sign | (e << 23) | (m << 13);
    }
  } else if (exp === 31) {
    // Inf or NaN — preserve payload bits in the f32 mantissa.
    bits = sign | 0x7f800000 | (mant << 13);
  } else {
    bits = sign | ((exp + 112) << 23) | (mant << 13); // normal: rebias 15→127
  }

  _f32View.setUint32(0, bits >>> 0, true);
  return _f32View.getFloat32(0, true);
}

// ---------------------------------------------------------------------------
// READ — local universal decoder (the actual parity fix)
// ---------------------------------------------------------------------------

/**
 * Decode any pre-encryption embedding payload — canonical f16, legacy JSON, or
 * legacy f32 binary — into a `number[]`, using ONLY local computation (no core
 * binding). Dispatch matches core's `decode_embedding_universal` exactly. See
 * the file header for the dispatch table. Throws on malformed JSON, invalid
 * base64, or an uninterpretable byte length.
 */
// Strict base64 validation (coordinator review): Buffer.from(s, 'base64') is
// silently LAX — it skips invalid characters and never throws, so a corrupted
// or foreign payload could decode to a plausible-length buffer of garbage
// floats instead of erroring. Core (Rust) and Python both REJECT invalid
// base64; the local fallback must match that contract. Standard alphabet,
// padded, whole-string.
// Note: this validates alphabet + padding shape but not trailing-bit
// canonicality (core's Rust engine also rejects non-zero trailing bits in
// the final block) — slightly more lenient in the SAFE direction only; no
// real encoder emits non-canonical trailing bits.
const BASE64_STRICT = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

export function decodeEmbeddingLocal(payload: string): number[] {
  const trimmed = payload.trim();

  // Legacy TS plugin format: a decrypted payload beginning with '[' is a JSON
  // float array (the historical JSON.stringify(vec) write).
  if (trimmed.startsWith('[')) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch (e) {
      throw new Error(
        `invalid JSON embedding array: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
    if (!Array.isArray(parsed) || !parsed.every((v) => typeof v === 'number')) {
      throw new Error('JSON embedding payload is not an array of numbers');
    }
    return parsed as number[];
  }

  if (!BASE64_STRICT.test(trimmed)) {
    throw new Error('base64 decode failed: payload contains non-base64 characters');
  }
  const buf: Buffer = Buffer.from(trimmed, 'base64');

  if (buf.length === EMBEDDING_DIMS * 2) {
    // Canonical f16 payload (640 * 2 bytes).
    const out = new Array<number>(EMBEDDING_DIMS);
    for (let i = 0; i < EMBEDDING_DIMS; i++) {
      out[i] = f16LeBytesToNumber(buf[i * 2], buf[i * 2 + 1]);
    }
    return out;
  }

  if (buf.length % 4 === 0) {
    // Legacy f32 binary payload (old Python '<Nf' / the MCP write), OR any
    // non-640-d vector packed as f32. 0 bytes → empty vector.
    const count = buf.length / 4;
    const out = new Array<number>(count);
    for (let i = 0; i < count; i++) {
      out[i] = buf.readFloatLE(i * 4);
    }
    return out;
  }

  throw new Error(
    `decoded embedding buffer has length ${buf.length} (expected ${
      EMBEDDING_DIMS * 2
    } for f16, or a multiple of 4 for f32)`,
  );
}

/**
 * Decode with an explicit core binding (injection seam). When `core` is
 * non-null it is preferred (byte-exact parity with core); otherwise the local
 * decoder runs. Production code calls {@link decodeEmbeddingUniversal}.
 */
export function decodeEmbeddingWithCore(
  payload: string,
  core: CoreEmbeddingCodec | null,
): number[] {
  if (core) return Array.from(core.decodeEmbeddingUniversal(payload));
  return decodeEmbeddingLocal(payload);
}

/**
 * Decode any embedding payload, preferring the installed `@totalreclaw/core`
 * binding when it exposes `decodeEmbeddingUniversal`, else the local fallback.
 */
export function decodeEmbeddingUniversal(payload: string): number[] {
  return decodeEmbeddingWithCore(payload, resolveCoreCodec());
}

// ---------------------------------------------------------------------------
// WRITE — canonical f16 (core-binding only) or legacy f32 fallback
// ---------------------------------------------------------------------------

/**
 * Encode a vector into the pre-encryption payload, with an explicit core
 * binding (injection seam). When `core` is present this delegates to
 * `core.encodeEmbeddingCanonical` (fail-closed on NaN/±inf/overflow — the
 * throw intentionally aborts the store). When `core` is null it returns the
 * marker `'__legacy-f32:use-mcp-packing__'`; the MCP `encryptEmbedding` helper
 * then packs the legacy f32 binary itself (its historical write). The f32→f16
 * rounding is deliberately left to core, never reimplemented here.
 */
export const LEGACY_F32_MARKER = '__totalreclaw-legacy-f32-packing__';

export function encodeEmbeddingPayloadWithCore(
  embedding: number[],
  core: CoreEmbeddingCodec | null,
): string {
  if (core) return core.encodeEmbeddingCanonical(embedding);
  return LEGACY_F32_MARKER;
}

/**
 * Encode a vector into the pre-encryption payload, preferring canonical f16 via
 * `@totalreclaw/core.encodeEmbeddingCanonical` when the installed core exposes
 * it, else the legacy-f32 marker (the MCP helper packs f32 itself). Writes flip
 * to canonical automatically when the core dependency bumps past the codec.
 */
export function encodeEmbeddingPayload(embedding: number[]): string {
  return encodeEmbeddingPayloadWithCore(embedding, resolveCoreCodec());
}
