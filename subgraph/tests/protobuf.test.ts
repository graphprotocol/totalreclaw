import { assert, describe, test } from "matchstick-as/assembly/index";
import { Bytes, BigDecimal, log } from "@graphprotocol/graph-ts";
import { decodeFact, isKeyedScheme, isTombstone } from "../src/protobuf";
import { vectors, Vector } from "./vectors.generated";

// Decoder conformance against the shared vectors
// (tests/parity/fixtures/protobuf-index-scheme-vectors.json, generated from the
// core encoder). The relay decoder loads the same file.

function checkVector(v: Vector): void {
  log.info("vector {}", [v.name]);
  let d = decodeFact(Bytes.fromHexString(v.hex));
  assert.i32Equals(d.indexScheme as i32, v.indexScheme as i32);
  assert.assertTrue(isKeyedScheme(d) == v.counts, v.name + ": counts");
  if (v.hasFull) assert.assertTrue(isTombstone(d) == (v.blobHex == ""), v.name + ": tombstone shape");
  if (v.hasId) assert.stringEquals(d.id, v.id);
  if (v.hasVersion) assert.i32Equals(d.version, v.version);
  if (v.hasFull) {
    assert.stringEquals(d.owner, v.owner);
    assert.stringEquals(d.timestamp, v.timestamp);
    assert.stringEquals(d.contentFp, v.contentFp);
    assert.stringEquals(d.encryptedEmbedding, v.embedding);
    assert.stringEquals(d.encryptedBlob.toHexString(), "0x" + v.blobHex);
    assert.assertTrue(d.decayScore.equals(BigDecimal.fromString(v.decayScore)), v.name + ": decayScore");
    assert.i32Equals(d.blindIndices.length, v.blindIndices.length);
    for (let j = 0; j < v.blindIndices.length; j++) {
      assert.stringEquals(d.blindIndices[j], v.blindIndices[j]);
    }
  }
}

describe("protobuf decoder: shared index_scheme vectors", () => {
  test("vector file is loaded", () => {
    assert.i32Equals(vectors().length, 9);
  });

  // matchstick's AssemblyScript has no closures, so all vectors run in one test;
  // the failing vector's name is in the assertion message.
  test("every vector decodes as specified", () => {
    let all = vectors();
    for (let i = 0; i < all.length; i++) {
      checkVector(all[i]);
    }
  });
});

describe("protobuf decoder: hardening", () => {
  test("oversized key does not alias field 14", () => {
    // key varint = 2^39 + 0x70 (would read as field 14 if narrowed to i32), value 1.
    let d = decodeFact(Bytes.fromHexString("f08080808010" + "01"));
    assert.i32Equals(d.indexScheme as i32, 0);
  });

  test("negative or huge length does not pass the bounds check", () => {
    // field 1, length = 2^32 + 1 (wraps to 1 as i32), then 1 byte.
    let d = decodeFact(Bytes.fromHexString("0a" + "8180808010" + "41"));
    assert.stringEquals(d.id, "");
  });

  test("varint longer than 10 bytes is rejected", () => {
    let d = decodeFact(Bytes.fromHexString("70" + "ffffffffffffffffffff01"));
    assert.i32Equals(d.indexScheme as i32, 0);
  });

  test("duplicate field 14: last varint wins", () => {
    let d = decodeFact(Bytes.fromHexString("7002" + "7001"));
    assert.i32Equals(d.indexScheme as i32, 1);
  });

  test("unknown fields are still skipped", () => {
    // field 15 varint, field 16 length-delimited, then field 14 = 1.
    let d = decodeFact(Bytes.fromHexString("7805" + "82010161" + "7001"));
    assert.i32Equals(d.indexScheme as i32, 1);
  });
});
