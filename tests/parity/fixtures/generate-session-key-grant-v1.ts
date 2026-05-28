/**
 * Generator for `session-key-grant-v1.json` — the cross-language parity
 * fixture for `SessionKeyPermissionGrant` (cred-9, spec §3.1 + §6.4).
 *
 * Locks byte-equality between three implementations of the EIP-712 grant
 * digest + master ECDSA signature + ABI-encoded install blob:
 *
 *   - Solidity validator (`contracts/contracts/SessionKeyModule.sol`,
 *     cred-5 stage 2, MERGED #272) — source-of-truth encoder.
 *   - TS / viem (`tests/parity/session-key-grant-roundtrip.ts`, this PR).
 *   - Python / `eth_account` (`python/...`, cred-8 in flight #333).
 *
 * The Solidity validator computes the digest via
 * `keccak256(abi.encodePacked(selectors))` over the `bytes4[]` selectors
 * field. Per Solidity's non-standard packed encoding rules, ARRAY ELEMENTS
 * ARE STILL PADDED to 32 bytes (only the array length prefix is dropped);
 * each `bytes4` is left-aligned in its 32-byte word. This is also what
 * the EIP-712 spec mandates for `bytes4[]`. This generator mirrors that
 * exact encoding so the digest is byte-equal across all three sibling
 * implementations.
 *
 * Inputs are fully deterministic — Anvil's first two well-known accounts,
 * the spec's example addresses, fixed nonce / issuedAt / chainId. Re-run
 * this script to regenerate the JSON fixture (no inputs change):
 *
 *   cd tests/parity && npx tsx fixtures/generate-session-key-grant-v1.ts
 */

import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  concat,
  encodeAbiParameters,
  keccak256,
  pad,
  toHex,
  type Hex,
} from 'viem';
import { privateKeyToAccount, sign } from 'viem/accounts';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Deterministic inputs — Anvil well-known accounts + spec example addresses.
// Any change here invalidates the cross-language fixture and ALL three
// sibling tests must be regenerated in lockstep.
// ---------------------------------------------------------------------------

const MASTER_PRIV =
  '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80' as Hex; // anvil[0]
const SESSION_PRIV =
  '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d' as Hex; // anvil[1]

const SMART_ACCOUNT = '0x2c0CF74B2b76110708CA431796367779e3738250' as Hex;
const DATA_EDGE = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca' as Hex;
const VERIFYING_CONTRACT =
  '0x0000000000000000000000000000000000001234' as Hex;

const EXECUTE_SELECTOR = '0xb61d27f6' as Hex;
const EXECUTE_BATCH_SELECTOR = '0x47e1da2a' as Hex;

const GRANT_VERSION = 1;
const NONCE = 1n;
const ISSUED_AT = 1748275200n;
const CHAIN_ID = 84532n; // Base Sepolia
const VALUE_MAX = 0n;

// Deterministic mock user-op hash so the session-side signature in the
// install blob is reproducible. NOT a real user-op hash — fixture only.
const MOCK_USER_OP_HASH = keccak256(
  toHex('cred-9 parity fixture v1 — mock user-op hash'),
);

// ---------------------------------------------------------------------------
// EIP-712 typehashes — must match SessionKeyModule.sol byte-for-byte.
// ---------------------------------------------------------------------------

const DOMAIN_TYPEHASH = keccak256(
  toHex(
    'EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)',
  ),
);
const SCOPE_TYPEHASH = keccak256(
  toHex('Scope(address target,bytes4[] selectors,uint256 valueMax)'),
);
const GRANT_TYPEHASH = keccak256(
  toHex(
    'SessionKeyPermissionGrant(address account,address signer,Scope scope,uint256 nonce,uint256 issuedAt)Scope(address target,bytes4[] selectors,uint256 valueMax)',
  ),
);
const DOMAIN_NAME_HASH = keccak256(toHex('TotalReclawSessionKey'));
const DOMAIN_VERSION_HASH = keccak256(toHex('1'));

// ---------------------------------------------------------------------------
// Solidity-mirrored grant digest. Non-standard `bytes4[]` packing — see
// header comment.
// ---------------------------------------------------------------------------

