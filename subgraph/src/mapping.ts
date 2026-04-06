/**
 * Subgraph mapping for EventfulDataEdge.
 *
 * Handles Log(bytes) events emitted by the EventfulDataEdge contract.
 * Each event contains an encrypted Protobuf-serialized TotalReclawFact.
 * The mapping extracts metadata fields and stores them as Fact records
 * with inverted BlindIndex entities for efficient hash_in queries.
 *
 * IMPORTANT: The subgraph stores encrypted data. It never sees plaintext.
 * The blind_indices are SHA-256 hashes — the subgraph can match them but
 * cannot reverse them to learn the original content.
 */

import { Bytes, BigDecimal, BigInt, log } from "@graphprotocol/graph-ts";
import { Log } from "../generated/EventfulDataEdge/EventfulDataEdge";
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

export function handleLog(event: Log): void {
  let data = event.params.data;
  if (data.length == 0) {
    log.warning("Empty Log event in tx {}", [event.transaction.hash.toHexString()]);
    return;
  }

  let decoded = decodeFact(data);

  // Use Protobuf ID or fall back to tx hash + log index
  let factId = decoded.id;
  if (factId == "") {
    factId = event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  }

  let fact = Fact.load(factId);
  let isNew = fact == null;
  if (fact == null) {
    fact = new Fact(factId);
  }

  // Owner: from Protobuf or tx.from
  let owner: Bytes;
  if (decoded.owner != "") {
    owner = Bytes.fromHexString(decoded.owner);
  } else {
    owner = event.transaction.from;
  }
  fact!.owner = owner;

  fact!.encryptedBlob = decoded.encryptedBlob;
  fact!.encryptedEmbedding = decoded.encryptedEmbedding;
  fact!.decayScore = decoded.decayScore;
  fact!.isActive = decoded.decayScore.ge(INACTIVE_THRESHOLD);
  fact!.contentFp = decoded.contentFp;
  fact!.agentId = decoded.agentId;
  fact!.version = decoded.version;
  fact!.source = decoded.source;
  fact!.blockNumber = event.block.number;
  fact!.timestamp = event.block.timestamp;
  fact!.txHash = event.transaction.hash;

  // Client-generated per-fact timestamp (from protobuf field 2).
  // Falls back to block timestamp if field 2 is missing or malformed.
  let parsedCreatedAt = parseISO8601ToUnix(decoded.timestamp);
  if (parsedCreatedAt.gt(BigInt.zero())) {
    fact!.createdAt = parsedCreatedAt;
  } else {
    fact!.createdAt = event.block.timestamp;
  }

  // Assign monotonic sequence ID
  let state = getOrCreateGlobalState();
  fact!.sequenceId = state.nextSequenceId;
  state.nextSequenceId = state.nextSequenceId.plus(BigInt.fromI32(1));
  if (isNew) {
    state.totalFacts = state.totalFacts.plus(BigInt.fromI32(1));
  }
  state.lastUpdated = event.block.timestamp;
  state.save();

  fact!.save();

  // Create inverted BlindIndex entities
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
    owner.toHexString()
  ]);
}
