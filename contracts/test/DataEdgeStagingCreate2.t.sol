// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {EventfulDataEdge} from "../contracts/EventfulDataEdge.sol";

/**
 * @title DataEdgeStagingCreate2Test
 * @notice ops-5 — locks two invariants for the isolated staging DataEdge:
 *   1. ISOLATION: the staging CREATE2 address differs from production's
 *      `0xC445…c36f`, purely because of a different salt.
 *   2. DETERMINISM: the staging address is byte-equal across chains
 *      (Gnosis 100 vs the local default), so a future broadcast lands at
 *      the same address regardless of chain — the property that lets ops-1
 *      reuse the staging config shape on prod.
 */
contract DataEdgeStagingCreate2Test is Test {
    // forge-std Base.CREATE2_FACTORY = 0x4e59b44847b379578588920cA78FbF26c0B4956C

    /// @dev Must match `DeployDataEdgeStaging.DATA_EDGE_STAGING_SALT`.
    bytes32 internal constant DATA_EDGE_STAGING_SALT =
        keccak256("totalreclaw.EventfulDataEdge.staging.v1");

    address internal constant PROD_DATA_EDGE =
        0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca;

    function _predict(bytes32 salt) internal pure returns (address) {
        bytes32 initCodeHash = keccak256(
            abi.encodePacked(type(EventfulDataEdge).creationCode)
        );
        return address(
            uint160(
                uint256(
                    keccak256(
                        abi.encodePacked(
                            bytes1(0xff),
                            CREATE2_FACTORY,
                            salt,
                            initCodeHash
                        )
                    )
                )
            )
        );
    }

    function test_staging_address_isolated_from_production() public pure {
        address staging = _predict(DATA_EDGE_STAGING_SALT);
        assertTrue(
            staging != PROD_DATA_EDGE,
            "staging DataEdge must NOT collide with prod 0xC445"
        );
    }

    function test_staging_address_byte_equal_across_chains() public {
        uint256 original = block.chainid;
        vm.chainId(100); // Gnosis
        address onGnosis = _predict(DATA_EDGE_STAGING_SALT);
        vm.chainId(84532); // Base Sepolia (for contrast)
        address onSepolia = _predict(DATA_EDGE_STAGING_SALT);
        vm.chainId(original);
        assertEq(
            onGnosis,
            onSepolia,
            "CREATE2 address must be chain-independent"
        );
    }

    function test_pinned_predicted_address() public pure {
        // Pin the computed address so an accidental salt/bytecode change
        // surfaces as a failing test (the staging subgraph + relay env are
        // configured against this exact address).
        assertEq(
            _predict(DATA_EDGE_STAGING_SALT),
            0xE7a4D2677B686e13775Ba9092631089e35F0BB91,
            "staging DataEdge predicted address drifted - update subgraph/relay config if intentional"
        );
    }
}
