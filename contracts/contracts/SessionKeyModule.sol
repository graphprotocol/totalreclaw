// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ISessionKeyModule} from "./interfaces/ISessionKeyModule.sol";
import {PackedUserOperation} from "./interfaces/IUserOperation.sol";

/**
 * @dev Minimal SimpleAccount-style owner interface. Used to verify that the
 *      master wallet (recovered from the EIP-712 grant signature) is the
 *      authoritative owner of the Smart Account during lazy-install.
 *
 *      Note: kernel v3 Smart Accounts do NOT expose a top-level `owner()`
 *      — cred-7 (cross-chain CREATE2 deploy / kernel v3 wiring) wraps this
 *      lookup to support both account models. Stage 2 ships with the
 *      SimpleAccount lookup; this is the same model used by the current
 *      production deployment (see CLAUDE.md "Smart Account" memory).
 */
interface IAccountWithOwner {
    function owner() external view returns (address);
}

/**
 * @title SessionKeyModule
 * @notice cred-5 stage 2 — real validator body. Per
 *         `docs/specs/cred/session-key-delegation.md` §4.2.
 *
 * STAGES SHIPPED IN THIS FILE
 * - validateSessionKeyUserOp (single-call path; executeBatch defers to cred-6)
 * - revokeSessionKey (caller == account, idempotent)
 * - isSessionKeyActive / getSessionKeyGrant (read-side)
 * - lazy install on first UserOp via `abi.encode(PermissionGrant, ecdsaSig)`
 * - EIP-712 verification of master-wallet grant signature
 * - replay protection via monotonic per-(account, signer) nonce
 *
 * STILL PENDING (separate work-leafs)
 * - cred-7: Pimlico CREATE2 cross-chain deploy + kernel v3 wiring.
 * - cred-8: subgraph indexing of SessionKeyInstalled / SessionKeyRevoked
 *           (out of scope for the validator itself).
 *
 * CROSS-SPEC INVARIANTS (load-bearing)
 *   1. CREATE2 address byte-equality pre/post install: this contract holds
 *      ALL session-key storage in its own mappings — NEVER in the parent
 *      Smart Account's slots. See `_grants` + `_minNonces` below.
 *   2. No TTL. `issuedAt` is stored but never compared to block.timestamp.
 *      Grants are valid until explicit revoke (PRD-01 §9 Q2).
 *   3. `valueMax` is enforced to be exactly 0 on the inner `execute` call —
 *      session keys cannot move ETH from the Smart Account.
 *   4. `executeBatch` per-inner-call scope validation (cred-6, spec §4.2):
 *      every inner call is checked individually for `target` / `value == 0` /
 *      selector membership. Validating only the outer selector or sampling
 *      the first inner is insufficient. Empty batches are rejected.
 */
