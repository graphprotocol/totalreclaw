/**
 * Global Jest setup (setupFilesAfterEnv) — totalreclaw-relay#79.
 *
 * `runPairBackgroundTask` resolves the paired Smart Account (an RPC round-trip
 * to a public chain endpoint) so the relay ack can carry `wallet_address`. No
 * test may reach that RPC: the pair-ack resolver answers "unknown" here, so the
 * ack stays the plain `{ type: 'ack' }` existing tests expect. Tests of the
 * address-on-ack path pass their own `resolveWalletAddress`, and tests of the
 * resolver itself use `jest.requireActual`. `deriveSmartAccountAddress` (used
 * by managed-mode startup) is left real.
 */
jest.mock('../src/smart-account-address', () => ({
  ...jest.requireActual('../src/smart-account-address'),
  resolvePairedSmartAccount: jest.fn(async () => undefined),
}));
