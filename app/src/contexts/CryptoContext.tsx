/**
 * L3 — phrase-safety. The single holder of decrypted key material.
 *
 * Passkey-PRF at-rest model (design 2026-06-07 §4): the mnemonic touches RAM
 * only at bootstrap, then is zeroed. The vault + auth + master keys are wrapped
 * under a WebAuthn `prf` secret in IndexedDB. Unlock re-derives the prf secret
 * via a passkey assertion and unwraps. The master key is unwrapped only
 * transiently per write (A.2).
 *
 * INVARIANTS: never log/print/transmit key bytes; best-effort zero on lock.
 */
import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useRef,
  ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { SessionKeys } from "../lib/types";
import {
  deriveSessionKeys,
  deriveEoaPrivateKey,
  generateRecoveryPhrase,
  bytesToHex,
} from "../lib/crypto";
import { isPasskeyPrfAvailable } from "../lib/auth/prf-support";
import { enrolPasskey, getPrfSecret, PrfUnsupportedError } from "../lib/auth/passkey";
import { wrapKey, unwrapKey, deriveMasterWrapSecret } from "../lib/auth/wrap";
import { runWithMasterKey } from "../lib/auth/master";
import { sealEscrow, sealedEscrowToFile, type EscrowFile } from "../lib/auth/escrow";
import { restoreFromEscrowCore, restoreFromFileCore } from "../lib/vault/recovery";
import {
  saveVaultRecord,
  loadVaultRecord,
  hasAnyVault,
  clearVault,
  refreshVaultRecordTtl,
} from "../lib/vault/idb";
import { saveSessionKeys, loadSessionKeys, clearSessionKeys } from "../lib/vault/session-storage";
import { registerSession, putEscrow } from "../lib/api";
import { b64urlEncode, b64urlDecode } from "../lib/base64url";

const SERVER_URL =
  import.meta.env.VITE_SERVER_URL?.replace(/\/$/, "") ?? "https://api.totalreclaw.xyz";
// Single-chain policy: all tiers route to Gnosis mainnet (chain 100).
const DEFAULT_CHAIN_ID = 100;

export type VaultStatus = "loading" | "no-vault" | "locked" | "unlocked";

/** What to do with the phrase at bootstrap time (spa-passkey-unlock.md §5.3). */
export type EscrowChoice = "relay" | "file" | "none";

export interface BootstrapResult {
  /** The vault this call created/restored — useful to callers reading it before a re-render lands. */
  smartAccount: string;
  /** Present when `escrow: "file"` was chosen — the caller triggers the download. */
  escrowFile?: EscrowFile;
  /** True when `escrow: "relay"` was chosen but `putEscrow` failed (non-fatal — spec §5.2). */
  escrowSaveFailed?: boolean;
}

/**
 * Idle auto-lock (§7): reset on user activity, fires `lock()` after this
 * many ms of inactivity. Not user-configurable in this phase.
 */
export const IDLE_LOCK_MS = 30 * 60 * 1000; // 30 minutes

