// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

/* solhint-disable avoid-low-level-calls */
/* solhint-disable no-inline-assembly */

import {SimpleAccount} from "account-abstraction/contracts/samples/SimpleAccount.sol";
import {IEntryPoint} from "account-abstraction/contracts/interfaces/IEntryPoint.sol";
import {
    PackedUserOperation
} from "account-abstraction/contracts/interfaces/PackedUserOperation.sol";
import {
    SIG_VALIDATION_FAILED,
    SIG_VALIDATION_SUCCESS
} from "account-abstraction/contracts/core/Helpers.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

/**
 * @title TotalReclawAccount
 * @notice Option E Phase 3 (P3-1/P3-2) — a `SimpleAccount` (eth-infinitism
 *         v0.7.0) subclass that accepts either an owner-EOA signature
 *         (unchanged stock behaviour) OR a scoped, revocable session-key
 *         signature. This REPLACES the standalone-module design
 *         (`SessionKeyModule.sol`, never deployed) — see
 *         `docs/plans/2026-08-02-option-e-phase3-account-model.md` §5 for
 *         why a standalone module cannot pass ERC-7562 bundler validation
 *         (its grant registry keys on `signer`, not on `msg.sender`, so the
 *         storage it reads during `validateUserOp` is not "associated"
 *         with the account under [STO-020]/[STO-021]). Putting the
 *         registry in the account's OWN storage (this file) is
 *         unconditionally allowed under [STO-010] — "access to the
 *         'account' storage is always allowed" — with no window
 *         arithmetic and no dependence on a contested reading of the
 *         rules.
 *
 * LICENSE NOTE: this file inherits directly from eth-infinitism's
 * `SimpleAccount` (GPL-3.0), so it is licensed GPL-3.0 too (unlike the
 * rest of this package, which is MIT) — matching the convention other
 * ERC-4337 wallets that subclass `SimpleAccount` use (e.g. Alchemy's
 * LightAccount). This is a licensing note, not a security one; flagged
 * for a sanity check alongside the audit, not resolved unilaterally here.
 *
 * THREAT-MODEL CEILING (restate per `docs/specs/cred/phase3-implementation-
 * spec.md` §0 — do not soften in any doc derived from this file): this
 * contract makes a host's SIGNING credential scoped and revocable. It does
 * NOT improve memory confidentiality at a compromised host — the derived
 * encryption key still materialises in the agent's RAM on every recall,
 * and every fact's ciphertext is publicly readable from the Gnosis
 * subgraph regardless of what this contract does.
 *
 * ============================================================
 * WHAT WAS PORTED VERBATIM FROM `SessionKeyModule.sol` (per phase3-impl
 * spec §3.2 — "re-deriving them is pure risk"):
 *   - `_grantDigest` and the four EIP-712 typehash/domain constants
 *     (parity-tested against TS/Python).
 *   - `_isCallDataInScope` / `_isBatchInScope` / `_inSelectors` — the
 *     per-inner-call `executeBatch` validation (cred-6 invariant: EVERY
 *     inner call's target, zero-value and selector are checked
 *     individually; one out-of-scope inner fails the whole batch. No
 *     outer-selector shortcut, no first-inner sampling, no partial
 *     execution).
 *   - `_recoverEcdsa` with the EIP-2 malleability guard (kept as the
 *     original hand-rolled assembly implementation rather than swapped
 *     for `ECDSA.tryRecover`, per the spec's explicit "lift verbatim"
 *     instruction for this specific helper).
 *   - Monotonic `_minNonces` replay protection.
 *   - `_tryDecodeInstallSig` / `_decodeInstallSig` self-external
 *     try/catch decode pattern (malformed lazy-install blob -> false,
 *     never a revert that would poison the whole validateUserOp call).
 *
 * WHAT CHANGED MECHANICALLY (per phase3-impl spec §3.2's table):
 *   - `msg.sender` (module's stand-in for account identity) -> `address(this)`.
 *   - `_grants[account][signer]` / `_minNonces[account][signer]` (module's
 *     doubly-nested, `signer`-first-in-the-hash storage — the very shape
 *     that broke ERC-7562) -> `signer -> Grant` / `signer -> nonce` in
 *     THIS contract's own ERC-7201 namespaced storage.
 *   - `g.account != msg.sender` -> `g.account != address(this)`.
 *   - `g.verifyingContract != address(this)` where `this` was the MODULE
 *     -> `!= address(this)` where `this` is now the ACCOUNT (P3-3 — see
 *     `_isGrantValid` below). Binds a grant to the thing it authorises;
 *     closes the CREATE2-redeploy-invalidates-every-grant hazard the
 *     module design had.
 *   - `revokeSessionKey(account, signer)` gated `msg.sender == account`
 *     -> `revokeSessionKey(signer)` gated by the inherited `onlyOwner`
 *     modifier (accepts the owner EOA directly, or a self-call driven by
 *     an owner-signed UserOp via `execute(address(this), 0,
 *     abi.encodeCall(this.revokeSessionKey, (signer)))`).
 *   - `_smartAccountOwner(account)` (an EXTERNAL call to
 *     `IAccountWithOwner(account).owner()`, needed because the module and
 *     the account were different contracts) -> a direct read of the
 *     inherited `owner` state variable. Strictly better: no external
 *     call, no CALL-opcode restriction to reason about during
 *     `validateUserOp`, and `owner` is slot 0 -- always ERC-7562
 *     [STO-010]-permitted.
 *
 * WHAT WAS DELIBERATELY NOT CARRIED FORWARD (phase3-impl spec §3.2
 * "interface cleanup" instruction): `ISessionKeyModule.sol` declares four
 * errors, three of which (`SessionKeyScopeMismatch`,
 * `SessionKeyInvalidSignature`, `SessionKeyReplay`) are never reverted --
 * the validator fails closed via SIG_VALIDATION_FAILED, which is the
 * correct ERC-4337 pattern. `SessionKeyModule.sol` itself ALSO declares
 * two more unused errors (`UnknownGrantVersion`, `InvalidSignatureLength`)
 * that are never reverted either -- the same defect, one level deeper,
 * that the spec's line-numbered callout didn't name but the same
 * principle applies to. This contract declares ZERO custom errors for
 * the session-key path: every validation failure is a clean
 * SIG_VALIDATION_FAILED return, and the only revert-shaped auth check
 * (`revokeSessionKey`) reuses the inherited `onlyOwner` modifier's
 * existing "only owner" require string rather than inventing a new error
 * type. The module's `ISessionKeyModule.sol:73` docstring also claimed
 * `revokeSessionKey`'s caller "MUST be the master wallet of `account`",
 * which contradicted its own `msg.sender == account` gate (only the
 * SMART ACCOUNT itself could call it, not the master wallet directly) --
 * corrected here: `onlyOwner` genuinely accepts the owner EOA directly OR
 * a self-call, and this doc says so plainly.
 *
 * `SessionKeyModule.sol` and `ISessionKeyModule.sol` are left untouched
 * on disk -- they are dead code (never deployed, per the account-model
 * memo §6) but still compile and their own test suite
 * (`test/SessionKeyModule.t.sol`) still exercises them; deleting them is
 * out of scope for this change.
 * ============================================================
 */
