/**
 * Display name for a newly enrolled passkey (passkey review §4.3 step 3, F7).
 *
 * Display metadata only: it is what the OS passkey picker and password
 * manager show next to "TotalReclaw". It plays no part in key derivation,
 * PRF output, wrapping or lookup. Every passkey used to be named
 * "TotalReclaw vault", so after a few re-enrolments the picker listed
 * several identical rows and picking the wrong one gave "no backup found".
 * Adding the short vault (Smart Account) address tells vaults apart; the
 * address is already public on-chain.
 */
export const PASSKEY_NAME_FALLBACK = "TotalReclaw vault";

export function shortAddress(address: string): string {
  const a = address.trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(a)) return a;
  return `${a.slice(0, 6)}…${a.slice(-4)}`;
}

export function passkeyUserName(smartAccount: string | null | undefined): string {
  if (!smartAccount || !/^0x[0-9a-fA-F]{40}$/.test(smartAccount.trim())) return PASSKEY_NAME_FALLBACK;
  return `${PASSKEY_NAME_FALLBACK} · ${shortAddress(smartAccount)}`;
}
