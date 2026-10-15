// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title EventfulDataEdgeV2
 * @notice Append-only data-availability log that records the CALLER alongside
 *         the payload. The subgraph indexes these events.
 *
 * WHY V2 EXISTS (audit finding 1.1 — write authorization):
 *   The original EventfulDataEdge emitted `Log(bytes data)` only — no record
 *   of who called it. The subgraph therefore had to trust the payload's own
 *   `owner` field and keyed facts by a payload-chosen id, so ANY address could
 *   call the contract directly with another user's fact id and owner and
 *   tombstone, overwrite, or reassign that user's facts. (Confirmed on staging
 *   2026-10-15.) Confidentiality was never at risk — payloads are
 *   XChaCha20-Poly1305 ciphertext — but integrity of the indexed view was.
 *
 * THE FIX:
 *   `Log(address indexed sender, bytes data)` records `msg.sender` — the Smart
 *   Account that called `execute()`/`executeBatch()`. A caller cannot forge
 *   another account's address, so the subgraph derives ownership from `sender`
 *   (NOT from the payload) and enforces: a fact may only be created by, or
 *   updated by, its owner. A direct caller trying to touch someone else's fact
 *   shows up as their own address and is rejected by the mapping.
 *
 * WHAT IS UNCHANGED:
 *   Writes remain permissionless (anyone may append their OWN facts) and
 *   payloads remain opaque ciphertext — the server/relay never sees plaintext.
 *   Integrity is enforced at the indexing layer using `sender`; the contract
 *   itself stays a minimal, storage-less, `fallback()`-only emitter.
 *
 * Deployment: CREATE2 (Arachnid factory) with a dedicated salt, so the address
 * is deterministic and byte-equal across chains. See
 * `script/DeployDataEdgeV2.s.sol`.
 */
contract EventfulDataEdgeV2 {
    /// @notice Emitted for every write. `sender` is `msg.sender` (the caller —
    ///         a user's Smart Account on the legitimate path); `data` is the
    ///         full encrypted Protobuf payload (the calldata).
    event Log(address indexed sender, bytes data);

    /**
     * @notice Fallback — emits the caller and the full calldata as a Log event.
     * @dev Permissionless by design. The write path:
     *      EntryPoint validates the UserOp signature -> Smart Account ->
     *      Smart Account calls this contract -> fallback() emits
     *      Log(msg.sender = Smart Account, msg.data). Because `sender` is the
     *      immediate caller, it cannot be spoofed to another account.
     */
    fallback() external payable {
        emit Log(msg.sender, msg.data);
    }

    /// @notice Accept ETH with empty calldata (no-op; required for a payable
    ///         fallback pattern). Emits nothing.
    receive() external payable {}
}