contract TotalReclawAccount is SimpleAccount {
    // -------------------------------------------------------------------
    // Constructor-less: the v0.7 canonical EntryPoint is a hardcoded
    // constant (same address on every chain that has it deployed, Gnosis
    // included -- verified live against the account-model memo §2.3) so
    // this contract's CREATE2 init code is bare `type(TotalReclawAccount)
    // .creationCode` with no constructor-argument encoding required.
    // -------------------------------------------------------------------

    address internal constant ENTRY_POINT_V07 = 0x0000000071727De22E5E9d8BAf0edAc6f37da032;

    constructor() SimpleAccount(IEntryPoint(ENTRY_POINT_V07)) {
        // SimpleAccount's constructor calls `_disableInitializers()` --
        // this implementation instance can never itself be `initialize`d,
        // exactly like the stock implementation it replaces. Proxies that
        // upgrade to this implementation keep whatever `_initialized`
        // state their `Initializable` storage already had (that storage
        // is untouched by this upgrade -- same base contract, same
        // ERC-7201 namespace), so an upgraded proxy cannot be
        // re-initialized to hijack ownership.
    }

    // -------------------------------------------------------------------
    // Grant wire-format constants (byte-for-byte identical to
    // `SessionKeyModule.sol` -- this is a WIRE FORMAT, not an
    // implementation detail; changing it breaks every existing signed
    // grant and the cross-language parity fixture).
    // -------------------------------------------------------------------

    uint8 internal constant GRANT_VERSION = 1;

    /// @dev SimpleAccount.execute(address,uint256,bytes) selector.
    bytes4 internal constant EXECUTE_SELECTOR = 0xb61d27f6;
    /// @dev SimpleAccount.executeBatch(address[],uint256[],bytes[]) selector.
    bytes4 internal constant EXECUTE_BATCH_SELECTOR = 0x47e1da2a;

    bytes32 internal constant DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 internal constant SCOPE_TYPEHASH =
        keccak256("Scope(address target,bytes4[] selectors,uint256 valueMax)");
    bytes32 internal constant GRANT_TYPEHASH = keccak256(
        "SessionKeyPermissionGrant(address account,address signer,Scope scope,uint256 nonce,uint256 issuedAt)Scope(address target,bytes4[] selectors,uint256 valueMax)"
    );
    bytes32 internal constant DOMAIN_NAME_HASH = keccak256(bytes("TotalReclawSessionKey"));
    bytes32 internal constant DOMAIN_VERSION_HASH = keccak256(bytes("1"));

    // -------------------------------------------------------------------
    // ERC-7201 namespaced storage. `owner` (inherited from SimpleAccount)
    // occupies slot 0 -- verified live (account-model memo §2.5) and
    // re-asserted in the fork test after a real upgrade, not by
    // inspection. This contract declares NO plain state variables of its
    // own; all new state lives at a namespaced slot computed from a
    // domain-separated hash, per ERC-7201, so it cannot collide with
    // slot 0 (or with any future OZ base-contract layout change).
    // -------------------------------------------------------------------

    struct GrantStorage {
        uint256 nonce; // grant nonce; 0 = uninstalled
        uint256 issuedAt; // informational -- never compared to block.timestamp
        address target; // scope.target (DataEdge address)
        uint256 valueMax; // always 0 per spec
        bytes4[] selectors; // scope.selectors (execute, executeBatch)
    }

    /// @custom:storage-location erc7201:totalreclaw.account.sessionkeys
    struct SessionKeyStorage {
        /// @dev `grants[signer]` -- active grant for this session key.
        ///      `nonce == 0` means "not installed".
        mapping(address signer => GrantStorage) grants;
        /// @dev `minNonces[signer]` -- smallest acceptable install nonce.
        ///      Starts at 1, bumps on revoke to `revokedNonce + 1`.
        ///      Prevents replay of a revoked grant.
        mapping(address signer => uint256) minNonces;
    }

    // keccak256(abi.encode(uint256(keccak256("totalreclaw.account.sessionkeys")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant SESSION_KEY_STORAGE_LOCATION =
        0x93ec0d14d62ae42f5c43c594ab5fc270681dab686d50adb6a516393064aef500;

    function _sessionKeyStorage() private pure returns (SessionKeyStorage storage $) {
        // solhint-disable-next-line no-inline-assembly
        assembly {
            $.slot := SESSION_KEY_STORAGE_LOCATION
        }
    }

    // -------------------------------------------------------------------
    // ABI-decoded calldata layout for the lazy-install UserOp. Field order
    // and types are byte-for-byte identical to `SessionKeyModule
    // .PermissionGrant` -- this is the cross-language wire format
    // (`derived-bundle-v1.md` §4.1's `signing.grant`), not free to change.
    // `userOp.signature = abi.encode(PermissionGrant, ecdsaSig)` where
    // `ecdsaSig` is a 65-byte ECDSA signed by the session key over the
    // RAW userOpHash (see `_validateSessionKeySignature` for why this is
    // NOT the EIP-191-prefixed hash the owner path uses).
    // -------------------------------------------------------------------

    struct PermissionGrant {
        uint8 version;
        address account;
        address signer;
        address target;
        bytes4[] selectors;
        uint256 valueMax;
        uint256 nonce;
        uint256 issuedAt;
        uint256 chainId;
        address verifyingContract;
        bytes masterSignature; // 65-byte ECDSA from master wallet over the EIP-712 digest
    }

    // -------------------------------------------------------------------
    // Events. See the "WHAT WAS DELIBERATELY NOT CARRIED FORWARD" note
    // above for why there are no custom errors here.
    // -------------------------------------------------------------------

    /// @notice Emitted on the first UserOp that uses a fresh session key
    ///         (lazy-install pattern -- no separate `installSessionKey()`
    ///         call). `signer` is indexed (not `account`, unlike the
    ///         module's event) -- the emitter's address already IS the
    ///         account, so indexing it again is redundant.
    event SessionKeyInstalled(address indexed signer, uint256 nonce);

    /// @notice Emitted when the owner (EOA or self-call) revokes a session
    ///         key. Idempotent -- revoking an unknown or already-revoked
    ///         signer is a no-op and does NOT re-emit this event.
    event SessionKeyRevoked(address indexed signer);

    // -------------------------------------------------------------------
    // _validateSignature -- the one genuinely dangerous function in this
    // contract (audit-risk memo §3 item 2). Owner-first ordering is
    // load-bearing: see the NatSpec on `_validateSignature` below.
    // -------------------------------------------------------------------

    /// @inheritdoc SimpleAccount
    ///
    /// @dev DEVIATION FROM A LITERAL PORT, FLAGGED EXPLICITLY: stock
    ///      `SimpleAccount._validateSignature` calls `ECDSA.recover`
    ///      (not `tryRecover`), which REVERTS the whole `validateUserOp`
    ///      call for any signature that is not a well-formed 65-byte
    ///      (r,s,v) blob. A session-key lazy-install signature is a much
    ///      longer `abi.encode(PermissionGrant, ecdsaSig)` blob -- a
    ///      literal "try owner's exact code path first" port would
    ///      REVERT on every lazy-install UserOp before ever reaching the
    ///      session-key branch, making the entire feature non-functional
    ///      (not merely insecure -- inert). `ECDSA.tryRecover` is used
    ///      instead, and any non-`NoError` outcome is treated as "not the
    ///      owner, keep going" rather than reverting.
    ///
    ///      Why this preserves "byte-identical to stock for the owner
    ///      path": for a genuine 65-byte signature, `tryRecover`'s
    ///      success/failure and recovered address are IDENTICAL to
    ///      `recover`'s -- same `ecrecover` call, same EIP-2 malleability
    ///      guard (`RecoverError.InvalidSignatureS`), same zero-address
    ///      guard (`RecoverError.InvalidSignature`). The only observable
    ///      difference is the FAILURE MODE for a malformed 65-byte owner
    ///      attempt: stock reverts the whole `validateUserOp`; this
    ///      contract instead falls through to the session-key branch
    ///      and, if that also fails, returns SIG_VALIDATION_FAILED
    ///      cleanly. No input that stock would have ACCEPTED is rejected
    ///      here, and no input stock would have REJECTED is accepted
    ///      here -- only the shape of "rejected" changes for inputs that
    ///      were never going to succeed anyway.
    ///
    ///      One case worth naming precisely: could a genuine session-key
    ///      signature accidentally trip a revert in stock's semantics if
    ///      we HAD used `ECDSA.recover` here? `ecrecover`'s "returns
    ///      address(0)" failure mode depends only on `(v, r, s)`, never
    ///      on the hash being checked against -- a real signature
    ///      produced by a real private key over ANY hash has a `r` that
    ///      decompresses to a valid curve point, so `ecrecover` will not
    ///      spuriously return zero when checked against the WRONG
    ///      (owner-path) hash either. The malleability guard is likewise
    ///      a pure function of `s`, independent of which hash is being
    ///      checked. So `ECDSA.recover` would not have spuriously
    ///      reverted on a well-formed session-key signature -- but
    ///      `tryRecover` is still the correct choice, because it is the
    ///      only one of the two that does not revert on the
    ///      DIFFERENT-LENGTH lazy-install blob, and using it uniformly
    ///      (rather than length-branching before deciding which ECDSA
    ///      entry point to call) keeps this function's control flow
    ///      simple and auditable.
    function _validateSignature(PackedUserOperation calldata userOp, bytes32 userOpHash)
        internal
        override
        returns (uint256 validationData)
    {
        // 1. Owner path -- tried first, and for any input stock SimpleAccount
        //    would have accepted or rejected, this produces the identical
        //    verdict. See the deviation note above for the one behavioural
        //    difference (soft-fail vs revert on a malformed non-owner-shaped
        //    signature) and why it is required, not incidental.
        bytes32 ethSignedHash = MessageHashUtils.toEthSignedMessageHash(userOpHash);
        (address recoveredOwner, ECDSA.RecoverError ownerErr,) =
            ECDSA.tryRecover(ethSignedHash, userOp.signature);
        if (ownerErr == ECDSA.RecoverError.NoError && recoveredOwner == owner) {
            return SIG_VALIDATION_SUCCESS;
        }

        // 2. Session-key path -- only reached when the owner path did not
        //    match. An account that has been upgraded but never paired
        //    with a session key has an empty session-key storage
        //    namespace, so every UserOp falls through to here and is
        //    rejected exactly as it would have been rejected by stock
        //    SimpleAccount (no owner match -> SIG_VALIDATION_FAILED).
        return _validateSessionKeySignature(userOp, userOpHash);
    }

    /// @dev Steady-state (65-byte) and lazy-install (abi-encoded grant +
    ///      65-byte session signature) paths, ported from
    ///      `SessionKeyModule.validateSessionKeyUserOp` with the
    ///      mechanical changes documented in the contract-level NatSpec.
    function _validateSessionKeySignature(PackedUserOperation calldata userOp, bytes32 userOpHash)
        private
        returns (uint256)
    {
        bytes calldata sig = userOp.signature;
        SessionKeyStorage storage $ = _sessionKeyStorage();

        // Steady-state path: 65-byte raw ECDSA over the RAW userOpHash --
        // deliberately NOT EIP-191-prefixed, unlike the owner path above.
        // This must match whatever the session-key signer actually
        // produces client-side (python/src/totalreclaw/userop.py
        // `sign_userop_with_session_key`, P3-8) -- a prefix mismatch here
        // would produce a signature that "looks valid" but recovers to
        // the wrong address, silently rejecting every legitimate
        // steady-state UserOp. Pinned in the fork test.
        if (sig.length == 65) {
            address signer = _recoverEcdsa(userOpHash, sig);
            if (signer == address(0)) return SIG_VALIDATION_FAILED;

            GrantStorage storage g = $.grants[signer];
            if (g.nonce == 0) return SIG_VALIDATION_FAILED;

            if (!_isCallDataInScope(userOp.callData, g.target, g.selectors)) {
                return SIG_VALIDATION_FAILED;
            }
            return SIG_VALIDATION_SUCCESS;
        }

        // Lazy-install path: signature is abi.encode(PermissionGrant, ecdsaSig).
        (bool ok, PermissionGrant memory grant, bytes memory ecdsaSig) = _tryDecodeInstallSig(sig);
        if (!ok) return SIG_VALIDATION_FAILED;

        // Verify the session signer first -- cheap, fails fast on tampered
        // signatures before touching the (more expensive) grant checks.
        address installSigner = _recoverEcdsa(userOpHash, ecdsaSig);
        if (installSigner == address(0) || installSigner != grant.signer) {
            return SIG_VALIDATION_FAILED;
        }

        if (!_isGrantValid(grant)) return SIG_VALIDATION_FAILED;

        // Replay protection -- nonce must be at-or-above the minimum.
        uint256 minN = $.minNonces[grant.signer];
        if (minN == 0) minN = 1; // first install
        if (grant.nonce < minN) return SIG_VALIDATION_FAILED;

        // Recover the master wallet from the EIP-712 grant signature and
        // verify it is THIS account's owner. Unlike the standalone-module
        // design (which needed an external call to
        // `IAccountWithOwner(account).owner()` because the module and the
        // account were different contracts), `owner` is a direct
        // inherited storage read here -- always ERC-7562 [STO-010]
        // permitted, no external CALL during validateUserOp.
        bytes32 digest = _grantDigest(grant);
        address master = _recoverEcdsa(digest, grant.masterSignature);
        if (master == address(0)) return SIG_VALIDATION_FAILED;
        if (master != owner) return SIG_VALIDATION_FAILED;

        // All checks pass -- install the grant.
        $.grants[grant.signer] = GrantStorage({
            nonce: grant.nonce,
            issuedAt: grant.issuedAt,
            target: grant.target,
            valueMax: grant.valueMax,
            selectors: grant.selectors
        });

        emit SessionKeyInstalled(grant.signer, grant.nonce);

        // After install, validate the actual userOp callData against the
        // newly-stored scope -- same path the steady-state branch takes.
        if (!_isCallDataInScope(userOp.callData, grant.target, grant.selectors)) {
            return SIG_VALIDATION_FAILED;
        }
        return SIG_VALIDATION_SUCCESS;
    }

    // -------------------------------------------------------------------
    // Mutating -- owner-gated session-key management.
    // -------------------------------------------------------------------

    /// @notice Revoke a session key. Gated by the inherited `onlyOwner`
    ///         modifier, which accepts EITHER the owner EOA calling
    ///         directly, OR a self-call (i.e. an owner-signed UserOp
    ///         whose callData is `execute(address(this), 0,
    ///         abi.encodeCall(this.revokeSessionKey, (signer)))`) -- the
    ///         pattern the SPA's "revoke" button uses. Idempotent:
    ///         revoking an unknown or already-revoked signer is a silent
    ///         no-op (no revert, no duplicate event).
    function revokeSessionKey(address signer) external onlyOwner {
        SessionKeyStorage storage $ = _sessionKeyStorage();
        GrantStorage storage g = $.grants[signer];
        if (g.nonce == 0) {
            // Already revoked or never installed -- idempotent no-op.
            return;
        }

        // Bump the min-nonce so this grant cannot be re-played even with
        // the original signed payload.
        $.minNonces[signer] = g.nonce + 1;
        delete $.grants[signer];

        emit SessionKeyRevoked(signer);
    }

    // -------------------------------------------------------------------
    // Views.
    // -------------------------------------------------------------------

    function isSessionKeyActive(address signer) external view returns (bool) {
        return _sessionKeyStorage().grants[signer].nonce != 0;
    }

    function getSessionKeyGrant(address signer)
        external
        view
        returns (uint256 nonce, uint256 issuedAt, bytes4[] memory selectors, address target)
    {
        GrantStorage storage g = _sessionKeyStorage().grants[signer];
        return (g.nonce, g.issuedAt, g.selectors, g.target);
    }

    /// @notice External read-only wrapper around `_grantDigest`, so tests
    ///         and off-chain tooling (SPA grant signing, future core
    ///         PyO3/WASM bindings) compute the EIP-712 digest via the
    ///         actual production code path -- never a hand-copied mirror
    ///         of it. (`SessionKeyModule.t.sol`'s `_grantDigestExt` was
    ///         exactly such a mirror, with a comment admitting both copies
    ///         had to be updated together; this wrapper is the fix.)
    function grantDigest(PermissionGrant calldata g) external pure returns (bytes32) {
        return _grantDigest(g);
    }

    // -------------------------------------------------------------------
    // Internal helpers -- ported verbatim from `SessionKeyModule.sol`
    // except where the contract-level NatSpec documents a mechanical
    // change.
    // -------------------------------------------------------------------

    /// @dev `try`/`catch` wrapper around `abi.decode` so a malformed
    ///      install signature returns `(false, ...)` instead of
    ///      reverting the whole `validateUserOp` callback.
    function _tryDecodeInstallSig(bytes calldata sig)
        internal
        view
        returns (bool ok, PermissionGrant memory grant, bytes memory ecdsaSig)
    {
        if (sig.length < 0x40) return (false, grant, ecdsaSig);

        try this._decodeInstallSig(sig) returns (PermissionGrant memory g, bytes memory s) {
            return (true, g, s);
        } catch {
            return (false, grant, ecdsaSig);
        }
    }

    /// @dev Public-but-only-self entry for the try/catch above. External
    ///      so the catch sees revert data cleanly.
    function _decodeInstallSig(bytes calldata sig)
        external
        pure
        returns (PermissionGrant memory grant, bytes memory ecdsaSig)
    {
        (grant, ecdsaSig) = abi.decode(sig, (PermissionGrant, bytes));
    }

    /// @dev Static grant-shape validation. Does NOT check the signature --
    ///      that lives in the caller, which recovers the master wallet
    ///      only once.
    ///
    ///      `g.verifyingContract != address(this)` is the P3-3 binding:
    ///      in the module design `this` was the MODULE; here it is the
    ///      ACCOUNT itself. This is a value change in what gets signed
    ///      off-chain, not a schema change -- same field, same type.
    function _isGrantValid(PermissionGrant memory g) internal view returns (bool) {
        if (g.version != GRANT_VERSION) return false;
        if (g.account != address(this)) return false; // grant binds to THIS account
        if (g.chainId != block.chainid) return false; // cross-chain replay guard
        if (g.verifyingContract != address(this)) return false; // P3-3: account-binding
        if (g.valueMax != 0) return false; // session keys cannot move ETH
        if (g.target == address(0)) return false;
        if (g.signer == address(0)) return false;
        if (g.selectors.length == 0) return false;
        if (g.masterSignature.length != 65) return false;
        return true;
    }

    /// @dev EIP-712 digest. Ported verbatim from
    ///      `SessionKeyModule._grantDigest` -- pure math over the grant's
    ///      OWN fields (including `g.chainId` / `g.verifyingContract` as
    ///      given, never re-derived from `block.chainid` /
    ///      `address(this)`), because the digest must match whatever was
    ///      actually signed off-chain before we know if it will validate.
    function _grantDigest(PermissionGrant memory g) internal pure returns (bytes32) {
        bytes32 scopeHash = keccak256(
            abi.encode(
                SCOPE_TYPEHASH, g.target, keccak256(abi.encodePacked(g.selectors)), g.valueMax
            )
        );
        bytes32 structHash = keccak256(
            abi.encode(GRANT_TYPEHASH, g.account, g.signer, scopeHash, g.nonce, g.issuedAt)
        );
        bytes32 domainSep = keccak256(
            abi.encode(
                DOMAIN_TYPEHASH,
                DOMAIN_NAME_HASH,
                DOMAIN_VERSION_HASH,
                g.chainId,
                g.verifyingContract
            )
        );
        return keccak256(abi.encodePacked(hex"1901", domainSep, structHash));
    }

    /// @dev Scope validation for both `execute` and `executeBatch` outer
    ///      calls. Ported verbatim from `SessionKeyModule
    ///      ._isCallDataInScope` -- no reference to `msg.sender` in the
    ///      original, so no mechanical change needed here either.
    function _isCallDataInScope(
        bytes calldata callData,
        address grantTarget,
        bytes4[] memory allowedSelectors
    ) internal view returns (bool) {
        if (callData.length < 4) return false;
        bytes4 outerSel = bytes4(callData[:4]);

        // Selector must be in the SA-level allowlist.
        if (!_inSelectors(outerSel, allowedSelectors)) return false;

        if (outerSel == EXECUTE_SELECTOR) {
            // execute(address target, uint256 value, bytes data)
            if (callData.length < 4 + 32 * 3) return false;
            (address innerTarget, uint256 value,) =
                abi.decode(callData[4:], (address, uint256, bytes));
            return innerTarget == grantTarget && value == 0;
        }

        if (outerSel == EXECUTE_BATCH_SELECTOR) {
            // executeBatch(address[] targets, uint256[] values, bytes[] datas).
            // Per the cred-6 invariant: every inner call is individually
            // validated. First non-match -> false; no partial-execution
            // semantics, no outer-selector-passes shortcut.
            try this._decodeExecuteBatch(callData[4:]) returns (
                address[] memory targets, uint256[] memory values, bytes[] memory datas
            ) {
                return _isBatchInScope(targets, values, datas, grantTarget, allowedSelectors);
            } catch {
                return false;
            }
        }

        return false;
    }

    /// @dev Public-but-only-self entry for the try/catch decode of the
    ///      executeBatch payload. Ported verbatim.
    function _decodeExecuteBatch(bytes calldata payload)
        external
        pure
        returns (address[] memory targets, uint256[] memory values, bytes[] memory datas)
    {
        (targets, values, datas) = abi.decode(payload, (address[], uint256[], bytes[]));
    }

    /// @dev Per-inner-call scope assertion. Ported verbatim from
    ///      `SessionKeyModule._isBatchInScope` -- the load-bearing cred-6
    ///      invariant: returns true ONLY when EVERY inner call's target,
    ///      zero-value, and selector all pass.
    function _isBatchInScope(
        address[] memory targets,
        uint256[] memory values,
        bytes[] memory datas,
        address grantTarget,
        bytes4[] memory allowedSelectors
    ) internal pure returns (bool) {
        uint256 n = targets.length;
        if (n == 0) return false;
        if (values.length != n || datas.length != n) return false;

        for (uint256 i = 0; i < n; i++) {
            if (targets[i] != grantTarget) return false;
            if (values[i] != 0) return false;

            bytes memory inner = datas[i];
            if (inner.length < 4) return false;

            // First 4 bytes of `inner` -- in-memory layout is
            // (length: 32 bytes)(data...), so mload at offset 0x20 yields
            // a word whose top 4 bytes are the selector.
            bytes4 innerSel;
            // solhint-disable-next-line no-inline-assembly
            assembly {
                innerSel := mload(add(inner, 0x20))
            }
            if (!_inSelectors(innerSel, allowedSelectors)) return false;
        }
        return true;
    }

    function _inSelectors(bytes4 needle, bytes4[] memory hay) internal pure returns (bool) {
        for (uint256 i = 0; i < hay.length; i++) {
            if (hay[i] == needle) return true;
        }
        return false;
    }

    /// @dev Minimal ECDSA recover (r, s, v). Ported verbatim from
    ///      `SessionKeyModule._recoverEcdsa` rather than swapped for
    ///      `ECDSA.tryRecover` -- functionally equivalent (same EIP-2
    ///      malleability guard), but the spec calls this out by name as
    ///      one of the best-tested pieces of the original file, so it is
    ///      lifted as-is rather than re-derived. Returns address(0) on
    ///      malformed input or `s` in the upper half of the curve order.
    function _recoverEcdsa(bytes32 digest, bytes memory sig) internal pure returns (address) {
        if (sig.length != 65) return address(0);

        bytes32 r;
        bytes32 s;
        uint8 v;
        // solhint-disable-next-line no-inline-assembly
        assembly {
            r := mload(add(sig, 0x20))
            s := mload(add(sig, 0x40))
            v := byte(0, mload(add(sig, 0x60)))
        }
        // EIP-2 malleability guard
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return address(0);
        }
        if (v != 27 && v != 28) return address(0);
        return ecrecover(digest, v, r, s);
    }
}