function grantDigest(args: {
  account: Hex;
  signer: Hex;
  target: Hex;
  selectors: Hex[];
  valueMax: bigint;
  nonce: bigint;
  issuedAt: bigint;
  chainId: bigint;
  verifyingContract: Hex;
}): Hex {
  // Solidity `abi.encodePacked(bytes4[])`: each element padded to 32 bytes,
  // left-aligned (selector occupies top 4 bytes of the word). Mirror that
  // with viem's `pad(...,{dir:'right'})` so the trailing zero-bytes match
  // the on-chain encoding.
  const selectorsPacked = concat(
    args.selectors.map((s) => pad(s, { dir: 'right', size: 32 })),
  );
  const selectorsHash = keccak256(selectorsPacked);

  const scopeHash = keccak256(
    encodeAbiParameters(
      [
        { type: 'bytes32' },
        { type: 'address' },
        { type: 'bytes32' },
        { type: 'uint256' },
      ],
      [SCOPE_TYPEHASH, args.target, selectorsHash, args.valueMax],
    ),
  );

  const structHash = keccak256(
    encodeAbiParameters(
      [
        { type: 'bytes32' },
        { type: 'address' },
        { type: 'address' },
        { type: 'bytes32' },
        { type: 'uint256' },
        { type: 'uint256' },
      ],
      [
        GRANT_TYPEHASH,
        args.account,
        args.signer,
        scopeHash,
        args.nonce,
        args.issuedAt,
      ],
    ),
  );

  const domainSeparator = keccak256(
    encodeAbiParameters(
      [
        { type: 'bytes32' },
        { type: 'bytes32' },
        { type: 'bytes32' },
        { type: 'uint256' },
        { type: 'address' },
      ],
      [
        DOMAIN_TYPEHASH,
        DOMAIN_NAME_HASH,
        DOMAIN_VERSION_HASH,
        args.chainId,
        args.verifyingContract,
      ],
    ),
  );

  return keccak256(concat(['0x1901' as Hex, domainSeparator, structHash]));
}

function signatureToHex(sig: { r: Hex; s: Hex; v: bigint | undefined; yParity?: number }): Hex {
  // viem returns r/s as 0x-prefixed 32-byte hex strings and v as the parity
  // byte (27 or 28 for legacy / EIP-191 / EIP-712 signatures). Pack to the
  // 65-byte (r, s, v) layout the SessionKeyModule._recoverEcdsa expects.
  let vByte: number;
  if (sig.v === 27n || sig.v === 28n) {
    vByte = Number(sig.v);
  } else if (sig.v === 0n || sig.v === 1n) {
    vByte = Number(sig.v) + 27;
  } else if (typeof sig.yParity === 'number') {
    vByte = sig.yParity + 27;
  } else {
    throw new Error(`unexpected sig.v=${sig.v}`);
  }
  if (vByte !== 27 && vByte !== 28) {
    throw new Error(`v normalisation failed: ${vByte}`);
  }
  const vHex = (`0x${vByte.toString(16).padStart(2, '0')}`) as Hex;
  return concat([sig.r, sig.s, vHex]);
}

