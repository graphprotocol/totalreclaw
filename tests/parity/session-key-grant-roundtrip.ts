/**
 * Cross-language `SessionKeyPermissionGrant` round-trip test
 * (TypeScript / viem side — cred-9).
 *
 * Loads `fixtures/session-key-grant-v1.json` (Solidity-mirrored generator)
 * and asserts:
 *
 *   1. The EIP-712 digest computed in TS over the fixture's grant payload
 *      is byte-equal to `eip712_hash`.
 *   2. viem's raw ECDSA over that digest with the master priv key produces
 *      `ecdsa_signature` byte-for-byte.
 *   3. Recovering the signer from `eip712_hash` + `ecdsa_signature` yields
 *      the master EOA (`accounts.master_address`).
 *   4. The session signer's signature over the mock user-op hash recovers
 *      to the session EOA.
 *   5. The ABI-encoded `(PermissionGrant, bytes)` install blob (what
 *      `SessionKeyModule.validateSessionKeyUserOp` decodes on the lazy-install
 *      path) is byte-equal to `abi_encoded_install_sig`, AND round-trip
 *      decodes back to the original fields.
 *
 * The Python sibling (cred-8, in flight at #333) and the Foundry sibling
 * (extension in `contracts/test/SessionKeyModule.t.sol` shipped in this
 * PR) load the same fixture and assert the same invariants. All three
 * passing is the cross-language guarantee.
 *
 * Run:
 *   cd tests/parity && npx tsx session-key-grant-roundtrip.ts
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  concat,
  decodeAbiParameters,
  encodeAbiParameters,
  keccak256,
  pad,
  recoverAddress,
  toHex,
  type Hex,
} from 'viem';
import { privateKeyToAccount, sign } from 'viem/accounts';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// EIP-712 typehashes — must match SessionKeyModule.sol byte-for-byte.
// Duplicated from the generator on purpose: a copy-paste divergence would
// surface here as a digest mismatch rather than a silent regen drift.
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
// Fixture shape — keep in sync with generate-session-key-grant-v1.ts.
// ---------------------------------------------------------------------------

interface Fixture {
  meta: { version: number; description: string; grant_version: number };
  accounts: {
    master_priv_key: Hex;
    master_address: Hex;
    session_priv_key: Hex;
    session_address: Hex;
  };
  domain: {
    name: string;
    version: string;
    chainId: number;
    verifyingContract: Hex;
  };
  grant: {
    version: number;
    account: Hex;
    signer: Hex;
    scope: { target: Hex; selectors: Hex[]; valueMax: string };
    nonce: string;
    issuedAt: string;
  };
  typehashes: {
    DOMAIN_TYPEHASH: Hex;
    SCOPE_TYPEHASH: Hex;
    GRANT_TYPEHASH: Hex;
    DOMAIN_NAME_HASH: Hex;
    DOMAIN_VERSION_HASH: Hex;
  };
  eip712_hash: Hex;
  ecdsa_signature: Hex;
  session: { mock_user_op_hash: Hex; session_ecdsa_signature: Hex };
  abi_encoded_install_sig: Hex;
}

// ---------------------------------------------------------------------------
// Mirrors SessionKeyModule._grantDigest. Non-standard `bytes4[]` encoding —
// see fixtures/generate-session-key-grant-v1.ts header for rationale.
// ---------------------------------------------------------------------------

function grantDigest(f: Fixture): Hex {
  // Solidity `abi.encodePacked(bytes4[])`: each element padded to 32 bytes,
  // left-aligned. Mirror with right-pad.
  const selectorsPacked = concat(
    f.grant.scope.selectors.map((s) => pad(s, { dir: 'right', size: 32 })),
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
      [
        SCOPE_TYPEHASH,
        f.grant.scope.target,
        selectorsHash,
        BigInt(f.grant.scope.valueMax),
      ],
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
        f.grant.account,
        f.grant.signer,
        scopeHash,
        BigInt(f.grant.nonce),
        BigInt(f.grant.issuedAt),
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
        BigInt(f.domain.chainId),
        f.domain.verifyingContract,
      ],
    ),
  );

  return keccak256(concat(['0x1901' as Hex, domainSeparator, structHash]));
}

function signatureToHex(sig: {
  r: Hex;
  s: Hex;
  v?: bigint;
  yParity?: number;
}): Hex {
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
  const vHex = (`0x${vByte.toString(16).padStart(2, '0')}`) as Hex;
  return concat([sig.r, sig.s, vHex]);
}

// ---------------------------------------------------------------------------
// Minimal TAP-flavoured runner — same style as userop-batch-parity.test.ts
// so the parity suite reads consistently.
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function assertEqHex(actual: string, expected: string, name: string): void {
  const ok = actual.toLowerCase() === expected.toLowerCase();
  if (!ok) {
    console.log(`  actual:   ${actual}`);
    console.log(`  expected: ${expected}`);
  }
  assert(ok, name);
}

function assertEqAddr(actual: string, expected: string, name: string): void {
  // EIP-55 checksumming may differ between viem versions — compare lower.
  assertEqHex(actual.toLowerCase(), expected.toLowerCase(), name);
}

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

async function main(): Promise<void> {
  const fixture = JSON.parse(
    readFileSync(
      join(__dirname, 'fixtures', 'session-key-grant-v1.json'),
      'utf8',
    ),
  ) as Fixture;

  // 0. Sanity-check: derived addresses match the fixture's stated addresses.
  const masterAccount = privateKeyToAccount(fixture.accounts.master_priv_key);
  const sessionAccount = privateKeyToAccount(fixture.accounts.session_priv_key);
  assertEqAddr(
    masterAccount.address,
    fixture.accounts.master_address,
    'master priv key derives to fixture.accounts.master_address',
  );
  assertEqAddr(
    sessionAccount.address,
    fixture.accounts.session_address,
    'session priv key derives to fixture.accounts.session_address',
  );
  assertEqAddr(
    fixture.grant.signer,
    fixture.accounts.session_address,
    'fixture.grant.signer == fixture.accounts.session_address',
  );

  // 1. Locally computed typehashes match fixture (catches generator drift).
  assertEqHex(
    DOMAIN_TYPEHASH,
    fixture.typehashes.DOMAIN_TYPEHASH,
    'DOMAIN_TYPEHASH matches fixture',
  );
  assertEqHex(
    SCOPE_TYPEHASH,
    fixture.typehashes.SCOPE_TYPEHASH,
    'SCOPE_TYPEHASH matches fixture',
  );
  assertEqHex(
    GRANT_TYPEHASH,
    fixture.typehashes.GRANT_TYPEHASH,
    'GRANT_TYPEHASH matches fixture',
  );
  assertEqHex(
    DOMAIN_NAME_HASH,
    fixture.typehashes.DOMAIN_NAME_HASH,
    'DOMAIN_NAME_HASH matches fixture',
  );
  assertEqHex(
    DOMAIN_VERSION_HASH,
    fixture.typehashes.DOMAIN_VERSION_HASH,
    'DOMAIN_VERSION_HASH matches fixture',
  );

  // 2. Locally computed EIP-712 digest matches fixture.eip712_hash.
  const localDigest = grantDigest(fixture);
  assertEqHex(
    localDigest,
    fixture.eip712_hash,
    'TS-side grantDigest matches fixture.eip712_hash (locked Solidity encoding)',
  );

  // 3. viem-signed digest matches fixture.ecdsa_signature byte-for-byte.
  const localSigRaw = await sign({
    hash: localDigest,
    privateKey: fixture.accounts.master_priv_key,
  });
  const localSig = signatureToHex(localSigRaw);
  assertEqHex(
    localSig,
    fixture.ecdsa_signature,
    'viem master ECDSA over digest matches fixture.ecdsa_signature',
  );

  // 4. Recovered signer from fixture sig == master EOA.
  const recovered = await recoverAddress({
    hash: fixture.eip712_hash,
    signature: fixture.ecdsa_signature,
  });
  assertEqAddr(
    recovered,
    fixture.accounts.master_address,
    'recovered address from fixture.eip712_hash + fixture.ecdsa_signature == master EOA',
  );

  // 5. Session-side: signer recovers from mock user-op hash + session sig.
  const recoveredSession = await recoverAddress({
    hash: fixture.session.mock_user_op_hash,
    signature: fixture.session.session_ecdsa_signature,
  });
  assertEqAddr(
    recoveredSession,
    fixture.accounts.session_address,
    'recovered session signer from mock user-op hash matches session EOA',
  );

  // 6. ABI-encoded install blob: locally rebuilt matches fixture byte-for-byte.
  const rebuiltInstallSig = encodeAbiParameters(
    [permissionGrantType, { type: 'bytes' }],
    [
      {
        version: fixture.grant.version,
        account: fixture.grant.account,
        signer: fixture.grant.signer,
        target: fixture.grant.scope.target,
        selectors: fixture.grant.scope.selectors,
        valueMax: BigInt(fixture.grant.scope.valueMax),
        nonce: BigInt(fixture.grant.nonce),
        issuedAt: BigInt(fixture.grant.issuedAt),
        chainId: BigInt(fixture.domain.chainId),
        verifyingContract: fixture.domain.verifyingContract,
        masterSignature: fixture.ecdsa_signature,
      },
      fixture.session.session_ecdsa_signature,
    ],
  );
  assertEqHex(
    rebuiltInstallSig,
    fixture.abi_encoded_install_sig,
    'locally rebuilt abi.encode((PermissionGrant, bytes)) matches fixture.abi_encoded_install_sig',
  );

  // 7. Round-trip decode of the install blob preserves every field.
  const [decodedGrant, decodedSessionSig] = decodeAbiParameters(
    [permissionGrantType, { type: 'bytes' }],
    fixture.abi_encoded_install_sig,
  ) as [
    {
      version: number;
      account: Hex;
      signer: Hex;
      target: Hex;
      selectors: Hex[];
      valueMax: bigint;
      nonce: bigint;
      issuedAt: bigint;
      chainId: bigint;
      verifyingContract: Hex;
      masterSignature: Hex;
    },
    Hex,
  ];
  assert(
    decodedGrant.version === fixture.grant.version,
    'decoded grant.version round-trips',
  );
  assertEqAddr(
    decodedGrant.account,
    fixture.grant.account,
    'decoded grant.account round-trips',
  );
  assertEqAddr(
    decodedGrant.signer,
    fixture.grant.signer,
    'decoded grant.signer round-trips',
  );
  assertEqAddr(
    decodedGrant.target,
    fixture.grant.scope.target,
    'decoded grant.target round-trips',
  );
  assert(
    decodedGrant.selectors.length === fixture.grant.scope.selectors.length &&
      decodedGrant.selectors.every(
        (s, i) =>
          s.toLowerCase() === fixture.grant.scope.selectors[i].toLowerCase(),
      ),
    'decoded grant.selectors round-trip in order',
  );
  assert(
    decodedGrant.valueMax === BigInt(fixture.grant.scope.valueMax),
    'decoded grant.valueMax round-trips',
  );
  assert(
    decodedGrant.nonce === BigInt(fixture.grant.nonce),
    'decoded grant.nonce round-trips',
  );
  assert(
    decodedGrant.issuedAt === BigInt(fixture.grant.issuedAt),
    'decoded grant.issuedAt round-trips',
  );
  assert(
    decodedGrant.chainId === BigInt(fixture.domain.chainId),
    'decoded grant.chainId round-trips',
  );
  assertEqAddr(
    decodedGrant.verifyingContract,
    fixture.domain.verifyingContract,
    'decoded grant.verifyingContract round-trips',
  );
  assertEqHex(
    decodedGrant.masterSignature,
    fixture.ecdsa_signature,
    'decoded grant.masterSignature round-trips',
  );
  assertEqHex(
    decodedSessionSig,
    fixture.session.session_ecdsa_signature,
    'decoded trailing session ECDSA sig round-trips',
  );

  console.log(`\n# ${passed}/${passed + failed} passed`);
  if (failed > 0) {
    console.log('\nSOME TESTS FAILED');
    process.exit(1);
  } else {
    console.log('\nALL TESTS PASSED');
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
