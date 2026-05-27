// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {PackedUserOperation} from "./IUserOperation.sol";

/**
 * @title ISessionKeyModule
 * @notice ERC-4337 module that validates UserOperations signed by a
 *         delegated session key (per cred spec §4.2). Master wallet
 *         keypair stays on the SPA; agents (Hermes etc.) receive only
 *         a scope-bounded session key.
 *
 * Cross-spec invariants (load-bearing — see cred spec §4.2):
 * - `validateSessionKeyUserOp` MUST decode any `executeBatch(...)`
 *   inner-calls and validate each one against the session-key scope.
 *   Validating only the outer `executeBatch` selector breaks the imp
 *   spec OQ-A cost model (cf. `docs/specs/imp/281-gnosis-batching-chain-gate.md`).
 * - CREATE2 address of the parent Smart Account MUST remain byte-equal
 *   pre- and post-module-install (cred §8 R2).
 * - No TTL on the session key (cred spec §3.1 / PRD-01 §9 Q2).
 *   Lifetime is bounded only by explicit revoke from the SPA.
 *
 * This file is the contract surface only — the implementation lives in
 * `SessionKeyModule.sol`. Stub implementation lands in cred-5 stage 1
 * (this PR); per-inner-call scope validation in cred-6; cross-chain
 * deploy in cred-7.
 */
interface ISessionKeyModule {
    // --- Errors ---

    error SessionKeyNotActive(address account, address signer);
    error SessionKeyScopeMismatch(address account, address signer);
    error SessionKeyInvalidSignature();
    error SessionKeyReplay(uint256 nonce);

    // --- Events ---

    /// @notice Emitted on the first UserOp that uses a fresh session key
    ///         (lazy-install pattern — no separate `installSessionKey()` call).
    event SessionKeyInstalled(
        address indexed account,
        address indexed signer,
        uint256 nonce
    );

    /// @notice Emitted when the master wallet (via the SPA) revokes a
    ///         session key. Idempotent — revoking an unknown or
    ///         already-revoked signer is a no-op (PRD-01 §11 R4).
    event SessionKeyRevoked(address indexed account, address indexed signer);

    // --- Mutating ---

    /**
     * @notice Called by the Smart Account during `validateUserOp`.
     * @return validationData `0` on success, `SIG_VALIDATION_FAILED`
     *         (constant `1`) on any mismatch. The v0.7 time-range packing
     *         is NOT used — there is no TTL.
     *
     * Cross-spec invariant: if `userOp.callData` is an `executeBatch(...)`
     * invocation, this function MUST decode the inner call array and
     * verify EACH inner call individually against the session-key scope
     * (`target` + `selectors`). See cred spec §4.2 + imp OQ-A.
     */
    function validateSessionKeyUserOp(
        PackedUserOperation calldata userOp,
        bytes32 userOpHash
    ) external returns (uint256 validationData);

    /**
     * @notice Owner-only — invoked from the master wallet via the SPA
     *         "revoke" button. Idempotent.
     * @param account The Smart Account address whose session key is being
     *                revoked. Caller MUST be the master wallet of `account`.
     * @param signer  The session-key signer address to revoke.
     */
    function revokeSessionKey(address account, address signer) external;

    // --- Views ---

    /// @notice Used by the SPA "active sessions" list and by Hermes for
    ///         post-revoke clean-up surfacing.
    function isSessionKeyActive(
        address account,
        address signer
    ) external view returns (bool);

    /// @notice Returns the grant metadata stored on install. Empty
    ///         arrays / zero values for unknown signers (no revert).
    function getSessionKeyGrant(
        address account,
        address signer
    )
        external
        view
        returns (
            uint256 nonce,
            uint256 issuedAt,
            bytes4[] memory selectors,
            address target
        );
}
