/**
 * Settings → "Security & recovery" sentence about where the recovery phrase
 * lives (passkey review §4.4, F10). It used to say the phrase "is never
 * stored — not on this device, not by us", directly above a list of relay
 * backups of that phrase. The sentence now follows the actual backup state.
 *
 * Facts it relies on: the browser keeps only passkey-wrapped vault keys
 * (VaultRecord), never the phrase. A relay backup is the phrase sealed under
 * a passkey PRF secret, which we can't open, but which the passkey ALONE opens
 * (no relay auth on fetch): whoever controls the account that syncs the
 * passkey, plus its PIN/passcode, can recover the phrase. The copy says so. A
 * file backup is the same sealed blob, saved wherever the user put it (the
 * SPA records only that a download happened, not that the file still exists),
 * so the file line makes no claim about relay state ("neither do we" is only
 * said when the relay was checked and holds no backup).
 */
export interface BackupState {
  /** Number of relay backups, or null while unknown (loading / unreachable). */
  relayBackups: number | null;
  /** When a backup file was last downloaded in this browser, or null. */
  fileBackupAt: string | null;
}

export const RECOVERY_COPY = {
  relay:
    "This browser doesn’t keep your recovery phrase. We keep an encrypted copy that only your passkey can open. We can’t read it, but anyone who gets into the account that syncs your passkey, along with its PIN or device passcode, could. Keep your written copy safe too: it’s the only backup that works without a passkey.",
  file:
    "This browser doesn’t keep your recovery phrase. You downloaded an encrypted backup file that only your passkey can open. Keep your written copy safe too: it’s the only backup that works without a passkey.",
  none:
    "This browser doesn’t keep your recovery phrase, and neither do we. Your written copy is your only backup, so keep it safe. You’ll need it to restore on a new device unless you add a backup below.",
  unknown:
    "This browser doesn’t keep your recovery phrase. Keep your written copy safe: it’s the only backup that works without a passkey.",
} as const;

export function recoveryPhraseStatus({ relayBackups, fileBackupAt }: BackupState): string {
  if (relayBackups !== null && relayBackups > 0) return RECOVERY_COPY.relay;
  if (fileBackupAt) return RECOVERY_COPY.file;
  if (relayBackups === 0) return RECOVERY_COPY.none;
  return RECOVERY_COPY.unknown;
}
