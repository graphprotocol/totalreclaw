#!/usr/bin/env python3
"""
Cross-client E2E test: Python client <-> TypeScript MCP server.

Proves that facts stored by one client can be retrieved by the other,
using the same mnemonic/wallet against the live staging relay.

Flow:
  1. Generate fresh BIP-39 mnemonic (clean slate)
  2. Python stores a fact on-chain
  3. TypeScript decrypts and recalls it (cross-client read)
  4. TypeScript stores a fact on-chain
  5. Python decrypts and recalls it (cross-client read)

Requires: Node.js >= 22, Python venv with totalreclaw deps, mcp/ deps installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

# Add parent to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mnemonic import Mnemonic
from totalreclaw.client import TotalReclaw

RELAY_URL = os.environ.get("TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz")
MCP_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "mcp")
INDEXING_WAIT = 35  # seconds for subgraph to index on-chain data


def generate_mnemonic() -> str:
    """Generate a fresh 12-word BIP-39 mnemonic."""
    m = Mnemonic("english")
    return m.generate(128)


def run_ts(script: str, mnemonic: str, timeout: int = 120) -> str:
    """Run a TypeScript snippet in the mcp/ directory via npx tsx.

    Writes a temporary .ts file and runs it with npx tsx, which
    handles TypeScript compilation including .js -> .ts resolution.
    """
    script_path = os.path.join(MCP_DIR, "_cross_client_e2e.ts")
    try:
        with open(script_path, "w") as f:
            f.write(script)

        env = {**os.environ, "TEST_MNEMONIC": mnemonic}
        result = subprocess.run(
            ["npx", "tsx", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=MCP_DIR,
            env=env,
        )

        if result.returncode != 0:
            print(f"  TS STDERR:\n{result.stderr[:2000]}")
            raise RuntimeError(
                f"TypeScript script failed (exit {result.returncode})"
            )

        return result.stdout.strip()
    finally:
        if os.path.exists(script_path):
            os.unlink(script_path)


def build_ts_recall_script(wallet: str) -> str:
    """Build TS script that queries the subgraph and decrypts all facts for a wallet."""
    return f"""
import {{ deriveKeysFromMnemonic, decrypt, generateBlindIndices }} from './src/subgraph/crypto.js';

const mnemonic = process.env.TEST_MNEMONIC!;

async function main() {{
    const keys = deriveKeysFromMnemonic(mnemonic);
    const authKeyHex = Buffer.from(keys.authKey).toString('hex');

    // Generate trapdoors for our search query
    const trapdoors = generateBlindIndices("cross client test Python stored fact");

    const resp = await fetch("{RELAY_URL}/v1/subgraph", {{
        method: "POST",
        headers: {{
            "Content-Type": "application/json",
            "Authorization": `Bearer ${{authKeyHex}}`,
            "X-TotalReclaw-Client": "mcp-server:cross-client-e2e",
        }},
        body: JSON.stringify({{
            query: `query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {{
                blindIndexes(
                    where: {{ hash_in: $trapdoors, owner: $owner, fact_: {{ isActive: true }} }}
                    first: $first
                    orderBy: id
                    orderDirection: desc
                ) {{
                    id
                    fact {{
                        id
                        encryptedBlob
                        isActive
                    }}
                }}
            }}`,
            variables: {{
                trapdoors: trapdoors.slice(0, 20),
                owner: "{wallet}",
                first: 100,
            }},
        }}),
    }});

    if (!resp.ok) {{
        console.log(`HTTP_ERROR: ${{resp.status}} ${{await resp.text()}}`);
        process.exit(1);
    }}

    const json = await resp.json() as any;
    const entries = json?.data?.blindIndexes || [];

    // Deduplicate by fact ID
    const seen = new Set<string>();
    const facts: any[] = [];
    for (const entry of entries) {{
        const fact = entry.fact;
        if (fact && fact.isActive && !seen.has(fact.id)) {{
            seen.add(fact.id);
            facts.push(fact);
        }}
    }}

    console.log(`FOUND_COUNT: ${{facts.length}}`);

    for (const fact of facts) {{
        try {{
            let blob = fact.encryptedBlob;
            if (blob.startsWith("0x")) blob = blob.slice(2);
            const b64 = Buffer.from(blob, "hex").toString("base64");
            const text = decrypt(b64, keys.encryptionKey);
            console.log(`DECRYPTED: ${{text}}`);
        }} catch (e) {{
            console.log(`DECRYPT_ERROR: ${{(e as Error).message}}`);
        }}
    }}
}}

