// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {EventfulDataEdge} from "../contracts/EventfulDataEdge.sol";

/**
 * @title DeployDataEdgeStaging
 * @notice ops-5 (script-stage) — CREATE2 deploy script for an ISOLATED
 *         staging instance of EventfulDataEdge on Gnosis mainnet.
 *         NO BROADCAST in this stage. Computes + logs the predicted CREATE2
 *         address so ops-6 (staging subgraph) can be pre-configured and
 *         ops-7/8 (relay env) know the target before the funded broadcast.
 *
 *         Per the staging-chain-isolation spec
 *         (`totalreclaw-internal/docs/specs/ops/staging-chain-isolation.md`):
 *         staging runs on Gnosis (chain 100) — same chain as production —
 *         but writes to a DIFFERENT DataEdge address so staging on-chain
 *         activity is isolated from production's `0xC445…c36f`.
 *
 *         The isolation comes purely from a DIFFERENT CREATE2 SALT:
 *           - prod salt → `0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca`
 *           - staging salt (this script) → a distinct address.
 *
 *         EventfulDataEdge has a PARAM-LESS constructor (`owner = msg.sender`),
 *         so its init code is deterministic → the CREATE2 address depends
 *         only on (factory, salt, initCodeHash) and is byte-equal on every
 *         chain. Under the Arachnid factory, `msg.sender` in the constructor
 *         resolves to the factory itself, so `owner` = factory — irrelevant,
 *         because EventfulDataEdge is a permissionless `fallback()`-only
 *         emitter with no owner-gated write path (same as prod, see CLAUDE.md
 *         "DataEdge: permissionless").
 *
 * Usage (script-stage — predicted address only):
 *   forge script script/DeployDataEdgeStaging.s.sol --rpc-url $GNOSIS_RPC_URL
 *
 * Usage (broadcast-stage — separate Pedro-supervised step, funded deployer):
 *   forge script script/DeployDataEdgeStaging.s.sol \
 *       --rpc-url $GNOSIS_RPC_URL \
 *       --private-key $DEPLOYER_PRIVATE_KEY \
 *       --broadcast --verify
 *   Then: record the address in `contracts/deployed-addresses.json` under a
 *   `stagingGnosis` key + set the staging relay's DATA_EDGE_ADDRESS /
 *   PRO_DATA_EDGE_ADDRESS to it (ops-7/8).
 */
contract DeployDataEdgeStaging is Script {
    // forge-std exposes Arachnid's deterministic deployment proxy as
    // `Base.CREATE2_FACTORY` (0x4e59b44847b379578588920cA78FbF26c0B4956C),
    // inherited via Script. Do NOT redeclare.

    /// @dev Staging-specific salt — DISTINCT from production's DataEdge salt,
    ///      which is what yields a different address on the same chain. Bump
    ///      the version suffix only for an intentional staging re-deploy.
    bytes32 internal constant DATA_EDGE_STAGING_SALT =
        keccak256("totalreclaw.EventfulDataEdge.staging.v1");

    function run() external view {
        bytes memory initCode = abi.encodePacked(type(EventfulDataEdge).creationCode);
        bytes32 initCodeHash = keccak256(initCode);

        address predicted = address(
            uint160(
                uint256(
                    keccak256(
                        abi.encodePacked(
                            bytes1(0xff),
                            CREATE2_FACTORY,
                            DATA_EDGE_STAGING_SALT,
                            initCodeHash
                        )
                    )
                )
            )
        );

        console2.log("=== Staging EventfulDataEdge CREATE2 deployment ===");
        console2.log("Chain id:        ", block.chainid);
        console2.log("Factory:         ", CREATE2_FACTORY);
        console2.log("Salt:            ");
        console2.logBytes32(DATA_EDGE_STAGING_SALT);
        console2.log("Init code hash:  ");
        console2.logBytes32(initCodeHash);
        console2.log("Predicted addr:  ", predicted);
        console2.log("Prod DataEdge:   0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca");
        console2.log("Isolated?:       ", predicted != 0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca);

        if (predicted.code.length > 0) {
            console2.log("Already deployed at predicted address. Skipping.");
            return;
        }
        console2.log("Script-stage: predicted address only - no broadcast.");
    }
}