interface CryptoContextValue {
  status: VaultStatus;
  smartAccount: string | null;
  chainId: number | null;
  /** Present only when unlocked. Consumed by the read API (api.ts). */
  keys: SessionKeys | null;
  /** Generate a fresh phrase for the bootstrap backup gate (page-held, transient). */
  generatePhrase: () => string;
  /**
   * Create a vault from a generated/imported phrase: derive → enrol passkey
   * → wrap → persist → unlock. `escrow` is the bootstrap consent choice
   * (§5.3); reuses the SAME prfSecret already in hand from enrolment — no
   * second biometric prompt (§5.2).
   */
  bootstrap: (opts: {
    mnemonic: string;
    chainId?: number;
    userName?: string;
    escrow?: EscrowChoice;
  }) => Promise<BootstrapResult>;
  /** Passkey-first unlock: prf assert → unwrap vault + auth. */
  unlock: () => Promise<void>;
  /** Recovery fallback: re-enter phrase → re-derive (→ optionally re-enrol a passkey here). */
  unlockWithPhrase: (
    mnemonic: string,
    opts?: { reEnrol?: boolean; chainId?: number; userName?: string },
  ) => Promise<void>;
  /**
   * Recover a vault on a device with no local VaultRecord, using only a
   * (typically synced) passkey. "no-escrow" is an expected outcome — this
   * passkey has no backup (spa-passkey-unlock.md §6.1).
   */
  restoreFromEscrow: () => Promise<"unlocked" | "no-escrow">;
  /**
   * Recover from a downloaded encrypted backup file (D-1 amendment). See
   * spa-passkey-unlock.md §6.4 for why "wrong-passkey" is a distinct AEAD
   * signal from the relay path's 404.
   */
  restoreFromFile: (file: File) => Promise<"unlocked" | "bad-file" | "unsupported-version" | "wrong-passkey">;
  /**
   * Settings-triggered escrow enrolment for a user who declined at
   * bootstrap (§5.3 "You can change this any time in Settings") — or who
   * wants to back up a SECOND device's passkey. The mnemonic is not held
   * anywhere after bootstrap, so the caller must have the user re-enter it;
   * this performs exactly one fresh passkey assertion against THIS
   * device's EXISTING credential (no re-enrolment) to seal it. Throws if
   * the entered phrase doesn't derive this vault's Smart Account.
   */
  enableEscrowBackup: (mnemonic: string, opts?: { label?: string }) => Promise<void>;
  /** Same re-entry + single-assertion flow as `enableEscrowBackup`, but returns the file to download instead of calling the relay (§5.4, §8). */
  sealPhraseToFile: (mnemonic: string) => Promise<EscrowFile>;
  /** A.2: transiently unwrap the master key to sign a UserOp. Throws until A.2. */
  withMasterKey: <T>(fn: (masterPriv: Uint8Array) => Promise<T>) => Promise<T>;
  /** Zero in-RAM keys; return to the locked screen. */
  lock: () => void;
  /** Remove this device's wrapped keys (on-chain data untouched). */
  forgetDevice: () => Promise<void>;
}

const CryptoContext = createContext<CryptoContextValue | null>(null);

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/** Build the read-capable SessionKeys from unwrapped bytes (no mnemonic/EOA). */
function sessionKeysFromUnwrapped(
  vaultKey: Uint8Array,
  authKey: Uint8Array,
  smartAccount: string,
  chainId: number,
): SessionKeys {
  return {
    authKey,
    encryptionKey: vaultKey,
    authKeyHex: bytesToHex(authKey),
    eoaAddress: "",
    walletAddress: smartAccount,
    chainId,
  };
}

