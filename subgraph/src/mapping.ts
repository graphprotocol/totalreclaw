/**
 * Subgraph mapping for EventfulDataEdgeV2.
 *
 * Handles `Log(address indexed sender, bytes data)` events. Each event carries
 * an XChaCha20-Poly1305 encrypted Protobuf-serialized TotalReclawFact; the
 * mapping extracts metadata into a Fact record plus inverted BlindIndex
 * entities for `hash_in` search.
 *
 * AUDIT 1.1 — write authorization. The old `EventfulDataEdge` emitted only
 * `Log(bytes)`, so the subgraph had to trust the payload's self-declared
 * `owner` and keyed facts by a client-chosen id in a GLOBAL namespace. Anyone
 * could therefore overwrite, tombstone or reassign any user's facts, or
 * front-run a create to squat an id. V2 records `msg.sender` — the user's
 * Smart Account, which the EVM sets and no caller can forge — and the entity
 * is keyed by `sender:id`, a PER-OWNER namespace:
 *
 *   - Ownership is derived from `sender`, never from the payload.
 *   - A write from address X can only ever address keys beginning `X:`, so it
 *     structurally cannot touch another account's facts (no overwrite, no
 *     reassignment) and cannot squat an id another account will use (their
 *     create lands under a different key). There is no cross-owner collision
 *     left to adjudicate.
 *
 * Idempotent key (tombstone round-trip). Clients receive `fact.id` (the
 * composite `sender:id`) from recall and pass it straight back as the payload
 * id when they forget/pin/retype. So a create sends a bare id while a
 * tombstone sends the already-prefixed one. The key is computed idempotently:
 * if the payload id already begins with the caller's own `sender:` prefix it
 * is used as-is, otherwise it is prefixed. Both resolve to the same entity,
 * with no client change. The prefix check is against `msg.sender`, so a
 * crafted `victim:uuid` from an attacker just lands in the attacker's own
 * namespace (`attacker:victim:uuid`).
 *
 * The subgraph stores only ciphertext and SHA-256 blind-index hashes; it never
 * sees plaintext.
 */

import { Address, BigDecimal, BigInt, log } from "@graphprotocol/graph-ts";
import { Log } from "../generated/EventfulDataEdgeV2/EventfulDataEdgeV2";
import { Fact, BlindIndex, GlobalState } from "../generated/schema";
import { decodeFact } from "./protobuf";

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
 * Per-owner composite entity id. Idempotent: a bare payload id (`uuid`, from a
 * create) is prefixed with `sender:`; an id that already carries the caller's
 * own prefix (`sender:uuid`, round-tripped from recall on a forget/pin) is
 * returned unchanged. The check is against the caller's own address, so a
 * foreign prefix does not satisfy it. The `:` delimiter is unambiguous: a fact
 * id is a uuid (hex + hyphens) and `sender` is `0x…` hex, neither contains `:`.
 */
function compositeId(senderHex: string, payloadId: string): string {
  let prefix = senderHex + ":";
  if (payloadId.startsWith(prefix)) {
    return payloadId;
  }
  return prefix + payloadId;
}

export function handleLog(event: Log): void {
  let data = event.params.data;
  if (data.length == 0) {
    log.warning("V2: empty Log event in tx {}", [event.transaction.hash.toHexString()]);
    return;
  }

  let sender: Address = event.params.sender;
  let senderHex = sender.toHexString();

  let decoded = decodeFact(data);

  // Resolve the client-chosen fact id; fall back to a per-event unique id.
  let payloadId = decoded.id;
  if (payloadId == "") {
    payloadId = event.transaction.hash.toHexString() + "-" + event.logIndex.toString();
  }

  let factId = compositeId(senderHex, payloadId);

  let fact = Fact.load(factId);
  let isNew = fact == null;

  // Defense in depth. The composite key already embeds the caller, so an entity
  // under this key can only have been created by this sender; a mismatch would
  // mean an invariant was violated upstream. Reject rather than overwrite.
  if (fact != null && fact.owner != sender) {
    log.warning("V2: refusing write to {} — sender {} != owner {} (tx {})", [
      factId,
      senderHex,
      fact.owner.toHexString(),
      event.transaction.hash.toHexString(),
    ]);
    return;
  }

  if (fact == null) {
    fact = new Fact(factId);
  }

  // Ownership comes from the on-chain caller, never from the payload.
  fact!.owner = sender;
  fact!.encryptedBlob = decoded.encryptedBlob;
  fact!.encryptedEmbedding = decoded.encryptedEmbedding;
  fact!.decayScore = decoded.decayScore;
  fact!.isActive = decoded.decayScore.ge(INACTIVE_THRESHOLD);
  fact!.contentFp = decoded.contentFp;
  fact!.version = decoded.version;
  fact!.blockNumber = event.block.number;
  fact!.timestamp = event.block.timestamp;
  fact!.txHash = event.transaction.hash;

  // Client-generated per-fact timestamp (protobuf field 2); fall back to block
  // timestamp if missing or malformed.
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

  // Inverted BlindIndex entities for hash_in search, keyed off the composite id.
  let indices = decoded.blindIndices;
  for (let i = 0; i < indices.length; i++) {
    let hash = indices[i];
    let indexId = factId + "-" + hash;
    let blindIndex = new BlindIndex(indexId);
    blindIndex.hash = hash;
    blindIndex.fact = factId;
    blindIndex.owner = sender;
    blindIndex.save();
  }

  log.info("Indexed fact {} with {} blind indices for owner {}", [
    factId,
    indices.length.toString(),
    senderHex,
  ]);
}
