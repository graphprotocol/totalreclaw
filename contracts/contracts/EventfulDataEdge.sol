// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title EventfulDataEdge
 * @notice Minimal data-availability contract for TotalReclaw.
 *
 * Any call to this contract (via fallback) emits a Log(bytes) event containing
 * the raw calldata. The subgraph indexes these events.
 *
 * Writes are permissionless — all payloads are XChaCha20-Poly1305 encrypted, so there
 * is no confidentiality risk from open writes. The subgraph filters events by
 * the `owner` field in the protobuf payload, ensuring each user only sees their
 * own data. Billing/quota enforcement happens at the relay API layer.
 *
 * Design rationale:
 * - No storage slots — all data lives in events (cheapest on-chain DA).
 * - fallback() instead of a named function — saves ~200 gas on function selector.
 * - Single event type keeps subgraph mapping simple.
 *
 * Gas cost: ~1,200 base + 16 gas/non-zero-byte + 8 gas/zero-byte of calldata.
 * A typical 256-byte encrypted fact costs ~5,300 gas (~$0.0002 on Base L2).
 */
contract EventfulDataEdge {
    /// @notice Emitted for every write. Contains the full encrypted Protobuf payload.
    event Log(bytes data);

    /// @notice Contract owner (deployer). Can transfer ownership.
    address public owner;

    constructor() {
        owner = msg.sender;
    }

    /// @notice Transfer ownership.
    /// @param _newOwner New owner address.
    function transferOwnership(address _newOwner) external {
        require(msg.sender == owner, "Only owner");
        require(_newOwner != address(0), "Invalid owner");
        owner = _newOwner;
    }

    /**
     * @notice Fallback function — emits the full calldata as a Log event.
     * @dev Permissionless. The write path via ERC-4337:
     *      EntryPoint validates UserOp signature -> Smart Account ->
     *      Smart Account calls this contract -> fallback() emits Log(calldata).
     *
     *      Security: payloads are E2E encrypted (XChaCha20-Poly1305). The server/relay
     *      never sees plaintext. Quota enforcement is at the relay API layer.
     */
    fallback() external payable {
        emit Log(msg.data);
    }

    /// @notice Receive ETH (no-op, but required for payable fallback pattern).
    receive() external payable {}
}
