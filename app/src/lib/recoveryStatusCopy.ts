/**
 * Settings → "Security & recovery" sentence about where the recovery phrase
 * lives (passkey review §4.4, F10). It used to say the phrase "is never
 * stored — not on this device, not by us", directly above a list of relay
 * backups of that phrase. The sentence now follows the actual backup state.
 *
 * Facts it relies on: the browser keeps only passkey-wrapped vault keys
 * (VaultRecord), never the phrase. A relay backup is the phrase sealed under
 * a passkey PRF secret, which we can't open. A file backup is the same sealed
 * blob, saved wherever the user put it (the SPA records only that a download
 * happened, not that the file still exists).
 */
export interface BackupState {
  /** Number of relay backups, or null while unknown (loading / unreachable). */
  relayBackups: number | null;
  /** When a backup file was last downloaded in this browser, or null. */
  fileBackupAt: string | null;
}

export const RECOVERY_COPY = {
  relay:
    "This browser doesn’t keep your recovery phrase. An encrypted copy is backed up with us, locked to your passkey — we can’t read it. Keep your written copy safe too: it’s the only backup that works without a passkey.",
  file:
    "This browser doesn’t keep your recovery phrase, and neither do we. You downloaded an encrypted backup file, locked to your passkey. Keep your written copy safe too: it’s the only backup that works without a passkey.",
  none:
    "This browser doesn’t keep your recovery phrase, and neither do we. Your written copy is your only backup — keep it safe. You’ll need it to restore on a new device unless you add a passkey backup below.",
  unknown:
    "This browser doesn’t keep your recovery phrase. Keep your written copy safe: it’s the only backup that works without a passkey.",
} as const;

export function recoveryPhraseStatus({ relayBackups, fileBackupAt }: BackupState): string {
  if (relayBackups !== null && relayBackups > 0) return RECOVERY_COPY.relay;
  if (fileBackupAt) return RECOVERY_COPY.file;
  if (relayBackups === 0) return RECOVERY_COPY.none;
  return RECOVERY_COPY.unknown;
}
