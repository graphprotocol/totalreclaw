/**
 * Local-only record of "a file backup was downloaded" (spa-passkey-unlock.md
 * §5.4, §8): "Show whether each backup type is in place... The file line is
 * a record of an action taken, not a live status — the SPA cannot know
 * whether the file still exists, and must not claim to."
 *
 * Stores nothing secret — just a per-vault timestamp in localStorage, purely
 * to render "Encrypted file downloaded 2 Aug" in Settings. Losing this
 * record (cleared storage, new browser) does not affect recovery in any
 * way — it's UI memory, not a security control.
 */
const PREFIX = "totalreclaw-spa:file-backup-at:";

function key(smartAccount: string): string {
  return PREFIX + smartAccount.toLowerCase();
}

export function recordFileBackup(smartAccount: string, at: Date = new Date()): void {
  try {
    localStorage.setItem(key(smartAccount), at.toISOString());
  } catch {
    // Storage unavailable — non-fatal, just means the Settings line won't show.
  }
}

/** ISO-8601 timestamp of the last recorded file download, or null. */
export function getLastFileBackupAt(smartAccount: string): string | null {
  try {
    return localStorage.getItem(key(smartAccount));
  } catch {
    return null;
  }
}
