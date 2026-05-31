// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {SessionKeyModule} from "../contracts/SessionKeyModule.sol";

/**
 * @title DeploySessionKeyModule
 * @notice cred-7 stage 1 — CREATE2 deploy script. NO BROADCAST in this
 *         stage. Computes the predicted CREATE2 address so stage 2 (actual
 *         on-chain broadcast with funded deployer) can verify byte-equality
 *         across chains before submitting.
 *
 *         Uses Arachnid's deterministic deployment proxy at
 *         `0x4e59b44847b379578588920cA78FbF26c0B4956C`. This factory exists
 *         at the same address on every major EVM chain via a one-shot
 *         raw-transaction deployment (see eip-2470 + Arachnid pattern).
 *         Same factory + same bytecode + same salt → byte-equal address on
 *         every chain. This is the load-bearing invariant for cred §8 R2
 *         (CREATE2 byte-equality post module install).
 *
 *         Stage 2 (separate PR, Pedro-supervised):
 *           - Funded deployer wallet (Base Sepolia faucet ETH +
 *             Gnosis xDAI from Pedro's hot wallet).
 *           - `--broadcast` on both chains.
 *           - Update `contracts/deployed-addresses.json` with the
 *             deployed address + tx hashes.
 *           - Verify on Etherscan / Gnosisscan.
 *           - Un-skip `test_create2_address_unchanged_after_module_install`
 *             with the on-chain assertion baked against the actual
 *             deployed address (already done in this stage as a predicted-
 *             address assertion).
 *
 * Usage (stage 1 — predicted-address only):
 *   forge script script/DeploySessionKeyModule.s.sol --rpc-url $RPC_URL
 *
 *   The script prints the predicted CREATE2 address. Run on both
 *   Base Sepolia and Gnosis to confirm they match.
 *
 * Usage (stage 2 — when ready to broadcast, separate PR):
 *   forge script script/DeploySessionKeyModule.s.sol \
 *       --rpc-url $BASE_SEPOLIA_RPC_URL \
 *       --private-key $DEPLOYER_PRIVATE_KEY \
 *       --broadcast \
 *       --verify
 *
 *   Repeat for Gnosis. Assert the resulting address matches stage-1's
 *   predicted address.
 */
contract DeploySessionKeyModule is Script {
    // Arachnid's deterministic deployment proxy is pinned via EIP-2470
    // at `0x4e59b44847b379578588920cA78FbF26c0B4956C` on every major EVM
    // chain. forge-std exposes this as `Base.CREATE2_FACTORY`; we reuse
    // that constant here (do NOT redeclare — it would collide with the
    // inherited Script → Base.CREATE2_FACTORY).

    /// @dev Deterministic salt for the v1 SessionKeyModule. Bumping this
    ///      yields a fresh address; intentional re-deploys for v2 should
    ///      use a different salt to avoid address collision.
    bytes32 internal constant SESSION_KEY_MODULE_SALT_V1 =
        keccak256("totalreclaw.SessionKeyModule.v1");

    function run() external {
        bytes memory initCode = abi.encodePacked(type(SessionKeyModule).creationCode);
        bytes32 initCodeHash = keccak256(initCode);

        // Predicted CREATE2 address: keccak256(0xff ++ factory ++ salt ++ initCodeHash)[12:]
        address predicted = address(
            uint160(
                uint256(
                    keccak256(
                        abi.encodePacked(
                            bytes1(0xff),
                            CREATE2_FACTORY,
                            SESSION_KEY_MODULE_SALT_V1,
                            initCodeHash
                        )
                    )
                )
            )
        );

        console2.log("=== SessionKeyModule CREATE2 deployment ===");
        console2.log("Chain id:       ", block.chainid);
        console2.log("Factory:        ", CREATE2_FACTORY);
        console2.log("Salt:           ");
        console2.logBytes32(SESSION_KEY_MODULE_SALT_V1);
        console2.log("Init code hash: ");
        console2.logBytes32(initCodeHash);
        console2.log("Predicted addr: ", predicted);

        // Check if already deployed (idempotent — running script twice
        // on the same chain should not redeploy).
        if (predicted.code.length > 0) {
            console2.log("Already deployed at predicted address. Skipping.");
            return;
        }

        // Stage 1: NO BROADCAST. Predicted address logged only.
        // Stage 2 (Pedro-supervised) replaces the body below with the
        // actual factory call:
        //
        //   vm.startBroadcast();
        //   (bool ok, bytes memory ret) = CREATE2_FACTORY.call(
        //       abi.encodePacked(SESSION_KEY_MODULE_SALT_V1, initCode)
        //   );
        //   require(ok, "CREATE2 deploy failed");
        //   address deployed = address(bytes20(ret[12:]));
        //   require(deployed == predicted, "CREATE2 address mismatch");
        //   vm.stopBroadcast();

        console2.log("Stage 1: predicted address only - no broadcast.");
    }
}