async function main(): Promise<void> {
  const masterAccount = privateKeyToAccount(MASTER_PRIV);
  const sessionAccount = privateKeyToAccount(SESSION_PRIV);

  const selectors = [EXECUTE_SELECTOR, EXECUTE_BATCH_SELECTOR];

  // 1. Compute the canonical EIP-712 digest (Solidity-mirrored).
  const digest = grantDigest({
    account: SMART_ACCOUNT,
    signer: sessionAccount.address as Hex,
    target: DATA_EDGE,
    selectors,
    valueMax: VALUE_MAX,
    nonce: NONCE,
    issuedAt: ISSUED_AT,
    chainId: CHAIN_ID,
    verifyingContract: VERIFYING_CONTRACT,
  });

  // 2. Master wallet signs the digest (raw ECDSA — same primitive the
  //    on-chain validator's `ecrecover` consumes).
  const masterRaw = await sign({ hash: digest, privateKey: MASTER_PRIV });
  const masterSignature = signatureToHex(masterRaw);

  // 3. Session signer's ECDSA over the mock user-op hash (the steady-state
  //    path's signature). Goes into the install blob's trailing `bytes` arg.
  const sessionRaw = await sign({
    hash: MOCK_USER_OP_HASH,
    privateKey: SESSION_PRIV,
  });
  const sessionSignature = signatureToHex(sessionRaw);

  // 4. ABI-encode the install blob — must decode as `(PermissionGrant, bytes)`
  //    inside SessionKeyModule._decodeInstallSig.
  const permissionGrantType = {
    type: 'tuple',
    components: [
      { type: 'uint8', name: 'version' },
      { type: 'address', name: 'account' },
      { type: 'address', name: 'signer' },
      { type: 'address', name: 'target' },
      { type: 'bytes4[]', name: 'selectors' },
      { type: 'uint256', name: 'valueMax' },
      { type: 'uint256', name: 'nonce' },
      { type: 'uint256', name: 'issuedAt' },
      { type: 'uint256', name: 'chainId' },
      { type: 'address', name: 'verifyingContract' },
      { type: 'bytes', name: 'masterSignature' },
    ],
  } as const;

  const abiEncodedInstallSig = encodeAbiParameters(
    [permissionGrantType, { type: 'bytes' }],
    [
      {
        version: GRANT_VERSION,
        account: SMART_ACCOUNT,
        signer: sessionAccount.address as Hex,
        target: DATA_EDGE,
        selectors,
        valueMax: VALUE_MAX,
        nonce: NONCE,
        issuedAt: ISSUED_AT,
        chainId: CHAIN_ID,
        verifyingContract: VERIFYING_CONTRACT,
        masterSignature,
      },
      sessionSignature,
    ],
  );

  const fixture = {
    meta: {
      version: 1,
      description:
        'Cross-language parity fixture for SessionKeyPermissionGrant (cred-9, spec §3.1 + §6.4). Locks the EIP-712 digest + master ECDSA signature + ABI-encoded install blob across Solidity (SessionKeyModule.sol), TS (viem), Python (eth_account). The Solidity validator uses a non-standard `bytes4[]` encoding — see generate-session-key-grant-v1.ts for the digest implementation siblings MUST mirror. Regenerate via: npx tsx fixtures/generate-session-key-grant-v1.ts',
      grant_version: GRANT_VERSION,
      generator: 'tests/parity/fixtures/generate-session-key-grant-v1.ts',
    },
    accounts: {
      master_priv_key: MASTER_PRIV,
      master_address: masterAccount.address,
      session_priv_key: SESSION_PRIV,
      session_address: sessionAccount.address,
    },
    domain: {
      name: 'TotalReclawSessionKey',
      version: '1',
      chainId: Number(CHAIN_ID),
      verifyingContract: VERIFYING_CONTRACT,
    },
    grant: {
      version: GRANT_VERSION,
      account: SMART_ACCOUNT,
      signer: sessionAccount.address,
      scope: {
        target: DATA_EDGE,
        selectors,
        valueMax: VALUE_MAX.toString(),
      },
      nonce: NONCE.toString(),
      issuedAt: ISSUED_AT.toString(),
    },
    typehashes: {
      DOMAIN_TYPEHASH,
      SCOPE_TYPEHASH,
      GRANT_TYPEHASH,
      DOMAIN_NAME_HASH,
      DOMAIN_VERSION_HASH,
    },
    eip712_hash: digest,
    ecdsa_signature: masterSignature,
    session: {
      mock_user_op_hash: MOCK_USER_OP_HASH,
      session_ecdsa_signature: sessionSignature,
    },
    abi_encoded_install_sig: abiEncodedInstallSig,
  };

  const outPath = join(__dirname, 'session-key-grant-v1.json');
  writeFileSync(outPath, `${JSON.stringify(fixture, null, 2)}\n`, 'utf8');
  console.log(`wrote ${outPath}`);
  console.log(`  eip712_hash:          ${digest}`);
  console.log(`  master_address:       ${masterAccount.address}`);
  console.log(`  session_address:      ${sessionAccount.address}`);
  console.log(`  install_sig_bytes:    ${(abiEncodedInstallSig.length - 2) / 2}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