contract SessionKeyModule is ISessionKeyModule {
    // -------------------------------------------------------------------------
    // ERC-4337 v0.7 validation-data constants
    // -------------------------------------------------------------------------

    /// @dev v0.7 EntryPoint treats `1` as SIG_VALIDATION_FAILED. Time-range
    ///      packing intentionally NOT used (no TTL — spec §3.1).
    uint256 internal constant SIG_VALIDATION_FAILED = 1;
    uint256 internal constant SIG_VALIDATION_SUCCESS = 0;

    /// @dev Only grant version this module accepts. Bumping requires a fresh
    ///      module deployment + spec §3.1 forward-compat clause.
    uint8 internal constant GRANT_VERSION = 1;

    /// @dev SimpleAccount.execute(address,uint256,bytes) selector.
    bytes4 internal constant EXECUTE_SELECTOR = 0xb61d27f6;
    /// @dev SimpleAccount.executeBatch(address[],uint256[],bytes[]) selector.
    bytes4 internal constant EXECUTE_BATCH_SELECTOR = 0x47e1da2a;

    // -------------------------------------------------------------------------
    // EIP-712 typehashes
    // Cred spec §3.1 defines the canonical typed-data payload — these constants
    // must match it byte-for-byte. The cred-9 cross-language parity test
    // (Python ↔ Solidity) is the load-bearing check that they stay aligned.
    // -------------------------------------------------------------------------

    bytes32 internal constant DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 internal constant SCOPE_TYPEHASH = keccak256(
        "Scope(address target,bytes4[] selectors,uint256 valueMax)"
    );
    bytes32 internal constant GRANT_TYPEHASH = keccak256(
        "SessionKeyPermissionGrant(address account,address signer,Scope scope,uint256 nonce,uint256 issuedAt)Scope(address target,bytes4[] selectors,uint256 valueMax)"
    );
    bytes32 internal constant DOMAIN_NAME_HASH = keccak256(bytes("TotalReclawSessionKey"));
    bytes32 internal constant DOMAIN_VERSION_HASH = keccak256(bytes("1"));

    // -------------------------------------------------------------------------
    // Storage — all session-key state lives HERE, never in the Smart Account.
    // This is the load-bearing constraint that preserves CREATE2 byte-equality
    // (cred spec §3.1 "Subgraph schema impact" paragraph + §8 R2).
    // -------------------------------------------------------------------------

    struct GrantStorage {
        uint256 nonce; // grant nonce; 0 = uninstalled
        uint256 issuedAt; // informational — never compared to block.timestamp
        address target; // scope.target (DataEdge address)
        uint256 valueMax; // always 0 per spec
        bytes4[] selectors; // scope.selectors (execute, executeBatch)
    }

    /// @dev `_grants[account][signer]` — active grant for this session key.
    ///      `nonce == 0` means "not installed".
    mapping(address account => mapping(address signer => GrantStorage)) private _grants;

    /// @dev `_minNonces[account][signer]` — smallest acceptable install
    ///      nonce. Starts at 1, bumps on revoke to `revokedNonce + 1`.
    ///      Prevents replay of a revoked grant.
    mapping(address account => mapping(address signer => uint256)) private _minNonces;

    // -------------------------------------------------------------------------
    // ABI-decoded calldata layout for the lazy-install UserOp.
    // `userOp.signature = abi.encode(PermissionGrant, ecdsaSig)` where
    // `PermissionGrant` is this struct and `ecdsaSig` is a 65-byte ECDSA over
    // the userOpHash (signed by the session key, not the master).
    // -------------------------------------------------------------------------

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
        bytes masterSignature; // 65-byte ECDSA from master wallet over EIP-712 digest
    }

    // -------------------------------------------------------------------------
    // Errors (kept inside the module — interface errors stay for ABI use)
    // -------------------------------------------------------------------------

    error UnknownGrantVersion(uint8 v);
    error InvalidSignatureLength();

    // -------------------------------------------------------------------------
    // ISessionKeyModule — mutating
    // -------------------------------------------------------------------------

    /// @inheritdoc ISessionKeyModule
    function validateSessionKeyUserOp(
        PackedUserOperation calldata userOp,
        bytes32 userOpHash
    ) external returns (uint256) {
        bytes calldata sig = userOp.signature;

        // Steady-state path: 65-byte raw ECDSA. Signer must already be installed.
        if (sig.length == 65) {
            address signer = _recoverEcdsa(userOpHash, sig);
            if (signer == address(0)) return SIG_VALIDATION_FAILED;

            GrantStorage storage g = _grants[msg.sender][signer];
            if (g.nonce == 0) return SIG_VALIDATION_FAILED;

            if (!_isCallDataInScope(userOp.callData, g.target, g.selectors)) {
                return SIG_VALIDATION_FAILED;
            }
            return SIG_VALIDATION_SUCCESS;
        }

        // Lazy-install path: signature is abi.encode(PermissionGrant, ecdsaSig).
        // Defensive decode — any malformed input → SIG_VALIDATION_FAILED.
        // We need bytes (not bytes calldata) here because abi.decode produces
        // memory copies; callData stays calldata.
        (bool ok, PermissionGrant memory grant, bytes memory ecdsaSig) = _tryDecodeInstallSig(sig);
        if (!ok) return SIG_VALIDATION_FAILED;

        // Verify session signer first — cheap, fails fast on tampered signatures.
        address signer = _recoverEcdsa(userOpHash, ecdsaSig);
        if (signer == address(0) || signer != grant.signer) return SIG_VALIDATION_FAILED;

        // Verify the grant itself.
        if (!_isGrantValid(grant)) return SIG_VALIDATION_FAILED;

        // Replay protection — nonce must be at-or-above the minimum.
        uint256 minN = _minNonces[msg.sender][signer];
        if (minN == 0) minN = 1; // first install
        if (grant.nonce < minN) return SIG_VALIDATION_FAILED;

        // Recover the master wallet from the EIP-712 grant signature and
        // verify it owns the calling Smart Account.
        bytes32 digest = _grantDigest(grant);
        address master = _recoverEcdsa(digest, grant.masterSignature);
        if (master == address(0)) return SIG_VALIDATION_FAILED;
        if (master != _smartAccountOwner(msg.sender)) return SIG_VALIDATION_FAILED;

        // All checks pass — install the grant.
        _grants[msg.sender][signer] = GrantStorage({
            nonce: grant.nonce,
            issuedAt: grant.issuedAt,
            target: grant.target,
            valueMax: grant.valueMax,
            selectors: grant.selectors
        });

        emit SessionKeyInstalled(msg.sender, signer, grant.nonce);

        // After install, validate the actual userOp callData against the
        // newly-stored scope — same path the steady-state branch takes.
        if (!_isCallDataInScope(userOp.callData, grant.target, grant.selectors)) {
            return SIG_VALIDATION_FAILED;
        }
        return SIG_VALIDATION_SUCCESS;
    }

    /// @inheritdoc ISessionKeyModule
    function revokeSessionKey(address account, address signer) external {
        // Master-wallet auth: the caller must BE the Smart Account itself
        // (i.e. the master wallet drove a UserOp into the SA which then
        // delegated to this revoke). Anyone calling directly is rejected.
        //
        // Idempotent — revoking an unknown / already-revoked signer is a
        // no-op (PRD-01 §11 R4). No revert, no event spam.
        if (msg.sender != account) {
            revert SessionKeyNotActive(account, signer);
        }

        GrantStorage storage g = _grants[account][signer];
        if (g.nonce == 0) {
            // Already revoked or never installed — idempotent no-op.
            return;
        }

        // Bump the min-nonce so this grant cannot be re-played even with the
        // original signed payload.
        _minNonces[account][signer] = g.nonce + 1;
        delete _grants[account][signer];

        emit SessionKeyRevoked(account, signer);
    }

    // -------------------------------------------------------------------------
    // ISessionKeyModule — views
    // -------------------------------------------------------------------------

    /// @inheritdoc ISessionKeyModule
    function isSessionKeyActive(
        address account,
        address signer
    ) external view returns (bool) {
        return _grants[account][signer].nonce != 0;
    }

    /// @inheritdoc ISessionKeyModule
    function getSessionKeyGrant(
        address account,
        address signer
    )
        external
        view
        returns (uint256 nonce, uint256 issuedAt, bytes4[] memory selectors, address target)
    {
        GrantStorage storage g = _grants[account][signer];
        return (g.nonce, g.issuedAt, g.selectors, g.target);
    }

    // -------------------------------------------------------------------------
    // Internal helpers
    // -------------------------------------------------------------------------

    /// @dev `try`/`catch` wrapper around `abi.decode` so a malformed install
    ///      signature returns `(false, ...)` instead of reverting the whole
    ///      validateUserOp callback. EntryPoint v0.7 treats a revert from
    ///      validateUserOp as `SIG_VALIDATION_FAILED` either way, but returning
    ///      cleanly is observably cheaper + keeps the trace easier to debug.
    function _tryDecodeInstallSig(
        bytes calldata sig
    ) internal view returns (bool ok, PermissionGrant memory grant, bytes memory ecdsaSig) {
        if (sig.length < 0x40) return (false, grant, ecdsaSig);

        try this._decodeInstallSig(sig) returns (
            PermissionGrant memory g,
            bytes memory s
        ) {
            return (true, g, s);
        } catch {
            return (false, grant, ecdsaSig);
        }
    }

    /// @dev Public-but-only-self entry for the try/catch above. Marked
    ///      external so the catch sees revert data; `require(msg.sender ==
    ///      address(this))` would defeat the staticcall semantics in some
    ///      kernel-v3 setups so we just trust the call boundary.
    function _decodeInstallSig(
        bytes calldata sig
    ) external pure returns (PermissionGrant memory grant, bytes memory ecdsaSig) {
        (grant, ecdsaSig) = abi.decode(sig, (PermissionGrant, bytes));
    }

    /// @dev Static grant-shape validation. Does NOT check the signature —
    ///      that lives in the caller because we want to recover the master
    ///      wallet only once.
    function _isGrantValid(PermissionGrant memory g) internal view returns (bool) {
        if (g.version != GRANT_VERSION) return false;
        if (g.account != msg.sender) return false; // grant binds to this SA
        if (g.chainId != block.chainid) return false; // cross-chain replay guard
        if (g.verifyingContract != address(this)) return false; // module-binding
        if (g.valueMax != 0) return false; // session keys cannot move ETH
        if (g.target == address(0)) return false;
        if (g.signer == address(0)) return false;
        if (g.selectors.length == 0) return false;
        if (g.masterSignature.length != 65) return false;
        return true;
    }

    /// @dev EIP-712 digest matching the spec §3.1 typed-data layout.
    function _grantDigest(PermissionGrant memory g) internal pure returns (bytes32) {
        bytes32 scopeHash = keccak256(
            abi.encode(
                SCOPE_TYPEHASH,
                g.target,
                keccak256(abi.encodePacked(g.selectors)),
                g.valueMax
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
        return keccak256(abi.encodePacked(hex"19_01", domainSep, structHash));
    }

    /// @dev Looks up the SA's master wallet via the SimpleAccount-style
    ///      `owner()` interface. Returns address(0) if the call reverts —
    ///      the caller treats that as SIG_VALIDATION_FAILED.
    function _smartAccountOwner(address account) internal view returns (address) {
        try IAccountWithOwner(account).owner() returns (address o) {
            return o;
        } catch {
            return address(0);
        }
    }

    /// @dev Scope validation. `execute` checks the outer single call's
    ///      `target` + `value`; `executeBatch` recurses into every inner call
    ///      and verifies each one against the grant. Spec §4.2 cross-spec
    ///      invariant requires per-inner validation — sampling first inner or
    ///      validating only outer selector is insufficient.
    function _isCallDataInScope(
        bytes calldata callData,
        address grantTarget,
        bytes4[] memory allowedSelectors
    ) internal pure returns (bool) {
        if (callData.length < 4) return false;
        bytes4 outerSel = bytes4(callData[:4]);

        // Selector must be in the SA-level allowlist.
        if (!_inSelectors(outerSel, allowedSelectors)) return false;

        if (outerSel == EXECUTE_SELECTOR) {
            // execute(address target, uint256 value, bytes data)
            if (callData.length < 4 + 32 * 3) return false;
            (address innerTarget, uint256 value, ) = abi.decode(
                callData[4:],
                (address, uint256, bytes)
            );
            return innerTarget == grantTarget && value == 0;
        }

        if (outerSel == EXECUTE_BATCH_SELECTOR) {
            // executeBatch(address[] targets, uint256[] values, bytes[] datas).
            // 3 × 32-byte head offsets at minimum.
            if (callData.length < 4 + 32 * 3) return false;
            (
                address[] memory targets,
                uint256[] memory values,
                bytes[] memory datas
            ) = abi.decode(callData[4:], (address[], uint256[], bytes[]));

            uint256 n = targets.length;
            // Empty batches add no value and let a misconfigured client slip
            // a no-op past validation — reject explicitly.
            if (n == 0) return false;
            if (values.length != n || datas.length != n) return false;

            for (uint256 i = 0; i < n; i++) {
                if (targets[i] != grantTarget) return false;
                if (values[i] != 0) return false;
                bytes memory inner = datas[i];
                if (inner.length < 4) return false;
                bytes4 innerSel;
                // solhint-disable-next-line no-inline-assembly
                assembly {
                    innerSel := mload(add(inner, 0x20))
                }
                if (!_inSelectors(innerSel, allowedSelectors)) return false;
            }
            return true;
        }

        return false;
    }

    function _inSelectors(bytes4 needle, bytes4[] memory hay) internal pure returns (bool) {
        for (uint256 i = 0; i < hay.length; i++) {
            if (hay[i] == needle) return true;
        }
        return false;
    }

    /// @dev Minimal ECDSA recover (r, s, v). Returns address(0) on malformed
    ///      input or `s` in the upper half of the curve order (malleability
    ///      guard — same EIP-2 split-rule OpenZeppelin uses).
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
