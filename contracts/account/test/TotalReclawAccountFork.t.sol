// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {
    PackedUserOperation
} from "account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {IEntryPoint} from "account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {SimpleAccount} from "account-abstraction/contracts/samples/SimpleAccount.sol";
import {SimpleAccountFactory} from "account-abstraction/contracts/samples/SimpleAccountFactory.sol";
import {TotalReclawAccount} from "../contracts/TotalReclawAccount.sol";

/**
 * @title TotalReclawAccountForkTest
 * @notice Option E Phase 3 (P3-4) — "the highest-value test in the phase"
 *         (phase3-impl spec §3.4). Forks live Gnosis mainnet, upgrades the
 *         REAL, already-deployed, already-in-use Smart Account
 *         (`0x2c0CF74B2b76110708CA431796367779e3738250`, 66 UserOps of
 *         history as of the block this was last verified against — see
 *         the PR description for the live `cast` output), and asserts the
 *         upgrade is exactly as behaviour-preserving as the account-model
 *         memo's on-chain simulation (§2.4) predicted.
 *
 *         READ-ONLY. This forks a public RPC and never broadcasts a
 *         transaction — every state change here (`vm.prank` + a call) is
 *         local to the forked EVM instance Foundry spins up in-process and
 *         is discarded when the test ends.
 *
 *         Network dependency: needs an RPC endpoint. Defaults to the
 *         public `https://rpc.gnosischain.com` (same one used throughout
 *         this investigation and in `hardhat.config.ts`'s default);
 *         override with `GNOSIS_RPC_URL` if you have a private endpoint
 *         and want less exposure to public-RPC flakiness/rate limits.
 */
