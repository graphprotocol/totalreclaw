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
    // under executeBatch (cred-6). Every inner is validated against
    // grant.target, value==0, and selector ∈ grant.selectors.
    // ============================================================

    function test_malformed_executeBatch_calldata_rejected() public {
        _installGrant(1);

        // Outer selector is executeBatch (in the allowlist) but the payload
        // is too short to abi.decode into (address[], uint256[], bytes[]).
        // The validator's self-external try/catch must surface the decode
        // revert as SIG_VALIDATION_FAILED, not bubble it up.
        bytes32 hash2 = keccak256("malformed-batch");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        bytes memory batchCallData = abi.encodePacked(EXECUTE_BATCH_SEL, hex"00");
        PackedUserOperation memory op = _makePackedOp(batchCallData, ecdsaSig);

        uint256 vd = account.callValidator(module, op, hash2);
        assertEq(vd, 1, "malformed executeBatch payload must SIG_VALIDATION_FAILED");
    }

    function test_session_key_rejects_batch_with_out_of_scope_inner_call() public {
        _installGrant(1);

        // 3-inner batch: first two in-scope, third targets a different
        // address. Per spec §4.2 step 4 ("Return SIG_VALIDATION_FAILED on
        // the first non-matching inner"), this rejects.
        address[] memory targets = new address[](3);
        uint256[] memory values = new uint256[](3);
        bytes[] memory datas = new bytes[](3);

        targets[0] = DATA_EDGE;
        targets[1] = DATA_EDGE;
        targets[2] = address(0xBADC0DE); // out-of-scope target
        for (uint256 i = 0; i < 3; i++) {
            values[i] = 0;
            datas[i] = abi.encodePacked(EXECUTE_SEL, uint256(i)); // selector ∈ allowlist
        }

        bytes32 h = keccak256("out-of-scope-inner");
        bytes memory sig = _signWithKey(signerPriv, h);
        bytes memory cd = abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        uint256 vd = account.callValidator(module, op, h);
        assertEq(vd, 1, "single out-of-scope inner must fail the whole batch");
    }

    function test_session_key_accepts_batch_with_all_in_scope_inner_calls() public {
        _installGrant(1);

        // 3-inner batch: every inner targets DATA_EDGE with value=0 and an
        // allowed selector. Validator must return SIG_VALIDATION_SUCCESS.
        bytes memory cd = _inScopeBatchCallData(3);

        bytes32 h = keccak256("all-in-scope-3");
        bytes memory sig = _signWithKey(signerPriv, h);
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        uint256 vd = account.callValidator(module, op, h);
        assertEq(vd, 0, "all-in-scope batch must SIG_VALIDATION_SUCCESS");
    }

    function test_session_key_rejects_batch_with_nonzero_value_inner() public {
        _installGrant(1);

        // Session keys cannot move ETH (spec §4.2 + invariant 3). Any
        // values[i] != 0 → reject.
        address[] memory targets = new address[](2);
        uint256[] memory values = new uint256[](2);
        bytes[] memory datas = new bytes[](2);
        targets[0] = DATA_EDGE;
        targets[1] = DATA_EDGE;
        values[0] = 0;
        values[1] = 1 wei;
        datas[0] = abi.encodePacked(EXECUTE_SEL, uint256(0));
        datas[1] = abi.encodePacked(EXECUTE_SEL, uint256(1));

        bytes32 h = keccak256("nonzero-value-inner");
        bytes memory sig = _signWithKey(signerPriv, h);
        bytes memory cd = abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        uint256 vd = account.callValidator(module, op, h);
        assertEq(vd, 1, "any nonzero value in batch must SIG_VALIDATION_FAILED");
    }

    function test_session_key_rejects_batch_with_out_of_scope_inner_selector() public {
        // Install grant with ONLY execute in the allowlist (no executeBatch
        // as inner selector). Then batch with an inner whose selector is
        // not in the grant — reject.
        bytes4[] memory onlyExec = new bytes4[](1);
        onlyExec[0] = EXECUTE_BATCH_SEL; // outer must be in grant for the batch path to be reachable

        SessionKeyModule.PermissionGrant memory grant = _buildGrant(
            address(account),
            signer,
            DATA_EDGE,
            onlyExec,
            1
        );
        bytes32 installH = keccak256("install-batch-only");
        // Install batch: outer = executeBatch (in grant), inner = executeBatch (in grant).
        bytes memory installCd = _batchWithSingleInner(EXECUTE_BATCH_SEL);
        bytes memory installSig = abi.encode(grant, _signWithKey(signerPriv, installH));
        PackedUserOperation memory installOp = _makePackedOp(installCd, installSig);
        assertEq(
            account.callValidator(module, installOp, installH),
            0,
            "install must succeed with all-in-scope inner"
        );

        // Now build a batch whose inner uses EXECUTE_SEL — NOT in grant.
        bytes memory cd = _batchWithSingleInner(EXECUTE_SEL);
        bytes32 h = keccak256("inner-sel-bad");
        bytes memory sig = _signWithKey(signerPriv, h);
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        uint256 vd = account.callValidator(module, op, h);
        assertEq(vd, 1, "inner selector not in grant must SIG_VALIDATION_FAILED");
    }

    function test_session_key_rejects_empty_batch() public {
        _installGrant(1);

        // Empty batch is defensively rejected — there is no positive
        // proof of in-scope work being performed.
        address[] memory targets = new address[](0);
        uint256[] memory values = new uint256[](0);
        bytes[] memory datas = new bytes[](0);

        bytes32 h = keccak256("empty-batch");
        bytes memory sig = _signWithKey(signerPriv, h);
        bytes memory cd = abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        uint256 vd = account.callValidator(module, op, h);
        assertEq(vd, 1, "empty executeBatch must SIG_VALIDATION_FAILED");
    }

    /// @dev Gas-budget assertion: per-inner-call validator overhead must be
    ///      under the 5K-gas budget set by the cred-6 issue acceptance
    ///      criteria. Compares the gas of a 2-inner batch and a 6-inner
    ///      batch; the delta divided by 4 is the per-inner cost.
    function test_session_key_batch_per_inner_gas_under_5k_budget() public {
        _installGrant(1);

        bytes memory cd2 = _inScopeBatchCallData(2);
        bytes memory cd6 = _inScopeBatchCallData(6);

        bytes32 h2 = keccak256("batch-gas-2");
        bytes32 h6 = keccak256("batch-gas-6");
        bytes memory sig2 = _signWithKey(signerPriv, h2);
        bytes memory sig6 = _signWithKey(signerPriv, h6);

        PackedUserOperation memory op2 = _makePackedOp(cd2, sig2);
        PackedUserOperation memory op6 = _makePackedOp(cd6, sig6);

        uint256 g0 = gasleft();
        uint256 vd2 = account.callValidator(module, op2, h2);
        uint256 gas2 = g0 - gasleft();
        assertEq(vd2, 0, "2-inner in-scope batch must succeed");

        uint256 g1 = gasleft();
        uint256 vd6 = account.callValidator(module, op6, h6);
        uint256 gas6 = g1 - gasleft();
        assertEq(vd6, 0, "6-inner in-scope batch must succeed");

        // (gas6 - gas2) isolates the cost of the 4 additional inner calls.
        uint256 perInner = (gas6 - gas2) / 4;
        emit log_named_uint("per-inner validator gas", perInner);
        assertLt(perInner, 5000, "per-inner-call overhead must be under 5K gas");
    }

    // ============================================================
    // cred-9 — cross-language parity fixture.
    // Loads `tests/parity/fixtures/session-key-grant-v1.json` and asserts
    // the Solidity-computed EIP-712 grant digest + ecrecover'd master EOA
    // match the values the TS sibling (and the in-flight Python sibling
    // cred-8 #333) commit to. Locks byte-equality across all three
    // implementations of `_grantDigest`.
    // ============================================================

    function test_parity_fixture_matches_solidity_digest_and_recovers_master() public {
        string memory json = vm.readFile(
            "../tests/parity/fixtures/session-key-grant-v1.json"
        );

        SessionKeyModule.PermissionGrant memory g;
        g.version = uint8(vm.parseJsonUint(json, ".grant.version"));
        g.account = vm.parseJsonAddress(json, ".grant.account");
        g.signer = vm.parseJsonAddress(json, ".grant.signer");
        g.target = vm.parseJsonAddress(json, ".grant.scope.target");
        // valueMax / nonce / issuedAt are emitted as JSON strings (uint256-safe
        // across languages); parse via parseJsonString + parseUint.
        g.valueMax = vm.parseUint(
            vm.parseJsonString(json, ".grant.scope.valueMax")
        );
        g.nonce = vm.parseUint(vm.parseJsonString(json, ".grant.nonce"));
        g.issuedAt = vm.parseUint(vm.parseJsonString(json, ".grant.issuedAt"));
        g.chainId = vm.parseJsonUint(json, ".domain.chainId");
        g.verifyingContract = vm.parseJsonAddress(json, ".domain.verifyingContract");
        g.masterSignature = vm.parseJsonBytes(json, ".ecdsa_signature");

        // bytes4[] — no native parseJsonBytes4Array; read each as bytes + cast.
        bytes memory sel0 = vm.parseJsonBytes(json, ".grant.scope.selectors[0]");
        bytes memory sel1 = vm.parseJsonBytes(json, ".grant.scope.selectors[1]");
        g.selectors = new bytes4[](2);
        g.selectors[0] = bytes4(sel0);
        g.selectors[1] = bytes4(sel1);
        assertEq(g.selectors[0], EXECUTE_SEL, "fixture selectors[0] is execute");
        assertEq(g.selectors[1], EXECUTE_BATCH_SEL, "fixture selectors[1] is executeBatch");

        bytes32 expectedDigest = vm.parseJsonBytes32(json, ".eip712_hash");
        address expectedMaster = vm.parseJsonAddress(json, ".accounts.master_address");

        // 1. Solidity-computed digest matches the fixture.
        bytes32 computedDigest = _grantDigestExt(g);
        assertEq(
            computedDigest,
            expectedDigest,
            "Solidity _grantDigest matches fixture.eip712_hash"
        );

        // 2. ecrecover on fixture sig + digest recovers the master EOA. This
        //    is the same primitive `SessionKeyModule.validateSessionKeyUserOp`
        //    uses to authorise the lazy-install path.
        (bytes32 r, bytes32 s, uint8 v) = _splitSig65(g.masterSignature);
        address recovered = ecrecover(expectedDigest, v, r, s);
        assertEq(recovered, expectedMaster, "ecrecover yields fixture master EOA");
    }

    function test_parity_fixture_install_blob_decodes_to_same_grant() public {
        string memory json = vm.readFile(
            "../tests/parity/fixtures/session-key-grant-v1.json"
        );

        bytes memory installSig = vm.parseJsonBytes(json, ".abi_encoded_install_sig");

        // The fixture's install blob must decode through the same shape the
        // module's `_decodeInstallSig` consumes: `(PermissionGrant, bytes)`.
        (SessionKeyModule.PermissionGrant memory g, bytes memory sessionSig) = abi.decode(
            installSig,
            (SessionKeyModule.PermissionGrant, bytes)
        );

        // Sanity-spot-check critical fields against the fixture's grant block.
        assertEq(uint256(g.version), vm.parseJsonUint(json, ".grant.version"));
        assertEq(g.account, vm.parseJsonAddress(json, ".grant.account"));
        assertEq(g.signer, vm.parseJsonAddress(json, ".grant.signer"));
        assertEq(g.target, vm.parseJsonAddress(json, ".grant.scope.target"));
        assertEq(g.chainId, vm.parseJsonUint(json, ".domain.chainId"));
        assertEq(g.verifyingContract, vm.parseJsonAddress(json, ".domain.verifyingContract"));
        assertEq(g.selectors.length, 2);
        assertEq(g.selectors[0], EXECUTE_SEL);
        assertEq(g.selectors[1], EXECUTE_BATCH_SEL);
        assertEq(g.masterSignature.length, 65);

        // The trailing session-side sig in the blob must recover to the
        // fixture's session EOA (same ecrecover primitive the steady-state
        // validator path uses).
        bytes32 mockUserOpHash = vm.parseJsonBytes32(json, ".session.mock_user_op_hash");
        address expectedSession = vm.parseJsonAddress(json, ".accounts.session_address");
        (bytes32 r, bytes32 s, uint8 v) = _splitSig65(sessionSig);
        address recoveredSession = ecrecover(mockUserOpHash, v, r, s);
        assertEq(recoveredSession, expectedSession, "trailing session sig recovers to session EOA");
    }

    /// @dev Splits a 65-byte (r, s, v) signature. Mirrors
    ///      `SessionKeyModule._recoverEcdsa` layout.
    function _splitSig65(
        bytes memory sig
    ) internal pure returns (bytes32 r, bytes32 s, uint8 v) {
        require(sig.length == 65, "sig length");
        // solhint-disable-next-line no-inline-assembly
        assembly {
            r := mload(add(sig, 0x20))
            s := mload(add(sig, 0x40))
            v := byte(0, mload(add(sig, 0x60)))
        }
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

    /// @dev Build an `executeBatch(...)` call whose inners all target
    ///      DATA_EDGE with value=0 and use EXECUTE_SEL as the inner selector
    ///      (which is in the default `_twoSelectors()` grant allowlist).
    function _inScopeBatchCallData(uint256 n) internal pure returns (bytes memory) {
        address[] memory targets = new address[](n);
        uint256[] memory values = new uint256[](n);
        bytes[] memory datas = new bytes[](n);
        for (uint256 i = 0; i < n; i++) {
            targets[i] = DATA_EDGE;
            values[i] = 0;
            datas[i] = abi.encodePacked(EXECUTE_SEL, uint256(i));
        }
        return abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
    }

    /// @dev Build a single-inner `executeBatch(...)` call with the given
    ///      inner selector. Used to test inner-selector allowlist checks.
    function _batchWithSingleInner(bytes4 innerSel) internal pure returns (bytes memory) {
        address[] memory targets = new address[](1);
        uint256[] memory values = new uint256[](1);
        bytes[] memory datas = new bytes[](1);
        targets[0] = DATA_EDGE;
        values[0] = 0;
        datas[0] = abi.encodePacked(innerSel, uint256(0));
        return abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
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
