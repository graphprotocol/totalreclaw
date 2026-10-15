/**
 * Subgraph mapping for the TotalReclaw DataEdge contracts.
 *
 * TWO data sources, ONE entity store (audit finding 1.1 — write authorization):
 *
 *   - handleLogV2  (EventfulDataEdgeV2, `Log(address indexed sender, bytes)`):
 *       the LIVE, authenticated path. Ownership is derived from `sender`
 *       (msg.sender = the Smart Account), NEVER from the payload. A fact may
 *       only be CREATED by a sender (owner := sender), or UPDATED by a write
 *       whose sender equals the fact's existing owner. Any other write — the
 *       tombstone / overwrite / hijack an attacker could previously perform by
 *       calling the contract directly with someone else's fact id — is rejected.
 *
 *   - handleLog    (legacy EventfulDataEdge, `Log(bytes)`):
 *       the pre-V2 contract, kept so existing facts stay readable. Behaviour is
 *       UNCHANGED (permissive: trusts the payload `owner`, last-write-wins). It
 *       is "frozen" at cutover by setting `endBlock` on this data source in the
 *       manifest (see the YAMLs) — past that block the legacy contract is no
 *       longer indexed, so new direct-call tampering on the old address can no
 *       longer affect the indexed view. Existing (possibly already-tampered)
 *       facts are carried as-is; this was an accepted trade (no healing).
 *
 * Both paths share `upsertFact` below for the field-writing + blind-index +
 * sequence bookkeeping, which is identical regardless of how ownership was
 * established.
 *
 * The subgraph stores only ciphertext and SHA-256 blind-index hashes; it never
 * sees plaintext.
 */

import { Bytes, BigDecimal, BigInt, log } from "@graphprotocol/graph-ts";
import { Log as LogLegacy } from "../generated/EventfulDataEdge/EventfulDataEdge";
import { Log as LogV2 } from "../generated/EventfulDataEdgeV2/EventfulDataEdgeV2";
import { Fact, BlindIndex, GlobalState } from "../generated/schema";
import { decodeFact, DecodedFact } from "./protobuf";

const GLOBAL_STATE_ID = "global";
const INACTIVE_THRESHOLD = BigDecimal.fromString("0.3");

/**
 * Parse a subset of ISO 8601 timestamps into Unix seconds (BigInt).
 *
 * Supports formats emitted by TotalReclaw clients:
 *   "YYYY-MM-DDTHH:MM:SSZ"
 *   "YYYY-MM-DDTHH:MM:SS.fffZ"
 *   "YYYY-MM-DDTHH:MM:SS+00:00"
 *
 * Returns BigInt.zero() on parse failure (caller falls back to block timestamp).
 */
function parseISO8601ToUnix(iso: string): BigInt {
  // Minimum valid: "YYYY-MM-DDTHH:MM:SSZ" = 20 chars
  if (iso.length < 20) return BigInt.zero();

  let year = I32.parseInt(iso.substring(0, 4)) as i32;
  let month = I32.parseInt(iso.substring(5, 7)) as i32;
  let day = I32.parseInt(iso.substring(8, 10)) as i32;
  let hour = I32.parseInt(iso.substring(11, 13)) as i32;
  let minute = I32.parseInt(iso.substring(14, 16)) as i32;
  let second = I32.parseInt(iso.substring(17, 19)) as i32;

  // Basic sanity checks
  if (year < 2024 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31) {
    return BigInt.zero();
  }
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 59) {
    return BigInt.zero();
  }

  // Days per month (non-leap)
  let daysInMonth: i32[] = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  let isLeap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);

  // Count days from Unix epoch (1970-01-01) to the given date
  let days: i64 = 0;
  for (let y: i32 = 1970; y < year; y++) {
    let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    days += leap ? 366 : 365;
  }
  for (let m: i32 = 1; m < month; m++) {
    days += daysInMonth[m] as i64;
    if (m == 2 && isLeap) {
      days += 1;
    }
  }
  days += (day - 1) as i64;

  let totalSeconds: i64 = days * 86400 + (hour as i64) * 3600 + (minute as i64) * 60 + (second as i64);

  return BigInt.fromI64(totalSeconds);
}

function getOrCreateGlobalState(): GlobalState {
  let state = GlobalState.load(GLOBAL_STATE_ID);
  if (state == null) {
    state = new GlobalState(GLOBAL_STATE_ID);
    state.nextSequenceId = BigInt.fromI32(1);
    state.totalFacts = BigInt.zero();
    state.lastUpdated = BigInt.zero();
  }
  return state;
}

