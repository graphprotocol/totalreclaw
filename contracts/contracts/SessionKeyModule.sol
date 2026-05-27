// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ISessionKeyModule} from "./interfaces/ISessionKeyModule.sol";
import {PackedUserOperation} from "./interfaces/IUserOperation.sol";

/**
 * @title SessionKeyModule
 * @notice STAGE-1 SCAFFOLD — cred-5 / PRD-01 / spec
 *         `docs/specs/cred/session-key-delegation.md`.
 *
 *         This file is intentionally a stub. Function bodies revert
 *         until the matching implementation work-leafs land:
 *
 *         - `validateSessionKeyUserOp` real body              → cred-5 stage 2
 *         - per-inner-call `executeBatch` scope validation   → cred-6
 *         - cross-chain Pimlico CREATE2 deploy               → cred-7
 *         - EIP-712 `SessionKeyPermissionGrant` decode       → cred-5 stage 2 + cred-8 parity
 *         - `revokeSessionKey` master-wallet auth check      → cred-5 stage 2
 *
 *         Cross-spec invariants the implementation MUST honour (load-bearing):
 *           1. CREATE2 address of the parent Smart Account MUST remain
 *              byte-equal pre- and post-install. Foundry test
 *              `test_create2_address_unchanged_after_module_install` is
 *              the load-bearing invariant test (cred-5 §6.1).
 *           2. `executeBatch` per-inner-call validation — see cred-6 +
 *              ISessionKeyModule docstring.
 *           3. No TTL. Session keys are valid until explicit revoke.
 *
 *         The chosen ERC-4337 module framework is ZeroDev kernel v3
 *         (cred-5 §11 Q1, Pedro sign-off 2026-05-27 on issue #320).
 *         The kernel-v3 dependency lands under `contracts/lib/` in
 *         cred-5 stage 2; this stage 1 PR adds Foundry tooling +
 *         interface only.
 */
contract SessionKeyModule is ISessionKeyModule {
    // --- ERC-4337 v0.7 validation-data constants ---

    /// @dev Returned by `validateSessionKeyUserOp` on any failure.
    ///      v0.7 EntryPoint treats `1` as SIG_VALIDATION_FAILED (no
    ///      time-range packing — we do not use TTL).
    uint256 internal constant SIG_VALIDATION_FAILED = 1;

    /// @dev Returned on success — time-range packing not used.
    uint256 internal constant SIG_VALIDATION_SUCCESS = 0;

    // --- Storage ---
    // Real storage layout lands in stage 2. For stage 1 we keep this
    // empty — adding storage now risks breaking CREATE2 byte-equality
    // when the layout settles in stage 2. The stage-2 work-leaf will
    // populate this section + freeze the storage layout for upgrade
    // forward-compat.

    // --- Validation ---

    /// @inheritdoc ISessionKeyModule
    function validateSessionKeyUserOp(
        PackedUserOperation calldata, /* userOp */
        bytes32 /* userOpHash */
    ) external pure returns (uint256) {
        // STAGE 1 — body lands in cred-5 stage 2. Reverts in production
        // would brick UserOp validation, so we return SIG_VALIDATION_FAILED
        // to keep the staged module safe-by-default until the real
        // validator ships.
        return SIG_VALIDATION_FAILED;
    }

    /// @inheritdoc ISessionKeyModule
    function revokeSessionKey(address /* account */, address /* signer */) external pure {
        // STAGE 1 — body lands in cred-5 stage 2 (will check master-wallet
        // auth + emit SessionKeyRevoked). Reverting here is safe: any
        // real revoke attempt at stage 1 fails loud rather than silently
        // succeeding.
        revert("SessionKeyModule: revokeSessionKey not implemented (cred-5 stage 2)");
    }

    // --- Views ---

    /// @inheritdoc ISessionKeyModule
    function isSessionKeyActive(
        address, /* account */
        address /* signer */
    ) external pure returns (bool) {
        // STAGE 1 — body lands in cred-5 stage 2.
        return false;
    }

    /// @inheritdoc ISessionKeyModule
    function getSessionKeyGrant(
        address, /* account */
        address /* signer */
    )
        external
        pure
        returns (
            uint256 nonce,
            uint256 issuedAt,
            bytes4[] memory selectors,
            address target
        )
    {
        // STAGE 1 — body lands in cred-5 stage 2.
        return (0, 0, new bytes4[](0), address(0));
    }
}
