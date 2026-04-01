#!/usr/bin/env python3
"""
Cross-Client Debrief Interop E2E Test (Test 2 from session debrief E2E plan).

Proves that debrief items stored by MCP can be recalled by Python and vice versa,
using the same mnemonic/wallet against the live staging relay.

Flow:
  1. Generate fresh BIP-39 mnemonic (clean slate)
  2. MCP stores 2 debrief items on-chain via ts-helper.ts (source: mcp_debrief)
  3. Wait 40s for subgraph indexing
  4. Python recalls the MCP-stored debrief items
  5. Python stores a debrief item (source: hermes_debrief)
  6. Wait 40s for subgraph indexing
  7. MCP recalls the Python-stored debrief item via ts-helper.ts

Requires: Node.js >= 22, Python venv with totalreclaw deps, mcp/ deps installed.

Run:
  cd python && source .venv/bin/activate && \
  TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz \
  TOTALRECLAW_TEST=true \
  python ../tests/e2e-debrief/cross-client-debrief-e2e.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

# Add parent to path for imports
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python", "src")
)

from mnemonic import Mnemonic
from totalreclaw.client import TotalReclaw

RELAY_URL = os.environ.get(
    "TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz"
)
# ts-helper.ts lives in the same directory as this file
TS_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ts-helper.ts")
MCP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mcp")
INDEXING_WAIT = 40  # seconds for subgraph to index on-chain data


def generate_mnemonic() -> str:
    """Generate a fresh 12-word BIP-39 mnemonic."""
    m = Mnemonic("english")
    return m.generate(128)


def run_ts_helper(
    args: list[str], mnemonic: str, timeout: int = 180
) -> str:
    """Run ts-helper.ts with the given arguments via npx tsx.

    The helper is run from the mcp/ directory so it can resolve
    imports from the MCP server source tree.
    """
    env = {
        **os.environ,
        "TEST_MNEMONIC": mnemonic,
        "TOTALRECLAW_SERVER_URL": RELAY_URL,
        "TOTALRECLAW_TEST": "true",
    }

    # ts-helper.ts needs to be referenced by absolute path since cwd is mcp/
    cmd = ["npx", "tsx", TS_HELPER] + args

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=MCP_DIR,
        env=env,
    )

    if result.returncode != 0:
        stderr_preview = result.stderr[:2000] if result.stderr else "(no stderr)"
        raise RuntimeError(
            f"ts-helper.ts failed (exit {result.returncode}):\n{stderr_preview}"
        )

    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Text matching helper
# ---------------------------------------------------------------------------


def _text_matches(expected: str, actual: str) -> bool:
    """Check if two texts match by comparing key distinctive words.

    Subgraph recall may return slightly different text due to protobuf encoding
    or truncation, so we check for overlap of significant words rather than
    exact equality.
    """
    expected_lower = expected.lower()
    actual_lower = actual.lower()

    # Extract significant words (skip common stop words)
    stop_words = {
        "a", "an", "the", "is", "was", "are", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "for", "of", "in", "to",
        "at", "by", "on", "with", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "and", "but", "or",
        "not", "no", "nor", "so", "yet", "both", "either", "neither", "each",
        "this", "that", "these", "those", "it", "its",
    }

    expected_words = {
        w for w in expected_lower.split() if len(w) > 2 and w not in stop_words
    }

    if not expected_words:
        return expected_lower in actual_lower

    # Count how many distinctive expected words appear in the actual text
    matches = sum(1 for w in expected_words if w in actual_lower)
    # Require at least 60% of distinctive words to match
    threshold = max(1, len(expected_words) * 6 // 10)
    return matches >= threshold


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


async def main():
    print("=" * 60)
    print("Cross-Client Debrief Interop E2E Test")
    print("MCP -> Python  |  Python -> MCP")
    print("=" * 60)
    print(f"  Relay:     {RELAY_URL}")
    print(f"  Helper:    {TS_HELPER}")
    print(f"  MCP dir:   {MCP_DIR}")

    results: dict[str, bool] = {}

    # Debrief items stored by MCP side
    mcp_debrief_items = [
        {
            "text": "The session was about implementing a caching layer for the API gateway",
            "importance": 0.8,
            "source": "mcp_debrief",
        },
        {
            "text": "Redis was chosen over Memcached because the team needs pub/sub for invalidation",
            "importance": 0.7,
            "source": "mcp_debrief",
        },
    ]

    # Debrief item stored by Python side
    py_debrief_text = "Follow-up needed: Redis cluster sizing for production traffic"

    # -------------------------------------------------------------------------
    # Step 1: Generate fresh mnemonic
    # -------------------------------------------------------------------------
    mnemonic = generate_mnemonic()
    print(f"\n1. Generated test mnemonic: {mnemonic[:25]}...")

    # -------------------------------------------------------------------------
    # Step 2: Initialize Python client and resolve Smart Account
    # -------------------------------------------------------------------------
    print("\n2. Initializing Python client...")
    py_client = TotalReclaw(mnemonic=mnemonic, relay_url=RELAY_URL, is_test=True)
    await py_client.resolve_address()
    wallet = py_client.wallet_address
    print(f"   Smart Account: {wallet}")

    # Register with the relay
    try:
        user_id = await py_client.register()
        print(f"   Registered: {user_id}")
    except Exception as e:
        print(f"   Registration: {e}")

    # -------------------------------------------------------------------------
    # Step 3: MCP stores 2 debrief items via ts-helper.ts
    # -------------------------------------------------------------------------
    print("\n3. MCP storing 2 debrief items via ts-helper.ts...")

    mcp_store_ok = True
    for i, item in enumerate(mcp_debrief_items):
        try:
            output = run_ts_helper(
                [
                    "store",
                    wallet,
                    item["text"],
                    str(item["importance"]),
                    item["source"],
                ],
                mnemonic,
            )
            print(f"   Item {i + 1} output:")
            for line in output.split("\n"):
                print(f"     {line}")

            if "success=true" not in output:
                print(f"   Item {i + 1} store did NOT succeed.")
                mcp_store_ok = False
                break

        except Exception as e:
            print(f"   Item {i + 1} store FAILED: {e}")
            mcp_store_ok = False
            break

    if not mcp_store_ok:
        results["mcp_store_py_recall"] = False
        results["py_store_mcp_recall"] = False
        await py_client.close()
        _print_summary(results)
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Step 4: Wait for subgraph indexing (40s)
    # -------------------------------------------------------------------------
    print(f"\n4. Waiting {INDEXING_WAIT}s for subgraph indexing...")
    await asyncio.sleep(INDEXING_WAIT)

    # -------------------------------------------------------------------------
    # Step 5: Python recalls the MCP-stored debrief items
    # -------------------------------------------------------------------------
    print("\n5. Python recalling MCP-stored debrief items...")
    print('   Query: "caching API gateway Redis"')

    try:
        recall_results = await py_client.recall(
            "caching API gateway Redis", top_k=8
        )
        texts = [r.text for r in recall_results]
        print(f"   Found {len(recall_results)} results")
        for r in recall_results:
            print(f"     [{r.rrf_score:.4f}] {r.text[:120]}")

        # Check if at least one MCP debrief item was found
        found_mcp_items = []
        for item in mcp_debrief_items:
            mcp_text = item["text"]
            for recalled_text in texts:
                if _text_matches(mcp_text, recalled_text):
                    found_mcp_items.append(mcp_text)
                    break

        py_recalled_mcp = len(found_mcp_items) >= 1
        results["mcp_store_py_recall"] = py_recalled_mcp
        print(
            f"   Python found MCP debrief items: "
            f"{len(found_mcp_items)}/{len(mcp_debrief_items)} "
            f"({'PASS' if py_recalled_mcp else 'FAIL'})"
        )
        for item_text in found_mcp_items:
            print(f"     Matched: {item_text[:80]}...")

    except Exception as e:
        print(f"   Python recall FAILED: {e}")
        import traceback
        traceback.print_exc()
        results["mcp_store_py_recall"] = False

    # -------------------------------------------------------------------------
    # Step 6: Python stores a debrief item
    # -------------------------------------------------------------------------
    print(f"\n6. Python storing debrief item: '{py_debrief_text}'")

    try:
        py_fact_id = await py_client.remember(
            py_debrief_text, importance=0.7, source="hermes_debrief"
        )
        print(f"   Stored with fact ID: {py_fact_id}")
    except Exception as e:
        print(f"   Python store FAILED: {e}")
        results["py_store_mcp_recall"] = False
        await py_client.close()
        _print_summary(results)
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Step 7: Wait for subgraph indexing (40s)
    # -------------------------------------------------------------------------
    print(f"\n7. Waiting {INDEXING_WAIT}s for subgraph indexing...")
    await asyncio.sleep(INDEXING_WAIT)

    # -------------------------------------------------------------------------
    # Step 8: MCP recalls the Python-stored debrief item via ts-helper.ts
    # -------------------------------------------------------------------------
    print("\n8. MCP recalling Python-stored debrief item via ts-helper.ts...")
    print('   Query: "Redis cluster production"')

    try:
        ts_recall_output = run_ts_helper(
            ["recall", wallet, "Redis cluster production"],
            mnemonic,
            timeout=30,
        )
        print(f"   TS recall output:")
        for line in ts_recall_output.split("\n"):
            print(f"     {line}")

        # Check if the Python-stored debrief item is in the results
        ts_found_py_debrief = False
        for line in ts_recall_output.split("\n"):
            if line.startswith("DECRYPTED:"):
                decrypted_text = line.split(":", 1)[1].strip()
                if _text_matches(py_debrief_text, decrypted_text):
                    ts_found_py_debrief = True
                    break

        results["py_store_mcp_recall"] = ts_found_py_debrief
        print(
            f"   MCP found Python debrief: "
            f"{'PASS' if ts_found_py_debrief else 'FAIL'}"
        )

    except Exception as e:
        print(f"   MCP recall FAILED: {e}")
        results["py_store_mcp_recall"] = False

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    await py_client.close()

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    _print_summary(results)

    all_passed = all(results.values())
    if not all_passed:
        sys.exit(1)
    else:
        print("\nAll cross-client debrief tests PASSED!")
        sys.exit(0)


def _print_summary(results: dict) -> None:
    """Print final PASS/FAIL summary."""
    print("\n" + "=" * 60)
    print("RESULTS:")
    mcp_to_py = results.get("mcp_store_py_recall", False)
    py_to_mcp = results.get("py_store_mcp_recall", False)
    print(
        f"  MCP debrief -> store -> Python recall:  "
        f"{'PASS' if mcp_to_py else 'FAIL'}"
    )
    print(
        f"  Python debrief -> store -> MCP recall:  "
        f"{'PASS' if py_to_mcp else 'FAIL'}"
    )
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
