// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SessionKeyModule} from "../contracts/SessionKeyModule.sol";
import {PackedUserOperation} from "../contracts/interfaces/IUserOperation.sol";

/**
 * @title SessionKeyModuleTest
 * @notice Foundry test scaffold for cred-5 (per spec §6.1, the twelve
 *         unit tests that gate stage-2 implementation).
 *
 *         STAGE 1 (this PR): scaffold only. Tests are `vm.skip(true)`'d so
 *         CI passes on the stub contract. Each test body lands as the
 *         matching implementation work-leaf ships:
 *
 *         - cred-5 stage 2: the 4 validator unit tests below.
 *         - cred-6: `test_session_key_rejects_batch_with_out_of_scope_inner_call`
 *           and `test_session_key_accepts_batch_with_all_in_scope_inner_calls`.
 *         - cred-7: deploy invariant + cross-chain CREATE2 byte-equality.
 *
 *         The load-bearing invariant `test_create2_address_unchanged_after_module_install`
 *         is intentionally placed first — it is the test that cred-5 stage 2
 *         CANNOT ship without (CREATE2 byte-equality is the spec §6 §8
 *         R2 invariant).
 */
contract SessionKeyModuleTest is Test {
    SessionKeyModule internal module;

    function setUp() public {
        module = new SessionKeyModule();
    }

    // -------------------------------------------------------------------------
    // Load-bearing invariant — Smart Account CREATE2 address byte-equality
    // pre- and post-install. Cred-5 stage 2 CANNOT ship without this test
    // passing on both Base Sepolia and Gnosis fixtures (spec §6 R2).
    // -------------------------------------------------------------------------

    function test_create2_address_unchanged_after_module_install() public {
        vm.skip(true);
        // Cred-5 stage 2 body:
        //   address pre = computeSimpleAccountAddress(salt);
        //   /* install SessionKeyModule via kernel-v3 hookManager */
        //   address post = computeSimpleAccountAddress(salt);
        //   assertEq(pre, post, "CREATE2 byte-equality invariant broken");
    }

    // -------------------------------------------------------------------------
    // Spec §6.1 — validator unit tests (cred-5 stage 2)
    // -------------------------------------------------------------------------

    function test_validate_rejects_unknown_target() public {
        vm.skip(true);
        // Cred-5 stage 2 body.
    }

    function test_validate_rejects_unknown_selector() public {
        vm.skip(true);
        // Cred-5 stage 2 body.
    }

    function test_first_call_installs_grant_and_emits_event() public {
        vm.skip(true);
        // Cred-5 stage 2 body — verifies lazy-install + SessionKeyInstalled.
    }

    function test_revoke_idempotent() public {
        vm.skip(true);
        // Cred-5 stage 2 body — revoking unknown/already-revoked signer
        // is a no-op (PRD-01 §11 R4).
    }

    function test_validate_invalid_signature_returns_failed() public {
        vm.skip(true);
    }

    function test_get_session_key_grant_empty_for_unknown_signer() public {
        vm.skip(true);
    }

    function test_is_session_key_active_false_after_revoke() public {
        vm.skip(true);
    }

    function test_replay_nonce_protection() public {
        vm.skip(true);
    }

    // -------------------------------------------------------------------------
    // Spec §4.2 cross-spec invariant — per-inner-call scope validation
    // under executeBatch. Lands in cred-6.
    // -------------------------------------------------------------------------

    function test_session_key_rejects_batch_with_out_of_scope_inner_call() public {
        vm.skip(true);
        // Cred-6 body.
    }

    function test_session_key_accepts_batch_with_all_in_scope_inner_calls() public {
        vm.skip(true);
        // Cred-6 body. Plus `forge snapshot` enforces per-inner-call gas
        // overhead < 5K (spec §6.1).
    }

    // -------------------------------------------------------------------------
    // Stage-1 sanity checks — the stubs return the expected safe defaults.
    // These DO run (no skip) so we get a green CI baseline for stage 1.
    // -------------------------------------------------------------------------

    function test_stage1_stub_validate_returns_failed() public {
        PackedUserOperation memory op;
        uint256 vd = module.validateSessionKeyUserOp(op, bytes32(0));
        assertEq(vd, 1, "stage-1 stub must safe-default to SIG_VALIDATION_FAILED");
    }

    function test_stage1_stub_is_session_key_active_false() public {
        assertFalse(module.isSessionKeyActive(address(0), address(0)));
    }

    function test_stage1_stub_revoke_reverts_with_stage_marker() public {
        vm.expectRevert(bytes("SessionKeyModule: revokeSessionKey not implemented (cred-5 stage 2)"));
        module.revokeSessionKey(address(0), address(0));
    }

    function test_stage1_stub_get_grant_returns_empty() public {
        (uint256 nonce, uint256 issuedAt, bytes4[] memory selectors, address target) =
            module.getSessionKeyGrant(address(0), address(0));
        assertEq(nonce, 0);
        assertEq(issuedAt, 0);
        assertEq(selectors.length, 0);
        assertEq(target, address(0));
    }
}
