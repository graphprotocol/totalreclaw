import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";

const cryptoState: { status: string } = { status: "no-vault" };

vi.mock("../contexts/CryptoContext", () => ({
  useCrypto: () => ({
    status: cryptoState.status,
    unlock: vi.fn(),
    unlockWithPhrase: vi.fn(),
    restoreFromEscrow: vi.fn(),
    restoreFromFile: vi.fn(),
    generatePhrase: vi.fn(),
    bootstrap: vi.fn(),
  }),
}));

const { UnlockPage, NoVaultFrontDoor, createVaultConfirmMessage, CREATE_AFTER_FAILED_RECOVERY_CONFIRM } =
  await import("./UnlockPage");
const { BootstrapPage, stepAfterSupportCheck, stepAfterBootstrapError } = await import("./BootstrapPage");
const { PrfUnsupportedError } = await import("../lib/auth/passkey");

const render = (el: React.ReactElement) => renderToStaticMarkup(<StaticRouter location="/">{el}</StaticRouter>);

beforeEach(() => {
  cryptoState.status = "no-vault";
});

describe("front door on a device with no vault (passkey review §4.4)", () => {
  it("leads with Create a vault (primary), then I already have one (secondary)", () => {
    const html = render(<UnlockPage />);
    const create = html.indexOf("Create a vault");
    const haveOne = html.indexOf("I already have one");
    expect(create).toBeGreaterThan(-1);
    expect(haveOne).toBeGreaterThan(create);
    // Primary button styling on create, secondary on "I already have one".
    expect(html).toMatch(/class="[^"]*bg-clay[^"]*"[^>]*>Create a vault</);
    expect(html).toMatch(/class="[^"]*bg-surface[^"]*"[^>]*>I already have one</);
  });

  it("does not greet a new user with 'Welcome back.' or the recovery choices", () => {
    const html = render(<UnlockPage />);
    expect(html).not.toContain("Welcome back.");
    expect(html).not.toContain("Recover with passkey");
    expect(html).not.toContain("Open a backup file");
  });

  it("NoVaultFrontDoor renders standalone", () => {
    const html = renderToStaticMarkup(<NoVaultFrontDoor onCreate={() => {}} onHaveOne={() => {}} />);
    expect(html).toContain("Your memory, kept safe.");
  });

  it("a device WITH a vault still shows passkey unlock", () => {
    cryptoState.status = "locked";
    const html = render(<UnlockPage />);
    expect(html).toContain("Unlock with passkey");
    expect(html).not.toContain("I already have one");
  });
});

describe("create-vault confirm", () => {
  it("first-time creation has no confirm", () => {
    expect(createVaultConfirmMessage(false)).toBeNull();
  });

  it("a softer confirm appears only after a failed recovery attempt", () => {
    expect(createVaultConfirmMessage(true)).toBe(CREATE_AFTER_FAILED_RECOVERY_CONFIRM);
    expect(CREATE_AFTER_FAILED_RECOVERY_CONFIRM).toMatch(/isn’t deleted/);
  });
});

describe("browser gate step (support matrix §5.2)", () => {
  it("a definitive no-PRF answer blocks before any phrase, whichever door the user came through", () => {
    expect(stepAfterSupportCheck("blocked", true)).toBe("unsupported");
    expect(stepAfterSupportCheck("blocked", false)).toBe("unsupported");
  });

  it("shows the advisory, not a block, when PRF support is unknown", () => {
    expect(stepAfterSupportCheck("advisory", true)).toBe("advisory");
    expect(stepAfterSupportCheck("advisory", false)).toBe("advisory");
  });

  it("goes straight to creation when the front door chose Create a vault", () => {
    expect(stepAfterSupportCheck("ok", true)).toBe("create");
  });

  it("falls back to the create-or-restore choice on a deep link", () => {
    expect(stepAfterSupportCheck("ok", false)).toBe("choose");
  });

  it("BootstrapPage starts on the browser check before showing any phrase", () => {
    const html = render(<BootstrapPage />);
    expect(html).toContain("Checking your browser…");
    expect(html).not.toContain("Write this down");
  });
});

describe("PRF missing at enrolment, after the phrase step", () => {
  it("goes to the recoverable enrol-failed screen, not the pre-gate block", () => {
    expect(stepAfterBootstrapError(new PrfUnsupportedError())).toBe("enrol-failed");
  });

  it("any other failure returns to the create-or-restore choice with the error", () => {
    expect(stepAfterBootstrapError(new Error("relay down"))).toBe("choose");
  });
});
