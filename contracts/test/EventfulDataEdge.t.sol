// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {EventfulDataEdge} from "../contracts/EventfulDataEdge.sol";

/**
 * @title EventfulDataEdgeTest
 * @notice Behavioral coverage for the live EventfulDataEdge data-availability
 *         contract, ported from the Hardhat suite `test/EventfulDataEdge.test.ts`
 *         (#650 phase 2c — Hardhat retirement). The pre-existing
 *         `DataEdgeStagingCreate2.t.sol` only pins CREATE2 addresses; this file
 *         pins the actual runtime behavior:
 *           - deployer becomes owner
 *           - fallback() emits Log(calldata) for any caller, any payload size
 *           - receive() accepts plain ETH transfers without emitting
 *           - transferOwnership access control + zero-address guard
 */
contract EventfulDataEdgeTest is Test {
    EventfulDataEdge internal edge;

    /// @dev Stand-in for an arbitrary non-owner address.
    address internal user;

    function setUp() public {
        edge = new EventfulDataEdge();
        user = makeAddr("user");
    }

    // ------------------------------------------------------------------
    // Deployment
    // ------------------------------------------------------------------

    /// TS: "should set the deployer as owner"
    function test_deployment_setsDeployerAsOwner() public {
        // setUp deployed from the test contract's perspective.
        assertEq(edge.owner(), address(this), "setUp deployer must own the edge");

        // A fresh deploy from a different sender must crown that sender.
        vm.prank(user);
        EventfulDataEdge fresh = new EventfulDataEdge();
        assertEq(fresh.owner(), user, "fresh deploy must set msg.sender as owner");
    }

    // ------------------------------------------------------------------
    // Log emission via fallback
    // ------------------------------------------------------------------

    /// TS: "should emit Log event with calldata" — any calldata sent to the
    /// contract must produce a Log emitted BY the edge itself.
    function test_fallback_emitsLogAtEdgeAddress() public {
        bytes memory payload = bytes("encrypted-protobuf-payload-here");

        vm.expectEmit(true, true, true, true, address(edge));
        emit EventfulDataEdge.Log(payload);

        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "fallback call must succeed");
    }

    /// TS: "should allow any address to write (permissionless)" — a non-owner
    /// caller is not filtered, and the emitted data matches the calldata exactly.
    function test_fallback_isPermissionless_nonOwnerWritesSucceed() public {
        bytes memory payload = bytes("user-encrypted-payload");

        vm.prank(user);
        vm.expectEmit(true, true, true, true, address(edge));
        emit EventfulDataEdge.Log(payload);

        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "permissionless write from non-owner must succeed");
    }

    /// TS: "should emit correct data bytes in Log event" — a 128-byte
    /// pseudo-random payload round-trips byte-for-byte through the Log event.
    function test_fallback_emitsExactPayloadBytes() public {
        bytes memory payload = _pseudoRandomPayload(4); // 4 * 32 = 128 bytes

        vm.expectEmit(true, true, true, true, address(edge));
        emit EventfulDataEdge.Log(payload);

        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "128-byte payload write must succeed");
    }

    /// TS: "should handle large payloads (1KB)" — a 1 KiB payload is emitted
    /// in full (exact-match is strictly stronger than the TS `length > 0` check).
    function test_fallback_handlesLargePayload1KB() public {
        bytes memory payload = _pseudoRandomPayload(32); // 32 * 32 = 1024 bytes

        vm.expectEmit(true, true, true, true, address(edge));
        emit EventfulDataEdge.Log(payload);

        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "1KB payload write must succeed");
    }

    /// Extra (beyond the TS suite): fallback is `payable`, so calldata + value
    /// must also go through the Log path without reverting.
    function test_fallback_isPayable_acceptsCalldataWithValue() public {
        vm.deal(user, 1 ether);
        bytes memory payload = bytes("payable-write");

        vm.prank(user);
        vm.expectEmit(true, true, true, true, address(edge));
        emit EventfulDataEdge.Log(payload);

        (bool ok, ) = address(edge).call{value: 0.01 ether}(payload);
        assertTrue(ok, "payable fallback with calldata must succeed");
        assertEq(address(edge).balance, 0.01 ether, "edge must hold the sent ETH");
    }

    // ------------------------------------------------------------------
    // receive()
    // ------------------------------------------------------------------

    /// TS: "should handle empty calldata via receive()" — a plain ETH transfer
    /// (no calldata) succeeds. Strengthened: it must NOT emit a Log event,
    /// since receive() is a separate no-op code path from fallback().
    function test_receive_acceptsEthWithoutCalldataAndDoesNotEmit() public {
        vm.deal(user, 1 ether);

        vm.recordLogs();
        (bool ok, ) = address(edge).call{value: 0.01 ether}("");
        assertTrue(ok, "receive() must accept plain ETH transfers");
        assertEq(address(edge).balance, 0.01 ether, "edge must hold the sent ETH");

        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            assertTrue(
                logs[i].emitter != address(edge),
                "receive() must not emit Log - only fallback() writes"
            );
        }
    }

    // ------------------------------------------------------------------
    // transferOwnership
    // ------------------------------------------------------------------

    /// TS: "should allow owner to transfer ownership"
    function test_transferOwnership_ownerCanTransferToNewOwner() public {
        edge.transferOwnership(user);
        assertEq(edge.owner(), user, "owner must be updated after transfer");
    }

    /// TS: "should reject non-owner transferring ownership"
    function test_transferOwnership_revertWhen_callerIsNotOwner() public {
        vm.prank(user);
        vm.expectRevert("Only owner");
        edge.transferOwnership(user);

        assertEq(edge.owner(), address(this), "failed transfer must not change owner");
    }

    /// TS: "should reject zero address for transferOwnership"
    function test_transferOwnership_revertWhen_newOwnerIsZeroAddress() public {
        vm.expectRevert("Invalid owner");
        edge.transferOwnership(address(0));

        assertEq(edge.owner(), address(this), "failed transfer must not change owner");
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /// @dev Deterministic pseudo-random payload of `chunks * 32` bytes
    ///      (stands in for ethers.randomBytes(n)).
    function _pseudoRandomPayload(uint256 chunks) internal pure returns (bytes memory payload) {
        for (uint256 i; i < chunks; ++i) {
            payload = abi.encodePacked(payload, keccak256(abi.encode(i)));
        }
    }
}
