import { describe, it, expect } from "vitest";
import { recoveryPhraseStatus, RECOVERY_COPY } from "./recoveryStatusCopy";

describe("recoveryPhraseStatus (Settings copy matches backup state)", () => {
  it("never claims the phrase is stored nowhere when a relay backup exists", () => {
    const s = recoveryPhraseStatus({ relayBackups: 2, fileBackupAt: null });
    expect(s).toBe(RECOVERY_COPY.relay);
    expect(s).not.toMatch(/never stored|neither do we/);
  });

  it("relay backup wins over a file backup", () => {
    expect(recoveryPhraseStatus({ relayBackups: 1, fileBackupAt: "2026-11-01T00:00:00Z" })).toBe(
      RECOVERY_COPY.relay,
    );
  });

  it("file backup only", () => {
    expect(recoveryPhraseStatus({ relayBackups: 0, fileBackupAt: "2026-11-01T00:00:00Z" })).toBe(
      RECOVERY_COPY.file,
    );
  });

  it("no backups: the written phrase is the only backup", () => {
    expect(recoveryPhraseStatus({ relayBackups: 0, fileBackupAt: null })).toBe(RECOVERY_COPY.none);
  });

  it("unknown relay state makes no claim about our servers", () => {
    const s = recoveryPhraseStatus({ relayBackups: null, fileBackupAt: null });
    expect(s).toBe(RECOVERY_COPY.unknown);
    expect(s).not.toMatch(/with us|neither do we/);
  });

  it("unknown relay state but a file was downloaded still mentions the file", () => {
    const s = recoveryPhraseStatus({ relayBackups: null, fileBackupAt: "2026-11-01T00:00:00Z" });
    expect(s).toBe(RECOVERY_COPY.file);
    // The relay was not checked, so the file line must not say we hold nothing.
    expect(s).not.toMatch(/neither do we/);
  });

  it("relay copy says who else can open the backup (the passkey alone opens it)", () => {
    expect(RECOVERY_COPY.relay).toMatch(/only your passkey can open/);
    expect(RECOVERY_COPY.relay).toMatch(/account that syncs your passkey/);
    expect(RECOVERY_COPY.relay).not.toMatch(/our servers/);
  });
});
