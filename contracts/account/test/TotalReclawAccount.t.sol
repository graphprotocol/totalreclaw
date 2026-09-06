// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {
    PackedUserOperation
} from "account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {SimpleAccount} from "account-abstraction/contracts/samples/SimpleAccount.sol";
import {TotalReclawAccount} from "../contracts/TotalReclawAccount.sol";

/**
 * @title TotalReclawAccountTest
 * @notice Option E Phase 3 (P3-4) — fast, non-forked unit tests for
 *         `TotalReclawAccount`. Every test here deploys a FRESH
 *         `ERC1967Proxy` pointing at a freshly-deployed implementation
 *         and initialises it with a Foundry-controlled owner keypair --
 *         exactly the deployment shape `SimpleAccountFactory.createAccount`
 *         produces on-chain (account-model memo §2.2 / §4.1), just without
 *         needing network access. The REAL, already-deployed,
 *         already-in-use account (`0x2c0CF74B2b76110708CA431796367779e3738250`)
 *         is exercised separately in `TotalReclawAccountFork.t.sol`, which
 *         is the only place state-PRESERVATION (as opposed to
 *         state-correctness) can meaningfully be asserted.
 */
contract TotalReclawAccountTest is Test {
    TotalReclawAccount internal implementation;
    TotalReclawAccount internal account;

    address internal owner;
    uint256 internal ownerPriv;
    address internal signer;
    uint256 internal signerPriv;

    /// @dev Must match `TotalReclawAccount.ENTRY_POINT_V07`. Duplicated
    ///      here deliberately -- it is a public, canonical, chain-fixed
    ///      address (not a formula or secret), the same convention the
    ///      pre-existing test file uses for `DATA_EDGE`.
    address internal constant ENTRY_POINT = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    address internal constant DATA_EDGE = 0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca;
    bytes4 internal constant EXECUTE_SEL = 0xb61d27f6;
    bytes4 internal constant EXECUTE_BATCH_SEL = 0x47e1da2a;

    function setUp() public {
        implementation = new TotalReclawAccount();

        (owner, ownerPriv) = makeAddrAndKey("owner");
        (signer, signerPriv) = makeAddrAndKey("signer");

        bytes memory initData = abi.encodeCall(SimpleAccount.initialize, (owner));
        ERC1967Proxy proxy = new ERC1967Proxy(address(implementation), initData);
        account = TotalReclawAccount(payable(address(proxy)));
    }

    // ============================================================
    // Constructor-less / CREATE2 determinism.
    // ============================================================

    /// @dev Pinned-address test, mirroring `DataEdgeStagingCreate2.t.sol
    ///      :69` -- NOT `SessionKeyModule.t.sol:86`'s tautological
    ///      version. `test_create2_address_unchanged_after_module_install`
    ///      recomputes `keccak256(0xff || factory || salt || initCodeHash)`
    ///      under different `vm.chainId` values and asserts the two
    ///      recomputations agree with EACH OTHER -- but `block.chainid` is
    ///      not an input to that expression at all, so it cannot fail
    ///      regardless of bytecode; it is not a real test. This test
    ///      instead pins the predicted address against an INDEPENDENTLY
    ///      hardcoded literal, so an accidental change to the constructor,
    ///      inheritance order, or compiler settings that alters
    ///      `type(TotalReclawAccount).creationCode` surfaces as a failing
    ///      assertion.
    ///
    ///      NOTE: this salt is a placeholder for THIS TEST ONLY, chosen to
    ///      follow the existing `totalreclaw.<ContractName>.v1` naming
    ///      convention (`DeploySessionKeyModule.SESSION_KEY_MODULE_SALT_V1`).
    ///      It does not commit to an actual deployment salt -- that is
    ///      decided by `2026-08-02-phase3-contract-deployment-plan.md`
    ///      (out of scope for P3-1/P3-2/P3-4). If a different salt is
    ///      chosen there, update this pin to match.
    ///
    ///      ALSO NOTE: solc's default metadata hash embeds a hash of the
    ///      exact source text, so this pin is sensitive to ANY edit to
    ///      `TotalReclawAccount.sol` -- including a pure `forge fmt`
    ///      reformat, not just logic changes. That is intentional (it is
    ///      what makes the test catch a real drift), but it means the pin
    ///      legitimately needs regenerating after routine formatting too;
    ///      that is not a sign anything is wrong. Nothing is deployed at
    ///      this address yet, so regenerating it here has zero cost --
    ///      contrast with `DataEdgeStagingCreate2.t.sol`'s pin, which
    ///      tracks a REAL deployed contract and must never be "fixed" by
    ///      just updating the literal.
    bytes32 internal constant TOTAL_RECLAW_ACCOUNT_SALT_V1 =
        keccak256("totalreclaw.TotalReclawAccount.v1");

    function _predictImplAddress(bytes32 salt) internal pure returns (address) {
        bytes32 initCodeHash = keccak256(abi.encodePacked(type(TotalReclawAccount).creationCode));
        return address(
            uint160(
                uint256(
                    keccak256(abi.encodePacked(bytes1(0xff), CREATE2_FACTORY, salt, initCodeHash))
                )
            )
        );
    }

    /// @dev This assertion is only meaningful if `creationCode` is
    ///      REPRODUCIBLE. It is not reproducible by default: foundry's
    ///      default `bytecode_hash = "ipfs"` appends a hash of the solc
    ///      metadata JSON to the deployed bytecode, and that JSON varies
    ///      with toolchain details beyond the pinned `solc_version` — so
    ///      the same source compiled on two machines yields two different
    ///      `creationCode` values and therefore two different CREATE2
    ///      addresses. Caught exactly that way: this test passed locally
    ///      and failed in CI with a different predicted address
    ///      (0x7D12D9f0… vs the then-pinned 0x25D4E718…).
    ///
    ///      `foundry.toml` now sets `bytecode_hash = "none"` +
    ///      `cbor_metadata = false`, which strips the trailer and makes
    ///      `creationCode` a pure function of (source, solc version,
    ///      optimizer settings). The address below is the post-fix value.
    ///
    ///      This matters well beyond the test: the deployment plan pins a
    ///      CREATE2 address, and a pin that depends on WHO compiled the
    ///      contract is not a pin at all — nobody could independently
    ///      reproduce or verify the deployed implementation.
    ///
    ///      Trade-off accepted: stripping the metadata hash makes
    ///      block-explorer source verification marginally less automatic
    ///      (no metadata-hash match). Exact-settings verification still
    ///      works, and determinism is worth more here.
    function test_pinned_implementation_create2_address() public pure {
        assertEq(
            _predictImplAddress(TOTAL_RECLAW_ACCOUNT_SALT_V1),
            0x6d17938F29072B3b7E883d410076B0D73676Cf34,
            "TotalReclawAccount predicted implementation address drifted - update the deployment plan's pinned value if intentional"
        );
    }

    function test_create2_address_is_chain_independent() public {
        // Genuinely exercises chain-independence, unlike the banned test:
        // this asserts the SAME salt+bytecode produce the SAME address
        // across chains, which is what CREATE2 guarantees and what a
        // constructor that reads `block.chainid` (this one does not) would
        // break.
        uint256 original = block.chainid;
        vm.chainId(100); // Gnosis
        address onGnosis = _predictImplAddress(TOTAL_RECLAW_ACCOUNT_SALT_V1);
        vm.chainId(1); // Ethereum mainnet, for contrast
        address onMainnet = _predictImplAddress(TOTAL_RECLAW_ACCOUNT_SALT_V1);
        vm.chainId(original);
        assertEq(onGnosis, onMainnet, "CREATE2 address must be chain-independent");
    }

    // ============================================================
    // Storage layout -- slot 0 stays `owner` after real proxy init.
    // ============================================================

    function test_slot_zero_is_owner_after_proxy_init() public view {
        bytes32 raw = vm.load(address(account), bytes32(uint256(0)));
        assertEq(address(uint160(uint256(raw))), owner, "slot 0 must be `owner` after initialize()");
        assertEq(account.owner(), owner);
    }

    // ============================================================
    // Owner path -- must behave like stock SimpleAccount for genuine
    // owner signatures, and must NOT revert (only soft-fail) for
    // signatures that are not owner-shaped, so the session-key branch is
    // reachable. See `TotalReclawAccount._validateSignature`'s NatSpec
    // for the full reasoning.
    // ============================================================

    function test_owner_signed_userop_validates() public {
        bytes32 userOpHash = keccak256("owner-userop");
        bytes memory sig = _signOwnerStyle(ownerPriv, userOpHash);
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 0, "genuine owner signature must validate, exactly like stock SimpleAccount");
    }

    function test_owner_signed_userop_with_wrong_key_falls_through_and_fails() public {
        (, uint256 attackerPriv) = makeAddrAndKey("attacker");
        bytes32 userOpHash = keccak256("wrong-owner-key");
        bytes memory sig = _signOwnerStyle(attackerPriv, userOpHash);
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        // No session key installed either, so this must cleanly fail --
        // not revert.
        assertEq(vd, 1, "wrong-key owner-shaped signature must SIG_VALIDATION_FAILED, not revert");
    }

    /// @dev Regression test for the documented `_validateSignature`
    ///      deviation: a signature that is neither a well-formed 65-byte
    ///      owner-style signature NOR a valid lazy-install blob must NOT
    ///      revert `validateUserOp` -- it must return SIG_VALIDATION_FAILED
    ///      cleanly. A literal port of stock's `ECDSA.recover` (which
    ///      reverts on a malformed non-65-byte input) would fail this
    ///      test by reverting instead of returning 1.
    function test_malformed_signature_does_not_revert() public {
        bytes32 userOpHash = keccak256("garbage-sig");
        bytes memory garbage = hex"deadbeef"; // 4 bytes -- not 65, not a decodable grant blob
        PackedUserOperation memory op =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), garbage);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "malformed signature must SIG_VALIDATION_FAILED, never revert");
    }

    function test_validateUserOp_reverts_when_not_called_by_entrypoint() public {
        bytes32 userOpHash = keccak256("direct-call");
        bytes memory sig = _signOwnerStyle(ownerPriv, userOpHash);
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.expectRevert(bytes("account: not from EntryPoint"));
        account.validateUserOp(op, userOpHash, 0);
    }

    // ============================================================
    // Session-key path -- lazy install, steady state, replay, scope.
    // ============================================================

    function test_first_call_installs_grant_and_emits_event() public {
        bytes4[] memory sels = _twoSelectors();
        TotalReclawAccount.PermissionGrant memory grant = _buildGrant(signer, DATA_EDGE, sels, 1);

        bytes32 userOpHash = keccak256("first-userop");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        bytes memory sig = abi.encode(grant, ecdsaSig);
        PackedUserOperation memory op =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"deadbeef"), sig);

        vm.expectEmit(true, false, false, true, address(account));
        emit TotalReclawAccount.SessionKeyInstalled(signer, 1);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 0, "first install + execute must succeed");

        assertTrue(account.isSessionKeyActive(signer));
        (uint256 n,,, address tgt) = account.getSessionKeyGrant(signer);
        assertEq(n, 1);
        assertEq(tgt, DATA_EDGE);
    }

    function test_steady_state_userop_after_install_succeeds() public {
        _installGrant(1);

        bytes32 hash2 = keccak256("steady-state");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        PackedUserOperation memory op =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"42"), ecdsaSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, hash2, 0);
        assertEq(vd, 0, "steady-state UserOp must succeed without re-sending the grant");
    }

    function test_validate_rejects_unknown_target() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("wrong-target");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        bytes memory cd = _executeCallData(address(0xDEAD), 0, hex"00");
        PackedUserOperation memory op = _makePackedOp(cd, ecdsaSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "out-of-scope target must SIG_VALIDATION_FAILED");
    }

    function test_validate_rejects_unknown_selector() public {
        bytes4[] memory onlyExecute = new bytes4[](1);
        onlyExecute[0] = EXECUTE_SEL;
        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, onlyExecute, 1);
        bytes32 installHash = keccak256("install-1");
        bytes memory installSig = abi.encode(grant, _signWithKey(signerPriv, installHash));
        PackedUserOperation memory installOp =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), installSig);
        vm.prank(ENTRY_POINT);
        assertEq(account.validateUserOp(installOp, installHash, 0), 0);

        bytes32 hash2 = keccak256("batch-attempt");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        bytes memory batchCallData = abi.encodePacked(EXECUTE_BATCH_SEL, hex"00");
        PackedUserOperation memory op = _makePackedOp(batchCallData, ecdsaSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, hash2, 0);
        assertEq(vd, 1, "out-of-scope selector must SIG_VALIDATION_FAILED");
    }

    function test_validate_invalid_signature_returns_failed() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("op-x");
        (, uint256 attackerPriv) = makeAddrAndKey("attacker-session");
        bytes memory badSig = _signWithKey(attackerPriv, userOpHash);
        PackedUserOperation memory op =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), badSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "wrong signer must SIG_VALIDATION_FAILED");
    }

    function test_nonzero_value_rejected() public {
        _installGrant(1);

        bytes32 userOpHash = keccak256("eth-transfer");
        bytes memory ecdsaSig = _signWithKey(signerPriv, userOpHash);
        bytes memory cd = _executeCallData(DATA_EDGE, 1 ether, hex"00");
        PackedUserOperation memory op = _makePackedOp(cd, ecdsaSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "non-zero value must SIG_VALIDATION_FAILED");
    }

    function test_replay_nonce_protection() public {
        _installGrant(1);
        vm.prank(owner);
        account.revokeSessionKey(signer);

        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 1);
        bytes32 hash2 = keccak256("replay-attempt");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, hash2));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, hash2, 0);
        assertEq(vd, 1, "replayed nonce must SIG_VALIDATION_FAILED");

        TotalReclawAccount.PermissionGrant memory grant2 =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 2);
        bytes32 hash3 = keccak256("fresh-nonce");
        bytes memory sig2 = abi.encode(grant2, _signWithKey(signerPriv, hash3));
        PackedUserOperation memory op2 =
            _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig2);

        vm.prank(ENTRY_POINT);
        uint256 vd2 = account.validateUserOp(op2, hash3, 0);
        assertEq(vd2, 0, "fresh nonce must succeed after revoke");
    }

    function test_cross_chain_grant_replay_rejected() public {
        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 1);
        grant.chainId = 999; // not block.chainid
        grant.masterSignature = _signWithKey(ownerPriv, account.grantDigest(grant));

        bytes32 userOpHash = keccak256("cross-chain");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "cross-chain replay must be rejected");
    }

    function test_master_signature_mismatch_rejects() public {
        (, uint256 attackerPriv) = makeAddrAndKey("attacker-master");

        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 1);
        bytes32 digest = account.grantDigest(grant);
        grant.masterSignature = _signWithKey(attackerPriv, digest);

        bytes32 userOpHash = keccak256("forged-master");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "forged master signature must be rejected");
    }

    /// @dev P3-3: a grant whose `verifyingContract` is NOT this account
    ///      (e.g. a stale grant signed against some other address, or a
    ///      would-be module address) must be rejected. This is the
    ///      concrete regression test for the module -> account binding
    ///      change documented in `_isGrantValid`.
    function test_grant_rejects_wrong_verifying_contract() public {
        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 1);
        grant.verifyingContract = address(0xBEEF); // not address(account)
        grant.masterSignature = _signWithKey(ownerPriv, account.grantDigest(grant));

        bytes32 userOpHash = keccak256("wrong-verifying-contract");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "grant not bound to this account must be rejected");
    }

    function test_grant_rejects_wrong_account_binding() public {
        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), 1);
        grant.account = address(0xBEEF); // not address(account)
        grant.masterSignature = _signWithKey(ownerPriv, account.grantDigest(grant));

        bytes32 userOpHash = keccak256("wrong-account-binding");
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 1, "grant bound to a different account must be rejected");
    }

    // ============================================================
    // Revocation -- owner-gated, idempotent, actually blocks reuse.
    // ============================================================

    function test_revoke_idempotent() public {
        vm.prank(owner);
        account.revokeSessionKey(signer); // never installed -- no-op, no revert
        assertFalse(account.isSessionKeyActive(signer));

        _installGrant(1);
        assertTrue(account.isSessionKeyActive(signer));

        vm.expectEmit(true, false, false, false, address(account));
        emit TotalReclawAccount.SessionKeyRevoked(signer);
        vm.prank(owner);
        account.revokeSessionKey(signer);
        assertFalse(account.isSessionKeyActive(signer));

        vm.prank(owner);
        account.revokeSessionKey(signer); // already revoked -- no-op, no revert, no 2nd event
    }

    function test_revoke_reverts_for_non_owner_caller() public {
        _installGrant(1);
        (address stranger,) = makeAddrAndKey("stranger");

        vm.prank(stranger);
        vm.expectRevert(bytes("only owner"));
        account.revokeSessionKey(signer);
    }

    /// @dev Exercises the SPA's actual revoke pattern: an owner-signed
    ///      UserOp whose callData is `execute(address(this), 0,
    ///      abi.encodeCall(revokeSessionKey, (signer)))`, routed through
    ///      `_requireFromEntryPointOrOwner` -> `execute` -> an inner call
    ///      back to `address(this)`, landing on `onlyOwner`'s self-call
    ///      branch (`msg.sender == address(this)`).
    function test_revoke_via_self_call_execute_pattern() public {
        _installGrant(1);
        assertTrue(account.isSessionKeyActive(signer));

        bytes memory revokeCalldata = abi.encodeCall(account.revokeSessionKey, (signer));
        vm.prank(owner);
        account.execute(address(account), 0, revokeCalldata);

        assertFalse(account.isSessionKeyActive(signer));
    }

    function test_is_session_key_active_false_after_revoke() public {
        _installGrant(1);
        assertTrue(account.isSessionKeyActive(signer));
        vm.prank(owner);
        account.revokeSessionKey(signer);
        assertFalse(account.isSessionKeyActive(signer));
    }

    function test_get_session_key_grant_empty_for_unknown_signer() public view {
        (uint256 n, uint256 iAt, bytes4[] memory sels, address tgt) =
            account.getSessionKeyGrant(address(0x1234));
        assertEq(n, 0);
        assertEq(iAt, 0);
        assertEq(sels.length, 0);
        assertEq(tgt, address(0));
    }

    // ============================================================
    // executeBatch scope invariant (cred-6) -- the six tests ported from
    // `SessionKeyModule.t.sol:424-591`, adapted to call `validateUserOp`
    // (pranked as the EntryPoint) against a real deployed proxy instead
    // of a MockSmartAccount trampoline into the standalone module.
    // ============================================================

    function test_malformed_executeBatch_calldata_rejected() public {
        _installGrant(1);

        bytes32 hash2 = keccak256("malformed-batch");
        bytes memory ecdsaSig = _signWithKey(signerPriv, hash2);
        bytes memory batchCallData = abi.encodePacked(EXECUTE_BATCH_SEL, hex"00");
        PackedUserOperation memory op = _makePackedOp(batchCallData, ecdsaSig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, hash2, 0);
        assertEq(vd, 1, "malformed executeBatch payload must SIG_VALIDATION_FAILED");
    }

    function test_batch_rejects_out_of_scope_inner_call() public {
        _installGrant(1);

        address[] memory targets = new address[](3);
        uint256[] memory values = new uint256[](3);
        bytes[] memory datas = new bytes[](3);
        targets[0] = DATA_EDGE;
        targets[1] = DATA_EDGE;
        targets[2] = address(0xBADC0DE); // out-of-scope target
        for (uint256 i = 0; i < 3; i++) {
            values[i] = 0;
            datas[i] = abi.encodePacked(EXECUTE_SEL, uint256(i));
        }

        bytes32 h = keccak256("out-of-scope-inner");
        bytes memory sig = _signWithKey(signerPriv, h);
        bytes memory cd = abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, h, 0);
        assertEq(vd, 1, "single out-of-scope inner must fail the whole batch");
    }

    function test_batch_accepts_all_in_scope_inner_calls() public {
        _installGrant(1);

        bytes memory cd = _inScopeBatchCallData(3);
        bytes32 h = keccak256("all-in-scope-3");
        bytes memory sig = _signWithKey(signerPriv, h);
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, h, 0);
        assertEq(vd, 0, "all-in-scope batch must SIG_VALIDATION_SUCCESS");
    }

    function test_batch_rejects_nonzero_value_inner() public {
        _installGrant(1);

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

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, h, 0);
        assertEq(vd, 1, "any nonzero value in batch must SIG_VALIDATION_FAILED");
    }

    function test_batch_rejects_out_of_scope_inner_selector() public {
        bytes4[] memory onlyBatch = new bytes4[](1);
        onlyBatch[0] = EXECUTE_BATCH_SEL;

        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, onlyBatch, 1);
        bytes32 installH = keccak256("install-batch-only");
        bytes memory installCd = _batchWithSingleInner(EXECUTE_BATCH_SEL);
        bytes memory installSig = abi.encode(grant, _signWithKey(signerPriv, installH));
        PackedUserOperation memory installOp = _makePackedOp(installCd, installSig);
        vm.prank(ENTRY_POINT);
        assertEq(
            account.validateUserOp(installOp, installH, 0),
            0,
            "install must succeed with all-in-scope inner"
        );

        bytes memory cd = _batchWithSingleInner(EXECUTE_SEL);
        bytes32 h = keccak256("inner-sel-bad");
        bytes memory sig = _signWithKey(signerPriv, h);
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, h, 0);
        assertEq(vd, 1, "inner selector not in grant must SIG_VALIDATION_FAILED");
    }

    function test_batch_rejects_empty_batch() public {
        _installGrant(1);

        address[] memory targets = new address[](0);
        uint256[] memory values = new uint256[](0);
        bytes[] memory datas = new bytes[](0);

        bytes32 h = keccak256("empty-batch");
        bytes memory sig = _signWithKey(signerPriv, h);
        bytes memory cd = abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
        PackedUserOperation memory op = _makePackedOp(cd, sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, h, 0);
        assertEq(vd, 1, "empty executeBatch must SIG_VALIDATION_FAILED");
    }

    // ============================================================
    // Helpers.
    // ============================================================

    function _twoSelectors() internal pure returns (bytes4[] memory s) {
        s = new bytes4[](2);
        s[0] = EXECUTE_SEL;
        s[1] = EXECUTE_BATCH_SEL;
    }

    function _buildGrant(
        address _signer,
        address _target,
        bytes4[] memory _selectors,
        uint256 _nonce
    ) internal view returns (TotalReclawAccount.PermissionGrant memory g) {
        g.version = 1;
        g.account = address(account);
        g.signer = _signer;
        g.target = _target;
        g.selectors = _selectors;
        g.valueMax = 0;
        g.nonce = _nonce;
        g.issuedAt = block.timestamp;
        g.chainId = block.chainid;
        g.verifyingContract = address(account); // P3-3: bound to the ACCOUNT
        g.masterSignature = _signWithKey(ownerPriv, account.grantDigest(g));
    }

    function _installGrant(uint256 nonce) internal {
        TotalReclawAccount.PermissionGrant memory grant =
            _buildGrant(signer, DATA_EDGE, _twoSelectors(), nonce);
        bytes32 userOpHash = keccak256(abi.encode("install-helper", nonce));
        bytes memory sig = abi.encode(grant, _signWithKey(signerPriv, userOpHash));
        PackedUserOperation memory op = _makePackedOp(_executeCallData(DATA_EDGE, 0, hex"00"), sig);

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(op, userOpHash, 0);
        assertEq(vd, 0, "_installGrant helper: install must succeed");
    }

    /// @dev Owner-path signature: EIP-191-prefixed, matching stock
    ///      SimpleAccount / `sign_user_op_hash` (`userop.py`).
    function _signOwnerStyle(uint256 privKey, bytes32 userOpHash)
        internal
        pure
        returns (bytes memory)
    {
        bytes32 ethSigned =
            keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", userOpHash));
        return _signWithKey(privKey, ethSigned);
    }

    /// @dev Session-key path signature: raw digest, NO EIP-191 prefix --
    ///      see `_validateSessionKeySignature`'s NatSpec for why this must
    ///      match `sign_userop_with_session_key` (`userop.py`, P3-8).
    function _signWithKey(uint256 privKey, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _executeCallData(address tgt, uint256 value, bytes memory data)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(EXECUTE_SEL, abi.encode(tgt, value, data));
    }

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

    function _batchWithSingleInner(bytes4 innerSel) internal pure returns (bytes memory) {
        address[] memory targets = new address[](1);
        uint256[] memory values = new uint256[](1);
        bytes[] memory datas = new bytes[](1);
        targets[0] = DATA_EDGE;
        values[0] = 0;
        datas[0] = abi.encodePacked(innerSel, uint256(0));
        return abi.encodePacked(EXECUTE_BATCH_SEL, abi.encode(targets, values, datas));
    }

    function _makePackedOp(bytes memory callData, bytes memory sig)
        internal
        view
        returns (PackedUserOperation memory op)
    {
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