main().catch(e => {{ console.error(e); process.exit(1); }});
"""


def build_ts_store_script(wallet: str, fact_text: str) -> str:
    """Build TS script that stores a fact on-chain using the MCP server's store pipeline."""
    return f"""
import {{ deriveKeysFromMnemonic, encrypt, generateBlindIndices, generateContentFingerprint }} from './src/subgraph/crypto.js';
import {{ encodeFactProtobuf, submitFactOnChain, getSubgraphConfig }} from './src/subgraph/store.js';
import type {{ FactPayload }} from './src/subgraph/store.js';
import {{ randomUUID }} from 'node:crypto';

const mnemonic = process.env.TEST_MNEMONIC!;

async function main() {{
    const keys = deriveKeysFromMnemonic(mnemonic);
    const authKeyHex = Buffer.from(keys.authKey).toString('hex');
    const owner = "{wallet}";
    const factText = {json.dumps(fact_text)};

    // Encrypt
    const encryptedB64 = encrypt(factText, keys.encryptionKey);
    const encryptedHex = Buffer.from(encryptedB64, "base64").toString("hex");

    // Blind indices
    const blindIndices = generateBlindIndices(factText);

    // Content fingerprint
    const contentFp = generateContentFingerprint(factText, keys.dedupKey);

    // Build fact payload
    const factId = randomUUID();
    const timestamp = new Date().toISOString().replace(/\\.\\d{{3}}Z$/, ".000Z");

    const payload: FactPayload = {{
        id: factId,
        timestamp,
        owner,
        encryptedBlob: encryptedHex,
        blindIndices,
        decayScore: 0.8,
        source: "mcp-server:cross-client-e2e",
        contentFp,
        agentId: "cross-client-e2e",
    }};

    // Encode as protobuf
    const protobuf = encodeFactProtobuf(payload);

    // Submit on-chain via relay
    const config = {{
        relayUrl: "{RELAY_URL}",
        mnemonic,
        cachePath: "/tmp/totalreclaw-cross-client-test-cache.enc",
        chainId: 84532,
        dataEdgeAddress: "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca",
        entryPointAddress: "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
        authKeyHex,
        walletAddress: owner,
    }};

    console.log(`SUBMITTING: ${{factId}}`);
    const result = await submitFactOnChain(protobuf, config);
    console.log(`STORED: txHash=${{result.txHash}} userOpHash=${{result.userOpHash}} success=${{result.success}}`);
    console.log(`FACT_ID: ${{factId}}`);
}}

main().catch(e => {{ console.error(e.message || e); process.exit(1); }});
"""


def build_ts_export_script(wallet: str) -> str:
    """Build TS script that exports all facts for a wallet (decrypted)."""
    return f"""
import {{ deriveKeysFromMnemonic, decrypt }} from './src/subgraph/crypto.js';

const mnemonic = process.env.TEST_MNEMONIC!;

async function main() {{
    const keys = deriveKeysFromMnemonic(mnemonic);
    const authKeyHex = Buffer.from(keys.authKey).toString('hex');

    const resp = await fetch("{RELAY_URL}/v1/subgraph", {{
        method: "POST",
        headers: {{
            "Content-Type": "application/json",
            "Authorization": `Bearer ${{authKeyHex}}`,
            "X-TotalReclaw-Client": "mcp-server:cross-client-e2e",
        }},
        body: JSON.stringify({{
            query: `query ExportFacts($owner: Bytes!, $first: Int!, $skip: Int!) {{
                facts(
                    where: {{ owner: $owner, isActive: true }}
                    first: $first
                    skip: $skip
                    orderBy: timestamp
                    orderDirection: desc
                ) {{
                    id
                    encryptedBlob
                    isActive
                }}
            }}`,
            variables: {{
                owner: "{wallet}",
                first: 100,
                skip: 0,
            }},
        }}),
    }});

    if (!resp.ok) {{
        console.log(`HTTP_ERROR: ${{resp.status}} ${{await resp.text()}}`);
        process.exit(1);
    }}

    const json = await resp.json() as any;
    const facts = json?.data?.facts || [];

    console.log(`EXPORT_COUNT: ${{facts.length}}`);
    for (const fact of facts) {{
        try {{
            let blob = fact.encryptedBlob;
            if (blob.startsWith("0x")) blob = blob.slice(2);
            const b64 = Buffer.from(blob, "hex").toString("base64");
            const text = decrypt(b64, keys.encryptionKey);
            console.log(`EXPORT_FACT: ${{text}}`);
        }} catch (e) {{
            console.log(`EXPORT_ERROR: ${{(e as Error).message}}`);
        }}
    }}
}}

main().catch(e => {{ console.error(e); process.exit(1); }});
"""


