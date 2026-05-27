// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SessionKeyModule} from "../contracts/SessionKeyModule.sol";
import {ISessionKeyModule} from "../contracts/interfaces/ISessionKeyModule.sol";
import {PackedUserOperation} from "../contracts/interfaces/IUserOperation.sol";

/**
 * @dev SimpleAccount-style mock — exposes `owner()` (the lookup
 *      SessionKeyModule._smartAccountOwner uses) and forwards a UserOp into
 *      the module so `msg.sender` matches the SA address. This is enough
 *      for cred-5 stage 2 unit tests; cred-7 wires the real kernel-v3
 *      account.
 */
contract MockSmartAccount {
    address public owner;

    constructor(address _owner) {
        owner = _owner;
    }

    function callValidator(
        SessionKeyModule mod,
        PackedUserOperation calldata op,
        bytes32 h
    ) external returns (uint256) {
        return mod.validateSessionKeyUserOp(op, h);
    }

    function callRevoke(SessionKeyModule mod, address signer) external {
        mod.revokeSessionKey(address(this), signer);
    }
}

/**
 * @title SessionKeyModuleTest
 * @notice Foundry tests for cred-5 stage 2 — real validator body.
 *         Per spec `docs/specs/cred/session-key-delegation.md` §6.1.
 *
 *         The vm.skip'd tests in stage 1 are now implemented. Tests that
 *         depend on later work-leafs (cred-6 batch validation, cred-7
 *         cross-chain deploy) remain skipped with a clarifying comment.
 */