export function CryptoProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<VaultStatus>("loading");
  const [keys, setKeysState] = useState<SessionKeys | null>(null);
  const [smartAccount, setSmartAccount] = useState<string | null>(null);
  const [chainId, setChainId] = useState<number | null>(null);
  // Credential id of this device's passkey — used to scope re-assertions (A.2).
  const credIdRef = useRef<Uint8Array | null>(null);
  const queryClient = useQueryClient();

  const enterUnlocked = useCallback(
    (sk: SessionKeys, sa: string, cid: number) => {
      setKeysState(sk);
      setSmartAccount(sa);
      setChainId(cid);
      setStatus("unlocked");
      saveSessionKeys(sk);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Stage A (#440): a same-tab refresh restores straight from
      // sessionStorage — no passkey re-prompt. Falls through to the normal
      // locked/no-vault check when absent or malformed.
      const restored = loadSessionKeys();
      if (restored) {
        if (!cancelled) enterUnlocked(restored, restored.walletAddress, restored.chainId);
        return;
      }
      const present = await hasAnyVault();
      if (cancelled) return;
      setStatus(present ? "locked" : "no-vault");
    })();
    return () => {
      cancelled = true;
    };
  }, [enterUnlocked]);

  const generatePhrase = useCallback(() => generateRecoveryPhrase(), []);

  const bootstrap = useCallback<CryptoContextValue["bootstrap"]>(
    async ({ mnemonic, chainId: cid = DEFAULT_CHAIN_ID, userName = "TotalReclaw vault", escrow = "none" }) => {
      if (!(await isPasskeyPrfAvailable())) throw new PrfUnsupportedError();

      const sk = await deriveSessionKeys(mnemonic, SERVER_URL, cid);
      const masterPriv = await deriveEoaPrivateKey(mnemonic); // validated 32 bytes
      let prfSecret: Uint8Array | null = null;
      let masterWrapSecret: Uint8Array | null = null;
      const result: BootstrapResult = { smartAccount: sk.walletAddress };
      try {
        // Register BEFORE any local persistence so a relay failure leaves no
        // half-built vault (idempotent on retry).
        await registerSession(sk);

        const userId = crypto.getRandomValues(new Uint8Array(16));
        const { credentialId } = await enrolPasskey({ userId, userName });
        prfSecret = (await getPrfSecret({ credentialId })).prfSecret;
        masterWrapSecret = deriveMasterWrapSecret(prfSecret);

        await saveVaultRecord({
          v: 1,
          smart_account: sk.walletAddress,
          chain_id: cid,
          credential_id: b64urlEncode(credentialId),
          wrapped_vault_key: wrapKey(sk.encryptionKey, prfSecret),
          wrapped_auth_key: wrapKey(sk.authKey, prfSecret),
          wrapped_master_key: wrapKey(masterPriv, masterWrapSecret),
          created_at: nowSeconds(),
        });
        credIdRef.current = credentialId;

        // Escrow consent (§5.2): reuses the SAME prfSecret already in hand
        // from enrolment above — deliberately no second biometric prompt.
        // Escrow failure is non-fatal: the vault is already fully
        // bootstrapped and usable either way.
        if (escrow === "relay" || escrow === "file") {
          const sealed = sealEscrow(mnemonic, prfSecret);
          if (escrow === "relay") {
            try {
              await putEscrow(sk, sealed, "Passkey backup");
            } catch {
              result.escrowSaveFailed = true;
            }
          } else {
            result.escrowFile = sealedEscrowToFile(sealed);
          }
        }

        // `sk` carries no mnemonic (see SessionKeys) — safe to hold unlocked.
        enterUnlocked(sk, sk.walletAddress, cid);
        return result;
      } finally {
        // Best-effort zero of every transient secret, even on error.
        masterPriv.fill(0);
        prfSecret?.fill(0);
        masterWrapSecret?.fill(0);
      }
    },
    [enterUnlocked],
  );

  const unlock = useCallback<CryptoContextValue["unlock"]>(async () => {
    const rec = await loadVaultRecord();
    if (!rec) throw new Error("No vault on this device. Restore with your recovery phrase.");
    const credId = b64urlDecode(rec.credential_id);
    const { prfSecret } = await getPrfSecret({ credentialId: credId });
    let sk: SessionKeys;
    try {
      const vaultKey = unwrapKey(rec.wrapped_vault_key, prfSecret);
      const authKey = unwrapKey(rec.wrapped_auth_key, prfSecret);
      sk = sessionKeysFromUnwrapped(vaultKey, authKey, rec.smart_account, rec.chain_id);
    } catch {
      // AEAD failure (wrong/foreign record, tamper) — nudge to recovery.
      throw new Error("Couldn’t unlock on this device. Try your recovery phrase.");
    } finally {
      prfSecret.fill(0);
    }
    credIdRef.current = credId;
    // Idempotent: ensures the relay user row exists (cheap; needed on fresh relay state).
    await registerSession(sk).catch(() => {});
    // Sliding TTL (#440): a successful unlock re-news created_at so an active
    // vault never ages out. Best-effort — a write failure must not block unlock
    // (the worst case is the record eventually expiring into re-bootstrap).
    await refreshVaultRecordTtl(rec).catch(() => {});
    enterUnlocked(sk, rec.smart_account, rec.chain_id);
  }, [enterUnlocked]);

  const unlockWithPhrase = useCallback<CryptoContextValue["unlockWithPhrase"]>(
    async (mnemonic, opts) => {
      const cid = opts?.chainId ?? DEFAULT_CHAIN_ID;
      const sk = await deriveSessionKeys(mnemonic, SERVER_URL, cid);
      await registerSession(sk);

      if (opts?.reEnrol) {
        if (!(await isPasskeyPrfAvailable())) throw new PrfUnsupportedError();
        const masterPriv = await deriveEoaPrivateKey(mnemonic);
        let prfSecret: Uint8Array | null = null;
        let masterWrapSecret: Uint8Array | null = null;
        try {
          const userId = crypto.getRandomValues(new Uint8Array(16));
          const { credentialId } = await enrolPasskey({
            userId,
            userName: opts?.userName ?? "TotalReclaw vault",
          });
          prfSecret = (await getPrfSecret({ credentialId })).prfSecret;
          masterWrapSecret = deriveMasterWrapSecret(prfSecret);
          await saveVaultRecord({
            v: 1,
            smart_account: sk.walletAddress,
            chain_id: cid,
            credential_id: b64urlEncode(credentialId),
            wrapped_vault_key: wrapKey(sk.encryptionKey, prfSecret),
            wrapped_auth_key: wrapKey(sk.authKey, prfSecret),
            wrapped_master_key: wrapKey(masterPriv, masterWrapSecret),
            created_at: nowSeconds(),
          });
          credIdRef.current = credentialId;
        } finally {
          masterPriv.fill(0);
          prfSecret?.fill(0);
          masterWrapSecret?.fill(0);
        }
      }

      enterUnlocked(sk, sk.walletAddress, cid);
    },
    [enterUnlocked],
  );

  const restoreFromEscrow = useCallback<CryptoContextValue["restoreFromEscrow"]>(async () => {
    const result = await restoreFromEscrowCore(SERVER_URL, DEFAULT_CHAIN_ID);
    if (result.status === "no-escrow") return "no-escrow";
    credIdRef.current = result.credentialId;
    enterUnlocked(result.sk, result.smartAccount, result.chainId);
    return "unlocked";
  }, [enterUnlocked]);

  const restoreFromFile = useCallback<CryptoContextValue["restoreFromFile"]>(
    async (file: File) => {
      const text = await file.text();
      const result = await restoreFromFileCore(text, SERVER_URL, DEFAULT_CHAIN_ID);
      if (result.status !== "unlocked") return result.status;
      credIdRef.current = result.credentialId;
      enterUnlocked(result.sk, result.smartAccount, result.chainId);
      return "unlocked";
    },
    [enterUnlocked],
  );

  /**
   * Shared by `enableEscrowBackup` / `sealPhraseToFile`: re-derive from a
   * freshly re-entered phrase, confirm it's THIS vault's phrase (not a typo
   * / a different vault's phrase — sealing the wrong one under this
   * passkey would be a silent, hard-to-detect mistake), assert this
   * device's EXISTING passkey (no re-enrolment), and seal. Zeroes the PRF
   * secret before returning either way.
   */
  const sealTypedPhrase = useCallback(
    async (mnemonic: string) => {
      if (!smartAccount) throw new Error("No unlocked vault.");
      const sk = await deriveSessionKeys(mnemonic, SERVER_URL, chainId ?? DEFAULT_CHAIN_ID);
      if (sk.walletAddress.toLowerCase() !== smartAccount.toLowerCase()) {
        throw new Error("That recovery phrase doesn’t match this vault.");
      }
      const rec = await loadVaultRecord(smartAccount);
      if (!rec) throw new Error("No vault on this device.");
      const credId = credIdRef.current ?? b64urlDecode(rec.credential_id);
      const { prfSecret } = await getPrfSecret({ credentialId: credId });
      try {
        credIdRef.current = credId;
        return sealEscrow(mnemonic, prfSecret);
      } finally {
        prfSecret.fill(0);
      }
    },
    [smartAccount, chainId],
  );

  const enableEscrowBackup = useCallback<CryptoContextValue["enableEscrowBackup"]>(
    async (mnemonic, opts) => {
      if (!keys) throw new Error("Not authenticated");
      const sealed = await sealTypedPhrase(mnemonic);
      await putEscrow(keys, sealed, opts?.label ?? "Passkey backup");
    },
    [keys, sealTypedPhrase],
  );

  const sealPhraseToFile = useCallback<CryptoContextValue["sealPhraseToFile"]>(
    async (mnemonic) => {
      const sealed = await sealTypedPhrase(mnemonic);
      return sealedEscrowToFile(sealed);
    },
    [sealTypedPhrase],
  );

  const withMasterKey = useCallback<CryptoContextValue["withMasterKey"]>(async (fn) => {
    // A.2: transiently unwrap the master wallet key to sign one UserOp, then
    // zero. Reuses the exact primitives unlock() uses — a fresh PRF assertion
    // (biometric/PIN) per call, unwrap `wrapped_master_key`, run the signer,
    // zero the key + PRF secret. The mnemonic is never involved (A.1 preserved);
    // the master key is NEVER derived from the seed here.
    const rec = await loadVaultRecord();
    if (!rec) {
      throw new Error("No vault on this device. Restore with your recovery phrase.");
    }
    const credId = credIdRef.current ?? b64urlDecode(rec.credential_id);
    const { prfSecret } = await getPrfSecret({ credentialId: credId });
    try {
      credIdRef.current = credId;
      const result = await runWithMasterKey(rec.wrapped_master_key, prfSecret, fn);
      // #440 sliding TTL: this path is also a successful passkey unlock (fresh
      // PRF assertion per call), and it's the only unlock a long-lived tab
      // restored from sessionStorage ever performs — without the refresh here
      // an active curator's record could age out mid-session. Best-effort,
      // mirroring unlock().
      refreshVaultRecordTtl(rec).catch(() => {});
      return result;
    } finally {
      prfSecret.fill(0);
    }
  }, []);

  const lock = useCallback(() => {
    if (keys) {
      keys.authKey.fill(0);
      keys.encryptionKey.fill(0);
    }
    credIdRef.current?.fill(0);
    credIdRef.current = null;
    setKeysState(null);
    clearSessionKeys();
    // Drop decrypted plaintext (VaultItem[]) from the react-query cache.
    queryClient.clear();
    setStatus("locked");
  }, [keys, queryClient]);

  const forgetDevice = useCallback<CryptoContextValue["forgetDevice"]>(async () => {
    const sa = smartAccount;
    lock();
    if (sa) await clearVault(sa);
    setSmartAccount(null);
    setChainId(null);
    setStatus("no-vault");
  }, [smartAccount, lock]);

  // Idle auto-lock (§7): a 30-minute inactivity timer, reset on
  // pointerdown/keydown/visibilitychange-to-visible, firing the existing
  // lock() — which already zeroes keys, clears sessionStorage, and calls
  // queryClient.clear() (drops decrypted plaintext from the query cache).
  // Only armed while unlocked; torn down otherwise so a locked/no-vault tab
  // never runs a timer with nothing to lock.
  useEffect(() => {
    if (status !== "unlocked") return;
    let timer: ReturnType<typeof setTimeout>;
    const reset = () => {
      clearTimeout(timer);
      timer = setTimeout(lock, IDLE_LOCK_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") reset();
    };
    reset();
    window.addEventListener("pointerdown", reset);
    window.addEventListener("keydown", reset);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pointerdown", reset);
      window.removeEventListener("keydown", reset);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [status, lock]);

  return (
    <CryptoContext.Provider
      value={{
        status,
        smartAccount,
        chainId,
        keys,
        generatePhrase,
        bootstrap,
        unlock,
        unlockWithPhrase,
        restoreFromEscrow,
        restoreFromFile,
        enableEscrowBackup,
        sealPhraseToFile,
        withMasterKey,
        lock,
        forgetDevice,
      }}
    >
      {children}
    </CryptoContext.Provider>
  );
}

export function useCrypto(): CryptoContextValue {
  const ctx = useContext(CryptoContext);
  if (!ctx) throw new Error("useCrypto must be used within CryptoProvider");
  return ctx;
}

export function useRequiredKeys(): SessionKeys {
  const { keys } = useCrypto();
  if (!keys) throw new Error("Not authenticated");
  return keys;
}