async def main():
    print("=" * 60)
    print("Cross-Client E2E Test: Python <-> TypeScript")
    print("=" * 60)

    results = {}

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
    # Step 3: Python stores a fact
    # -------------------------------------------------------------------------
    ts = int(time.time())
    py_fact_text = f"Cross-client test: Python stored this fact at {ts}"
    print(f"\n3. Python storing fact: '{py_fact_text}'")
    py_fact_id = await py_client.remember(py_fact_text, importance=0.8)
    print(f"   Stored with fact ID: {py_fact_id}")

    # Wait for subgraph indexing
    print(f"\n   Waiting {INDEXING_WAIT}s for subgraph indexing...")
    await asyncio.sleep(INDEXING_WAIT)

    # -------------------------------------------------------------------------
    # Step 4: TypeScript recalls the Python-stored fact
    # -------------------------------------------------------------------------
    print("\n4. TypeScript recalling Python-stored fact...")
    ts_recall_script = build_ts_recall_script(wallet)

    try:
        ts_output = run_ts(ts_recall_script, mnemonic, timeout=30)
        print(f"   TS output:")
        for line in ts_output.split("\n"):
            print(f"     {line}")

        ts_found_py_fact = f"at {ts}" in ts_output
        results["py_store_ts_recall"] = ts_found_py_fact
        print(
            f"   TypeScript found Python fact: {'YES' if ts_found_py_fact else 'NO'}"
        )
    except Exception as e:
        print(f"   TypeScript recall FAILED: {e}")
        results["py_store_ts_recall"] = False

    # -------------------------------------------------------------------------
    # Step 5: TypeScript stores a fact
    # -------------------------------------------------------------------------
    ts2 = int(time.time())
    ts_fact_text = f"Cross-client test: TypeScript stored this fact at {ts2}"
    print(f"\n5. TypeScript storing fact: '{ts_fact_text}'")

    ts_store_script = build_ts_store_script(wallet, ts_fact_text)

    try:
        ts_store_output = run_ts(ts_store_script, mnemonic, timeout=120)
        print(f"   TS store output:")
        for line in ts_store_output.split("\n"):
            print(f"     {line}")

        ts_store_ok = "STORED:" in ts_store_output and "success=true" in ts_store_output
        if not ts_store_ok:
            print("   TypeScript store did NOT succeed.")
            results["ts_store_py_recall"] = False
        else:
            # Wait for subgraph indexing
            print(f"\n   Waiting {INDEXING_WAIT}s for subgraph indexing...")
            await asyncio.sleep(INDEXING_WAIT)

            # -----------------------------------------------------------------
            # Step 6: Python recalls the TypeScript-stored fact
            # -----------------------------------------------------------------
            print("\n6. Python recalling TypeScript-stored fact...")
            recall_results = await py_client.recall(
                f"cross client test TypeScript stored fact {ts2}"
            )
            texts = [r.text for r in recall_results]
            print(f"   Found {len(recall_results)} results")
            for r in recall_results:
                print(f"     [{r.rrf_score:.4f}] {r.text[:100]}")

            py_found_ts_fact = any(str(ts2) in t for t in texts)
            results["ts_store_py_recall"] = py_found_ts_fact
            print(
                f"   Python found TypeScript fact: {'YES' if py_found_ts_fact else 'NO'}"
            )

    except Exception as e:
        print(f"   TypeScript store FAILED: {e}")
        results["ts_store_py_recall"] = False

    # -------------------------------------------------------------------------
    # Step 7: (Bonus) TypeScript exports all facts to verify both are visible
    # -------------------------------------------------------------------------
    print("\n7. TypeScript exporting all facts (bonus verification)...")
    ts_export_script = build_ts_export_script(wallet)
    try:
        ts_export_output = run_ts(ts_export_script, mnemonic, timeout=30)
        print(f"   TS export output:")
        for line in ts_export_output.split("\n"):
            print(f"     {line}")
    except Exception as e:
        print(f"   TypeScript export failed (non-fatal): {e}")

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    await py_client.close()

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("RESULTS:")
    py_to_ts = results.get("py_store_ts_recall", False)
    ts_to_py = results.get("ts_store_py_recall", False)
    print(f"  Python -> store -> TS recall:  {'PASS' if py_to_ts else 'FAIL'}")
    print(f"  TS -> store -> Python recall:  {'PASS' if ts_to_py else 'FAIL'}")
    print("=" * 60)

    if not py_to_ts or not ts_to_py:
        sys.exit(1)
    else:
        print("\nAll cross-client tests PASSED!")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