contract TotalReclawAccountForkTest is Test {
    // Live Gnosis mainnet addresses. Verified live 2026-08-17 (owner(),
    // ERC-1967 implementation slot, EntryPoint nonce, and the factory's
    // accountImplementation() all re-checked via `cast call`/`cast
    // storage` against https://rpc.gnosischain.com immediately before
    // writing this file — see the PR description for the exact commands
    // and output). Re-verify before contract freeze; these are exactly
    // the kind of addresses the account-model memo (§2) warns can need
    // re-confirming as ecosystem state moves.
    address internal constant REAL_ACCOUNT = 0x2c0CF74B2b76110708CA431796367779e3738250;
    address internal constant REAL_OWNER = 0x9858EfFD232B4033E47d90003D41EC34EcaEda94;
    address internal constant FACTORY = 0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985;
    address internal constant ENTRY_POINT = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;
    /// @dev Staging DataEdge (`0xE7a4D2…`) — used as an arbitrary-but-
    ///      realistic grant target in the signature-path test below. Not
    ///      actually written to (validateUserOp only, no execute).
    address internal constant DATA_EDGE_STAGING = 0xE7a4D2677B686e13775Ba9092631089e35F0BB91;

    /// @dev ERC-1967 implementation slot:
    ///      bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1).
    bytes32 internal constant ERC1967_IMPL_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;

    bytes4 internal constant EXECUTE_SEL = 0xb61d27f6;
    bytes4 internal constant EXECUTE_BATCH_SEL = 0x47e1da2a;

    function _fork() internal {
        string memory rpc = vm.envOr("GNOSIS_RPC_URL", string("https://rpc.gnosischain.com"));
        vm.createSelectFork(rpc);
    }

    // ============================================================
    // Test 1 — the real, already-in-use account. State preservation only
    // (no signing needed: we do not have REAL_OWNER's private key, and we
    // should not need it — vm.prank spoofs msg.sender, which is all
    // `_onlyOwner()` and `_requireFromEntryPointOrOwner()` check).
    // ============================================================

    function test_fork_upgrade_preserves_real_account_state() public {
        _fork();

        SimpleAccount realAccount = SimpleAccount(payable(REAL_ACCOUNT));

        address ownerBefore = realAccount.owner();
        bytes32 slot0Before = vm.load(REAL_ACCOUNT, bytes32(uint256(0)));
        uint256 nonceBefore = IEntryPoint(ENTRY_POINT).getNonce(REAL_ACCOUNT, 0);

        assertEq(ownerBefore, REAL_OWNER, "sanity: live owner must match the last-verified value");

        TotalReclawAccount newImpl = new TotalReclawAccount();

        // Owner EOA calls upgradeToAndCall DIRECTLY. This is exactly the
        // "escape hatch" path verified live in the account-model memo §2.4
        // (its 2nd `cast call` simulation, `--from` the owner EOA) and
        // written down as the B1 mitigation in the audit-risk memo §5:
        // no bundler, no paymaster, no UserOp signature needed.
        vm.prank(REAL_OWNER);
        realAccount.upgradeToAndCall(address(newImpl), "");

        // ---- the account-model memo's own state-preservation claims, re-asserted post-upgrade ----
        assertEq(realAccount.owner(), ownerBefore, "owner() must be unchanged after upgrade");
        assertEq(
            vm.load(REAL_ACCOUNT, bytes32(uint256(0))),
            slot0Before,
            "raw slot 0 must be byte-identical after upgrade"
        );
        assertEq(
            IEntryPoint(ENTRY_POINT).getNonce(REAL_ACCOUNT, 0),
            nonceBefore,
            "EntryPoint nonce must be unchanged by the upgrade itself (no UserOp was submitted)"
        );
        assertEq(
            address(uint160(uint256(vm.load(REAL_ACCOUNT, ERC1967_IMPL_SLOT)))),
            address(newImpl),
            "proxy must now delegate to TotalReclawAccount (upgrade actually landed)"
        );

        // Functional continuity beyond just reading storage: the owner EOA
        // can still drive execute() directly post-upgrade, exactly as
        // before (0-value, no-op call to itself -- proves the
        // owner-authorization path still works end to end, not just that
        // a view function returns the same value).
        vm.prank(REAL_OWNER);
        realAccount.execute(REAL_OWNER, 0, "");
    }

    function test_fork_upgrade_is_idempotent_on_real_account() public {
        _fork();
        SimpleAccount realAccount = SimpleAccount(payable(REAL_ACCOUNT));
        TotalReclawAccount newImpl = new TotalReclawAccount();

        vm.prank(REAL_OWNER);
        realAccount.upgradeToAndCall(address(newImpl), "");
        assertEq(
            address(uint160(uint256(vm.load(REAL_ACCOUNT, ERC1967_IMPL_SLOT)))), address(newImpl)
        );

        // Running the exact same upgrade a second time must not revert or
        // change anything further (account-model memo §7's rollout plan
        // explicitly calls out upgrade idempotence as a requirement, and
        // §2.4 confirms an OZ v5 UUPS no-op re-upgrade to the SAME address
        // succeeds).
        vm.prank(REAL_OWNER);
        realAccount.upgradeToAndCall(address(newImpl), "");
        assertEq(
            address(uint160(uint256(vm.load(REAL_ACCOUNT, ERC1967_IMPL_SLOT)))),
            address(newImpl),
            "re-running the same upgrade must be harmless"
        );
    }

    // ============================================================
    // Test 2 — a FRESH account, deployed through the REAL, LIVE factory
    // (same factory + same baseline implementation the real account
    // above was originally created with), owned by a Foundry-controlled
    // keypair so the full validateUserOp signature paths can actually be
    // exercised (we do not, and should not, have REAL_OWNER's private
    // key). This is what lets this file assert the three signature-shaped
    // bullets the spec calls for: owner-signed validates, session-key
    // validates, out-of-scope does not.
    // ============================================================

    /// @dev Split into four smaller tests (each calling this + `_installSessionKey`
    ///      helper below) rather than one large function -- the original,
    ///      single-function version tripped solc's "stack too deep" under
    ///      the legacy (non-IR) codegen this package deliberately keeps
    ///      (see `foundry.toml`'s `via_ir = false`, matching the outer
    ///      project). Splitting is also better test hygiene: one failure
    ///      now names exactly which property broke.
    function test_fork_owner_signed_userop_validates_on_factory_account() public {
        (TotalReclawAccount account,, uint256 freshOwnerPriv,,) = _deployFreshUpgradedAccount();

        bytes32 ownerOpHash = keccak256("fork-owner-op");
        bytes memory ownerSig = _signOwnerStyle(freshOwnerPriv, ownerOpHash);
        PackedUserOperation memory ownerOp = _makePackedOp(
            address(account), _executeCallData(DATA_EDGE_STAGING, 0, hex"00"), ownerSig
        );

        vm.prank(ENTRY_POINT);
        assertEq(
            account.validateUserOp(ownerOp, ownerOpHash, 0),
            0,
            "owner-signed UserOp must validate post-upgrade, on the real factory + real baseline impl"
        );
    }

    function test_fork_session_key_validates_on_factory_account() public {
        (
            TotalReclawAccount account,
            address freshOwner,
            uint256 freshOwnerPriv,
            address freshSigner,
            uint256 freshSignerPriv
        ) = _deployFreshUpgradedAccount();
        _installSessionKey(account, freshOwner, freshOwnerPriv, freshSigner, freshSignerPriv, 1);
        assertTrue(account.isSessionKeyActive(freshSigner));

        bytes32 steadyHash = keccak256("fork-steady-state");
        bytes memory steadySig = _signWithKey(freshSignerPriv, steadyHash);
        PackedUserOperation memory steadyOp = _makePackedOp(
            address(account), _executeCallData(DATA_EDGE_STAGING, 0, hex"01"), steadySig
        );

        vm.prank(ENTRY_POINT);
        assertEq(
            account.validateUserOp(steadyOp, steadyHash, 0),
            0,
            "session-key steady-state UserOp must validate"
        );
    }

    function test_fork_out_of_scope_userop_rejected_on_factory_account() public {
        (
            TotalReclawAccount account,
            address freshOwner,
            uint256 freshOwnerPriv,
            address freshSigner,
            uint256 freshSignerPriv
        ) = _deployFreshUpgradedAccount();
        _installSessionKey(account, freshOwner, freshOwnerPriv, freshSigner, freshSignerPriv, 1);

        bytes32 badHash = keccak256("fork-out-of-scope");
        bytes memory badSig = _signWithKey(freshSignerPriv, badHash);
        PackedUserOperation memory badOp = _makePackedOp(
            address(account),
            _executeCallData(address(0xBADC0DE), 0, hex"00"), // wrong target
            badSig
        );

        vm.prank(ENTRY_POINT);
        assertEq(
            account.validateUserOp(badOp, badHash, 0),
            1,
            "out-of-scope target must SIG_VALIDATION_FAILED, even on a real upgraded factory-deployed account"
        );
    }

    /// @dev B4 mitigation, exercised end to end on a real, upgraded,
    ///      factory-deployed account: revoke actually blocks reuse, not
    ///      just flips a view function.
    function test_fork_revocation_blocks_reuse_on_factory_account() public {
        (
            TotalReclawAccount account,
            address freshOwner,
            uint256 freshOwnerPriv,
            address freshSigner,
            uint256 freshSignerPriv
        ) = _deployFreshUpgradedAccount();
        _installSessionKey(account, freshOwner, freshOwnerPriv, freshSigner, freshSignerPriv, 1);

        vm.prank(freshOwner);
        account.revokeSessionKey(freshSigner);
        assertFalse(account.isSessionKeyActive(freshSigner));

        bytes32 revokedHash = keccak256("fork-post-revoke");
        bytes memory revokedSig = _signWithKey(freshSignerPriv, revokedHash);
        PackedUserOperation memory revokedOp = _makePackedOp(
            address(account), _executeCallData(DATA_EDGE_STAGING, 0, hex"00"), revokedSig
        );

        vm.prank(ENTRY_POINT);
        assertEq(
            account.validateUserOp(revokedOp, revokedHash, 0),
            1,
            "a revoked session key must no longer validate"
        );
    }

    // ============================================================
    // Helpers.
    // ============================================================

    /// @dev Forks Gnosis, deploys a fresh account through the REAL, LIVE
    ///      factory (owned by a Foundry-controlled keypair, since we do
    ///      not and should not have REAL_OWNER's private key), and
    ///      upgrades it to a fresh `TotalReclawAccount` implementation --
    ///      the same factory + same baseline implementation the real
    ///      account in test 1 was originally created with.
    function _deployFreshUpgradedAccount()
        internal
        returns (
            TotalReclawAccount account,
            address freshOwner,
            uint256 freshOwnerPriv,
            address freshSigner,
            uint256 freshSignerPriv
        )
    {
        _fork();

        (freshOwner, freshOwnerPriv) = makeAddrAndKey("fork-fresh-owner");
        (freshSigner, freshSignerPriv) = makeAddrAndKey("fork-fresh-signer");

        SimpleAccountFactory factory = SimpleAccountFactory(FACTORY);
        uint256 salt = uint256(keccak256("totalreclaw.fork-test.v1"));
        address predicted = factory.getAddress(freshOwner, salt);
        SimpleAccount fresh = factory.createAccount(freshOwner, salt);
        require(
            address(fresh) == predicted, "helper: factory address must match its own prediction"
        );

        TotalReclawAccount newImpl = new TotalReclawAccount();
        vm.prank(freshOwner);
        SimpleAccount(payable(address(fresh))).upgradeToAndCall(address(newImpl), "");

        account = TotalReclawAccount(payable(address(fresh)));
    }

    /// @dev Signs and validates a lazy-install session-key UserOp so
    ///      subsequent test bodies can start from "a session key is
    ///      installed" without re-deriving the grant-construction
    ///      boilerplate (also what kept the original single-function
    ///      version under the stack-too-deep ceiling).
    function _installSessionKey(
        TotalReclawAccount account,
        address, /* freshOwner */
        uint256 freshOwnerPriv,
        address freshSigner,
        uint256 freshSignerPriv,
        uint256 nonce
    ) internal {
        bytes4[] memory sels = new bytes4[](2);
        sels[0] = EXECUTE_SEL;
        sels[1] = EXECUTE_BATCH_SEL;

        TotalReclawAccount.PermissionGrant memory grant;
        grant.version = 1;
        grant.account = address(account);
        grant.signer = freshSigner;
        grant.target = DATA_EDGE_STAGING;
        grant.selectors = sels;
        grant.valueMax = 0;
        grant.nonce = nonce;
        grant.issuedAt = block.timestamp;
        grant.chainId = block.chainid;
        grant.verifyingContract = address(account); // P3-3: bound to the account
        grant.masterSignature = _signWithKey(freshOwnerPriv, account.grantDigest(grant));

        bytes32 installHash = keccak256(abi.encode("fork-install", nonce));
        bytes memory installSig = abi.encode(grant, _signWithKey(freshSignerPriv, installHash));
        PackedUserOperation memory installOp = _makePackedOp(
            address(account), _executeCallData(DATA_EDGE_STAGING, 0, hex"00"), installSig
        );

        vm.prank(ENTRY_POINT);
        uint256 vd = account.validateUserOp(installOp, installHash, 0);
        require(vd == 0, "helper: session-key lazy-install must succeed");
    }

    function _signOwnerStyle(uint256 privKey, bytes32 userOpHash)
        internal
        pure
        returns (bytes memory)
    {
        bytes32 ethSigned =
            keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", userOpHash));
        return _signWithKey(privKey, ethSigned);
    }

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

    function _makePackedOp(address sender, bytes memory callData, bytes memory sig)
        internal
        pure
        returns (PackedUserOperation memory op)
    {
        op.sender = sender;
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