contract SessionKeyModuleTest is Test {
    SessionKeyModule internal module;
    MockSmartAccount internal account;
    address internal master;
    uint256 internal masterPriv;
    address internal signer;
    uint256 internal signerPriv;
    address internal constant DATA_EDGE = 0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca;
    bytes4 internal constant EXECUTE_SEL = 0xb61d27f6;
    bytes4 internal constant EXECUTE_BATCH_SEL = 0x47e1da2a;

    function setUp() public {
        module = new SessionKeyModule();

        (master, masterPriv) = makeAddrAndKey("master");
        (signer, signerPriv) = makeAddrAndKey("signer");

        account = new MockSmartAccount(master);
        // Pin the chain id so the EIP-712 domain is stable across forks.
        // Anvil's default is 31337; that's fine for unit tests.
    }

    // ============================================================
    // Load-bearing invariant — CREATE2 byte-equality post install.
    // Lands in cred-7 (requires Pimlico CREATE2 factory + on-chain
    // module install via kernel-v3 hookManager). Stays skipped here.
    // ============================================================

    function test_create2_address_unchanged_after_module_install() public {
        vm.skip(true);
    }

    // ============================================================
    // Spec §6.1 — validator unit tests
    // ============================================================

    function test_first_call_installs_grant_and_emits_event() public {
        bytes4[] memory sels = _twoSelectors();
        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            sels,
            1
        );

        bytes32 userOpHash = keccak256("first-userop");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        bytes memory sig = abi.encode(grant, ecdsaSig);

        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"deadbeef"),
            sig
        );

        vm.expectEmit(true, true, false, true, address(module));
        emit ISessionKeyModule.SessionKeyInstalled(address(account), signer, 1);

        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 0, "first install + execute must succeed");

        assertTrue(module.isSessionKeyActive(address(account), signer));
        (uint256 n, , , address tgt) = module.getSessionKeyGrant(address(account), signer);
        assertEq(n, 1);
        assertEq(tgt, DATA_EDGE);
    }

    function test_validate_rejects_unknown_target() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("wrong-target");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        // Inner target is NOT DataEdge — module must reject.
        bytes memory cd = _executeCallData(address(0xDEAD), 0, hex"00");
        PackedUserOperation memory op = _makePackedOp(cd, ecdsaSig);

        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 1, "out-of-scope target must SIG_VALIDATION_FAILED");
    }

    function test_validate_rejects_unknown_selector() public {
        // Install grant with only EXECUTE_SEL allowed (no executeBatch).
        bytes4[] memory onlyExecute = new bytes4[](1);
        onlyExecute[0] = EXECUTE_SEL;

        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            onlyExecute,
            1
        );

        bytes32 installHash = keccak256("install-1");
        bytes memory installSig = abi.encode(grant, _signWithKey(signerPriv, installHash));
        PackedUserOperation memory installOp = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            installSig
        );
        assertEq(account.callValidator(module, installOp, installHash), 0);

        // Now try a second UserOp whose outer selector is executeBatch — not in scope.
        bytes32 hash2 = keccak256("batch-attempt");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        bytes memory batchCallData = abi.encodePacked(EXECUTE_BATCH_SEL, hex"00");
        PackedUserOperation memory op = _makePackedOp(batchCallData, ecdsaSig);

        uint256 vd = account.callValidator(module, op, hash2);
        assertEq(vd, 1, "out-of-scope selector must SIG_VALIDATION_FAILED");
    }

    function test_revoke_idempotent() public {
        // Revoke a signer that was NEVER installed — must be a no-op (no revert).
        account.callRevoke(module, signer);
        assertFalse(module.isSessionKeyActive(address(account), signer));

        // Install + revoke + revoke again. Second revoke is also a no-op.
        _installGrant(1);
        assertTrue(module.isSessionKeyActive(address(account), signer));

        vm.expectEmit(true, true, false, false, address(module));
        emit ISessionKeyModule.SessionKeyRevoked(address(account), signer);
        account.callRevoke(module, signer);
        assertFalse(module.isSessionKeyActive(address(account), signer));

        // No revert. No second event.
        account.callRevoke(module, signer);
    }

    function test_revoke_only_callable_by_account() public {
        _installGrant(1);
        // Direct call (not from the account) — must revert.
        vm.expectRevert(
            abi.encodeWithSelector(
                ISessionKeyModule.SessionKeyNotActive.selector,
                address(account),
                signer
            )
        );
        module.revokeSessionKey(address(account), signer);
    }

    function test_validate_invalid_signature_returns_failed() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("op-x");
        // Sign with a DIFFERENT key — signer mismatch, module looks up
        // grants[account][recovered] and finds none.
        (, uint256 attackerPriv) = makeAddrAndKey("attacker");
        bytes memory badSig = _signWithKey(attackerPriv, userOpHash);

        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            badSig
        );
        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 1, "wrong signer must SIG_VALIDATION_FAILED");
    }

    function test_get_session_key_grant_empty_for_unknown_signer() public view {
        (uint256 n, uint256 iAt, bytes4[] memory sels, address tgt) = module
            .getSessionKeyGrant(address(account), address(0x1234));
        assertEq(n, 0);
        assertEq(iAt, 0);
        assertEq(sels.length, 0);
        assertEq(tgt, address(0));
    }

    function test_is_session_key_active_false_after_revoke() public {
        _installGrant(1);
        assertTrue(module.isSessionKeyActive(address(account), signer));
        account.callRevoke(module, signer);
        assertFalse(module.isSessionKeyActive(address(account), signer));
    }

    function test_replay_nonce_protection() public {
        // Install nonce 1, revoke, then attempt to re-install nonce 1 → reject.
        _installGrant(1);
        account.callRevoke(module, signer);

        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            _twoSelectors(),
            1
        );
        bytes32 hash2 = keccak256("replay-attempt");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, hash2));
        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            sig
        );

        uint256 vd = account.callValidator(module, op, hash2);
        assertEq(vd, 1, "replayed nonce must SIG_VALIDATION_FAILED");

        // A FRESH nonce (2) should succeed.
        SessionKeyModule.PermissionGrant memory grant2 = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            _twoSelectors(),
            2
        );
        bytes32 hash3 = keccak256("fresh-nonce");
        bytes memory sig2 = abi.encode(grant2, _signWithKey(signerPriv, hash3));
        PackedUserOperation memory op2 = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            sig2
        );
        uint256 vd2 = account.callValidator(module, op2, hash3);
        assertEq(vd2, 0, "fresh nonce must succeed after revoke");
    }

    function test_cross_chain_grant_replay_rejected() public {
        // Grant signed for a DIFFERENT chainId — module must reject.
        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            _twoSelectors(),
            1
        );
        grant.chainId = 999; // not block.chainid
        // Re-sign the master sig over the new digest
        grant.masterSignature = _signMasterGrant(grant);

        bytes32 userOpHash = keccak256("cross-chain");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            sig
        );
        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 1, "cross-chain replay must be rejected");
    }

    function test_master_signature_mismatch_rejects() public {
        // Forge a grant signed by the wrong master.
        (, uint256 attackerPriv) = makeAddrAndKey("attacker-master");

        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            _twoSelectors(),
            1
        );
        // Replace master sig with one signed by attacker.
        bytes32 digest = _grantDigestExt(grant);
        grant.masterSignature = _signWithKey(attackerPriv, digest);

        bytes32 userOpHash = keccak256("forged-master");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            sig
        );
        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 1, "forged master signature must be rejected");
    }

    function test_steady_state_userop_after_install_succeeds() public {
        _installGrant(1);

        // Now a second UserOp with JUST the ecdsa sig (no grant blob).
        bytes32 hash2 = keccak256("steady-state");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"42"),
            ecdsaSig
        );
        uint256 vd = account.callValidator(module, op, hash2);
        assertEq(vd, 0, "steady-state UserOp must succeed without re-sending grant");
    }

    function test_nonzero_value_rejected() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("eth-transfer");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        // value != 0 — session keys cannot move ETH (spec §3.1 valueMax = 0).
        bytes memory cd = _executeCallData(DATA_EDGE, 1 ether, hex"00");
        PackedUserOperation memory op = _makePackedOp(cd, ecdsaSig);

        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 1, "non-zero value must SIG_VALIDATION_FAILED");
    }

    // ============================================================
    // Spec §4.2 cross-spec invariant — per-inner-call validation
    // under executeBatch. Lands in cred-6. Stage 2 SAFE-defaults
    // to SIG_VALIDATION_FAILED for any executeBatch UserOp.
    // ============================================================

    function test_executeBatch_rejected_until_cred6() public {
        _installGrant(1);

        // Even with executeBatch in the grant's allowlist, stage 2 rejects.
        bytes32 hash2 = keccak256("batch-attempt");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        bytes memory batchCallData = abi.encodePacked(EXECUTE_BATCH_SEL, hex"00");
        PackedUserOperation memory op = _makePackedOp(batchCallData, ecdsaSig);

        uint256 vd = account.callValidator(module, op, hash2);
        assertEq(vd, 1, "stage 2 must reject all executeBatch - wait for cred-6");
    }

    function test_session_key_rejects_batch_with_out_of_scope_inner_call() public {
        vm.skip(true); // cred-6
    }

    function test_session_key_accepts_batch_with_all_in_scope_inner_calls() public {
        vm.skip(true); // cred-6
    }

    // ============================================================
    // Helpers
    // ============================================================

    function _twoSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](2);
        s[0] = EXECUTE_SEL;
        s[1] = EXECUTE_BATCH_SEL;
    }

    function _buildGrant(
        address _account,
        address _signer,
        address _target,
        bytes4[] memory _selectors,
        uint256 _nonce
    ) internal view returns (SessionKeyModule.PermissionGrant memory g) {
        g.version = 1;
        g.account = _account;
        g.signer = _signer;
        g.target = _target;
        g.selectors = _selectors;
        g.valueMax = 0;
        g.nonce = _nonce;
        g.issuedAt = block.timestamp;
        g.chainId = block.chainid;
        g.verifyingContract = address(module);
        g.masterSignature = _signMasterGrant(g);
    }

    function _installGrant(uint256 nonce) internal {
        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            _twoSelectors(),
            nonce
        );
        bytes32 userOpHash = keccak256(abi.encode("install-helper", nonce));
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(
            _executeCallData(DATA_EDGE, 0, hex"00"),
            sig
        );
        uint256 vd = account.callValidator(module, op, userOpHash);
        assertEq(vd, 0, "_installGrant helper: install must succeed");
    }

    function _signMasterGrant(
        SessionKeyModule.PermissionGrant memory g
    ) internal view returns (bytes memory) {
        bytes32 digest = _grantDigestExt(g);
        return _signWithKey(masterPriv, digest);
    }

    /// @dev Mirror of `SessionKeyModule._grantDigest`. Kept in the test file
    ///      so the parity contract (cred-9) can also pull from here. If the
    ///      module hash construction changes, update both.
    function _grantDigestExt(
        SessionKeyModule.PermissionGrant memory g
    ) internal pure returns (bytes32) {
        bytes32 domainTypeHash = keccak256(
            "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        );
        bytes32 scopeTypeHash = keccak256(
            "Scope(address target,bytes4[] selectors,uint256 valueMax)"
        );
        bytes32 grantTypeHash = keccak256(
            "SessionKeyPermissionGrant(address account,address signer,Scope scope,uint256 nonce,uint256 issuedAt)Scope(address target,bytes4[] selectors,uint256 valueMax)"
        );

        bytes32 scopeHash = keccak256(
            abi.encode(scopeTypeHash, g.target, keccak256(abi.encodePacked(g.selectors)), g.valueMax)
        );
        bytes32 structHash = keccak256(
            abi.encode(grantTypeHash, g.account, g.signer, scopeHash, g.nonce, g.issuedAt)
        );
        bytes32 domainSep = keccak256(
            abi.encode(
                domainTypeHash,
                keccak256(bytes("TotalReclawSessionKey")),
                keccak256(bytes("1")),
                g.chainId,
                g.verifyingContract
            )
        );
        return keccak256(abi.encodePacked(hex"19_01", domainSep, structHash));
    }

    function _signWithKey(uint256 privKey, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _executeCallData(
        address tgt,
        uint256 value,
        bytes memory data
    ) internal pure returns (bytes memory) {
        return abi.encodePacked(EXECUTE_SEL, abi.encode(tgt, value, data));
    }

    function _makePackedOp(
        bytes memory callData,
        bytes memory sig
    ) internal view returns (PackedUserOperation memory op) {
        op.sender = address(account);
        op.nonce = 0;
        op.initCode = "";
        op.callData = callData;
        op.accountGasLimits = 0;
        op.preVerificationGas = 0;
        op.gasFees = 0;
        op.paymasterAndData = "";
        op.signature = sig;
    }
}
