// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @dev Packed UserOperation struct from ERC-4337 v0.7.
 *
 * Defined locally for the cred-5 session-key delegation work (the
 * `contracts/` package is not currently wired to the `account-abstraction`
 * npm package — see `interfaces/IEntryPoint.sol` for the matching minimal
 * pattern). When account-abstraction v0.7 contracts are pulled in as a
 * Foundry lib (cred-5 stage 2), this file is removed in favour of the
 * canonical import.
 *
 * Field shape matches the v0.7 EIP-4337 standard verbatim — do not
 * reorder, the layout is consumed by `validateUserOp` validators that
 * compute the hash via abi.encodePacked.
 */
struct PackedUserOperation {
    address sender;
    uint256 nonce;
    bytes initCode;
    bytes callData;
    bytes32 accountGasLimits;
    uint256 preVerificationGas;
    bytes32 gasFees;
    bytes paymasterAndData;
    bytes signature;
}
