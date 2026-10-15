// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {EventfulDataEdgeV2} from "../contracts/EventfulDataEdgeV2.sol";

/**
 * @title EventfulDataEdgeV2Test
 * @notice Behavioral coverage for the audit-1.1 fix contract. The security
 *         property lives in the subgraph mapping, but it rests on ONE contract
 *         guarantee: every write emits `Log(msg.sender, data)` with `sender`
 *         equal to the actual caller and `data` equal to the exact calldata.
 *         These tests pin exactly that, plus permissionlessness (anyone may
 *         append) and that `receive()` stays silent.
 */
contract EventfulDataEdgeV2Test is Test {
    EventfulDataEdgeV2 internal edge;
    address internal alice;
    address internal bob;

    // Mirror of the contract's event for expectEmit matching.
    event Log(address indexed sender, bytes data);

    function setUp() public {
        edge = new EventfulDataEdgeV2();
        alice = makeAddr("alice");
        bob = makeAddr("bob");
    }

    // ------------------------------------------------------------------
    // Core guarantee: sender == caller, data == calldata
    // ------------------------------------------------------------------

    function test_fallback_emitsCallerAsSender() public {
        bytes memory payload = hex"0a04deadbeef";

        vm.expectEmit(true, false, false, true, address(edge));
        emit Log(alice, payload);

        vm.prank(alice);
        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "fallback call must succeed");
    }

    /// The whole point of V2: two different callers sending the SAME payload
    /// (same fact id) produce events with DIFFERENT senders. This is what lets
    /// the subgraph tell the owner apart from an impersonator.
    function test_fallback_samePayloadDifferentCallers_distinctSenders() public {
        bytes memory payload = bytes("audit-fact-1");

        vm.recordLogs();
        vm.prank(alice);
        (bool ok1, ) = address(edge).call(payload);
        vm.prank(bob);
        (bool ok2, ) = address(edge).call(payload);
        assertTrue(ok1 && ok2, "both calls must succeed");

        Vm.Log[] memory entries = vm.getRecordedLogs();
        assertEq(entries.length, 2, "two Log events");
        // topic[1] is the indexed sender, left-padded to 32 bytes.
        assertEq(entries[0].topics[1], bytes32(uint256(uint160(alice))), "first sender = alice");
        assertEq(entries[1].topics[1], bytes32(uint256(uint160(bob))), "second sender = bob");
    }

    function test_fallback_isPermissionless_anyCallerSucceeds() public {
        vm.prank(bob);
        (bool ok, ) = address(edge).call(hex"1234");
        assertTrue(ok, "any address may append its own write");
    }

    function test_fallback_handlesLargePayload() public {
        bytes memory payload = new bytes(1024);
        for (uint256 i = 0; i < payload.length; i++) {
            payload[i] = bytes1(uint8(i % 256));
        }

        vm.expectEmit(true, false, false, true, address(edge));
        emit Log(address(this), payload);
        (bool ok, ) = address(edge).call(payload);
        assertTrue(ok, "large payload must emit intact");
    }

    // ------------------------------------------------------------------
    // receive(): plain ETH, no event
    // ------------------------------------------------------------------

    function test_receive_acceptsEthWithoutEmitting() public {
        vm.deal(address(this), 1 ether);
        vm.recordLogs();
        (bool ok, ) = address(edge).call{value: 1 wei}("");
        assertTrue(ok, "receive must accept empty-calldata ETH");
        assertEq(vm.getRecordedLogs().length, 0, "receive must emit nothing");
    }
}
