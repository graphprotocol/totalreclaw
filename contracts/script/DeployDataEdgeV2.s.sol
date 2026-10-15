// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {EventfulDataEdgeV2} from "../contracts/EventfulDataEdgeV2.sol";

/**
 * @title DeployDataEdgeV2
 * @notice CREATE2 deploy script for EventfulDataEdgeV2 (audit 1.1 fix). Mirrors
 *         DeployDataEdgeStaging: predict-only without --broadcast (no key, safe
 *         anywhere), real deploy with --broadcast + a funded deployer.
 *
 *         Two salts, selected by env `TR_DATAEDGE_V2_TARGET` (default "staging"):
 *           - "staging" -> isolated staging instance
 *           - "prod"    -> production instance
 *         Distinct salts yield distinct, chain-independent addresses, exactly as
 *         the v1 prod/staging split does.
 *
 *         NOTE ON THE ADDRESS PIN: unlike DeployDataEdgeStaging, this script does
 *         NOT assert against a hardcoded EXPECTED address. The outer Foundry
 *         project keeps solc's default (ipfs) metadata hash, so `creationCode`
 *         — and therefore the CREATE2 address — is not byte-reproducible across
 *         toolchains, and a hardcoded literal computed on one machine would be a
 *         false pin. The script instead computes the predicted address in-run and
 *         asserts the factory returned it, which is the guarantee that matters.
 *         Record the resulting address in deployed-addresses.json and wire it
 *         into the subgraph V2 data source (currently a 0x0…0 placeholder).
 *
 * Usage (predict-only — no key, no tx):
 *   forge script script/DeployDataEdgeV2.s.sol --rpc-url $GNOSIS_RPC_URL
 *
 * Usage (broadcast — Pedro-supervised, funded deployer on Gnosis):
 *   cast wallet import tr-deployer --interactive   # one-time
 *   TR_DATAEDGE_V2_TARGET=staging forge script script/DeployDataEdgeV2.s.sol \
 *       --rpc-url https://rpc.gnosischain.com --account tr-deployer --broadcast
 */
contract DeployDataEdgeV2 is Script {
    // Arachnid deterministic factory, inherited as Base.CREATE2_FACTORY
    // (0x4e59b44847b379578588920cA78FbF26c0B4956C). Do NOT redeclare.

    bytes32 internal constant SALT_PROD =
        keccak256("totalreclaw.EventfulDataEdgeV2.v1");
    bytes32 internal constant SALT_STAGING =
        keccak256("totalreclaw.EventfulDataEdgeV2.staging.v1");

    function run() external {
        string memory target = vm.envOr("TR_DATAEDGE_V2_TARGET", string("staging"));
        bool isProd = keccak256(bytes(target)) == keccak256(bytes("prod"));
        bytes32 salt = isProd ? SALT_PROD : SALT_STAGING;

        bytes memory initCode = abi.encodePacked(type(EventfulDataEdgeV2).creationCode);
        bytes32 initCodeHash = keccak256(initCode);
        address predicted = address(
            uint160(
                uint256(
                    keccak256(
                        abi.encodePacked(bytes1(0xff), CREATE2_FACTORY, salt, initCodeHash)
                    )
                )
            )
        );

        console2.log("=== EventfulDataEdgeV2 CREATE2 deployment ===");
        console2.log("Target:          ", target);
        console2.log("Chain id:        ", block.chainid);
        console2.log("Factory:         ", CREATE2_FACTORY);
        console2.log("Salt:");
        console2.logBytes32(salt);
        console2.log("Init code hash:");
        console2.logBytes32(initCodeHash);
        console2.log("Predicted addr:  ", predicted);

        if (predicted.code.length > 0) {
            console2.log("Already deployed at predicted address. Nothing to do.");
            return;
        }

        vm.startBroadcast();
        (bool ok, bytes memory ret) = CREATE2_FACTORY.call(
            abi.encodePacked(salt, initCode)
        );
        vm.stopBroadcast();

        require(ok, "CREATE2 factory call failed");
        address deployed = address(bytes20(ret));
        require(deployed == predicted, "deployed addr != predicted");

        console2.log("Deployed EventfulDataEdgeV2 at:", deployed);
        console2.log("-> record in deployed-addresses.json and set the subgraph V2 data source address + startBlock.");
    }
}