/**
 * Write (create or update) a Fact and its BlindIndex entries. Shared by both
 * handlers; ownership (`owner`) and authorization are decided by the caller.
 *
 * @param factId   resolved entity id (payload id, or txHash-logIndex fallback)
 * @param owner    the owner to record (legacy: payload/tx.from; V2: sender)
 * @param decoded  the already-decoded payload
 * @param isNew    whether this is a brand-new entity (for the totalFacts count)
 */
function upsertFact(
  factId: string,
  owner: Bytes,
  decoded: DecodedFact,
  isNew: boolean,
  blockNumber: BigInt,
  blockTimestamp: BigInt,
  txHash: Bytes
): void {
  let fact = Fact.load(factId);
  if (fact == null) {
    fact = new Fact(factId);
  }

  fact.owner = owner;
  fact.encryptedBlob = decoded.encryptedBlob;
  fact.encryptedEmbedding = decoded.encryptedEmbedding;
  fact.decayScore = decoded.decayScore;
  fact.isActive = decoded.decayScore.ge(INACTIVE_THRESHOLD);
  fact.contentFp = decoded.contentFp;
  fact.version = decoded.version;
  fact.blockNumber = blockNumber;
  fact.timestamp = blockTimestamp;
  fact.txHash = txHash;

  // Client-generated per-fact timestamp (protobuf field 2); fall back to block
  // timestamp if missing or malformed.
  let parsedCreatedAt = parseISO8601ToUnix(decoded.timestamp);
  fact.createdAt = parsedCreatedAt.gt(BigInt.zero()) ? parsedCreatedAt : blockTimestamp;

  // Monotonic sequence id.
  let state = getOrCreateGlobalState();
  fact.sequenceId = state.nextSequenceId;
  state.nextSequenceId = state.nextSequenceId.plus(BigInt.fromI32(1));
  if (isNew) {
    state.totalFacts = state.totalFacts.plus(BigInt.fromI32(1));
  }
  state.lastUpdated = blockTimestamp;
  state.save();

  fact.save();

  // Inverted BlindIndex entities for hash_in search.
  let indices = decoded.blindIndices;
  for (let i = 0; i < indices.length; i++) {
    let hash = indices[i];
    let indexId = factId + "-" + hash;
    let blindIndex = new BlindIndex(indexId);
    blindIndex.hash = hash;
    blindIndex.fact = factId;
    blindIndex.owner = owner;
    blindIndex.save();
  }

  log.info("Indexed fact {} with {} blind indices for owner {}", [
    factId,
    indices.length.toString(),
    owner.toHexString(),
  ]);
}

/**
 * LIVE path — EventfulDataEdgeV2 `Log(address indexed sender, bytes data)`.
 *
 * Ownership comes from `sender` (the Smart Account), not the payload. A write
 * may create a fact or update one it already owns; a write whose sender is not
 * the existing owner is rejected (this is the audit-1.1 fix).
 */
export function handleLogV2(event: LogV2): void {
  let data = event.params.data;
  if (data.length == 0) {
    log.warning("V2: empty Log event in tx {}", [event.transaction.hash.toHexString()]);
    return;
  }

  let sender = event.params.sender;

  // Resolve the fact id: payload id, else a per-event fallback. The fallback is
  // globally unique, so it can only ever create a fresh (sender-owned) fact.
  let decoded = decodeFact(data);
  let factId = decoded.id;
  if (factId == "") {
    factId = event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  }

  // Authorization: only the owner may update an existing fact.
  let existing = Fact.load(factId);
  let isNew = existing == null;
  if (existing != null && existing.owner != sender) {
    log.warning(
      "V2: rejected write to fact {} — sender {} is not owner {} (tx {})",
      [
        factId,
        sender.toHexString(),
        existing.owner.toHexString(),
        event.transaction.hash.toHexString(),
      ]
    );
    return;
  }

  upsertFact(
    factId,
    sender,
    decoded,
    isNew,
    event.block.number,
    event.block.timestamp,
    event.transaction.hash
  );
}

/**
 * LEGACY path — EventfulDataEdge `Log(bytes data)`. Behaviour unchanged
 * (permissive, trusts payload owner, last-write-wins). Frozen at cutover via
 * the data source's `endBlock` in the manifest — see the mapping-level comment.
 */
export function handleLog(event: LogLegacy): void {
  let data = event.params.data;
  if (data.length == 0) {
    log.warning("Empty Log event in tx {}", [event.transaction.hash.toHexString()]);
    return;
  }

  let decoded = decodeFact(data);
  let factId = decoded.id;
  if (factId == "") {
    factId = event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  }

  let owner: Bytes;
  if (decoded.owner != "") {
    owner = Bytes.fromHexString(decoded.owner);
  } else {
    owner = event.transaction.from;
  }

  let isNew = Fact.load(factId) == null;
  upsertFact(
    factId,
    owner,
    decoded,
    isNew,
    event.block.number,
    event.block.timestamp,
    event.transaction.hash
  );
}
