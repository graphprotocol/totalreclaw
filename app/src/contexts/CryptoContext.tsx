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
import { saveVaultRecord, loadVaultRecord, hasAnyVault, clearVault } from "../lib/vault/idb";
import { registerSession } from "../lib/api";

const SERVER_URL =
  import.meta.env.VITE_SERVER_URL?.replace(/\/$/, "") ?? "https://api.totalreclaw.xyz";
// Single-chain policy: all tiers route to Gnosis mainnet (chain 100).
const DEFAULT_CHAIN_ID = 100;

export type VaultStatus = "loading" | "no-vault" | "locked" | "unlocked";

interface CryptoContextValue {
  status: VaultStatus;
  smartAccount: string | null;
  chainId: number | null;
  /** Present only when unlocked. Consumed by the read API (api.ts). */
  keys: SessionKeys | null;
  /** Generate a fresh phrase for the bootstrap backup gate (page-held, transient). */
  generatePhrase: () => string;
  /** Create a vault from a generated/imported phrase: derive → enrol passkey → wrap → persist → unlock. */
  bootstrap: (opts: { mnemonic: string; chainId?: number; userName?: string }) => Promise<void>;
  /** Passkey-first unlock: prf assert → unwrap vault + auth. */
  unlock: () => Promise<void>;
  /** Recovery fallback: re-enter phrase → re-derive (→ optionally re-enrol a passkey here). */
  unlockWithPhrase: (
    mnemonic: string,
    opts?: { reEnrol?: boolean; chainId?: number; userName?: string },
  ) => Promise<void>;
  /** A.2: transiently unwrap the master key to sign a UserOp. Throws until A.2. */
  withMasterKey: <T>(fn: (masterPriv: Uint8Array) => Promise<T>) => Promise<T>;
  /** Zero in-RAM keys; return to the locked screen. */
  lock: () => void;
  /** Remove this device's wrapped keys (on-chain data untouched). */
  forgetDevice: () => Promise<void>;
}

const CryptoContext = createContext<CryptoContextValue | null>(null);

function b64urlEncode(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

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

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const present = await hasAnyVault();
      if (cancelled) return;
      setStatus(present ? "locked" : "no-vault");
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const enterUnlocked = useCallback(
    (sk: SessionKeys, sa: string, cid: number) => {
      setKeysState(sk);
      setSmartAccount(sa);
      setChainId(cid);
      setStatus("unlocked");
    },
    [],
  );

  const generatePhrase = useCallback(() => generateRecoveryPhrase(), []);

  const bootstrap = useCallback<CryptoContextValue["bootstrap"]>(
    async ({ mnemonic, chainId: cid = DEFAULT_CHAIN_ID, userName = "TotalReclaw vault" }) => {
      if (!(await isPasskeyPrfAvailable())) throw new PrfUnsupportedError();

      const sk = await deriveSessionKeys(mnemonic, SERVER_URL, cid);
      const masterPriv = await deriveEoaPrivateKey(mnemonic); // validated 32 bytes
      let prfSecret: Uint8Array | null = null;
      let masterWrapSecret: Uint8Array | null = null;
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
        // `sk` carries no mnemonic (see SessionKeys) — safe to hold unlocked.
        enterUnlocked(sk, sk.walletAddress, cid);
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

  const withMasterKey = useCallback<CryptoContextValue["withMasterKey"]>(async () => {
    // A.2: unwrap rec.wrapped_master_key via a fresh prf assertion (credIdRef),
    // run the signer, then zero. Reads (A.1) never need the master key.
    throw new Error("Curation writes arrive in the next phase (A.2).");
  }, []);

  const lock = useCallback(() => {
    if (keys) {
      keys.authKey.fill(0);
      keys.encryptionKey.fill(0);
    }
    credIdRef.current?.fill(0);
    credIdRef.current = null;
    setKeysState(null);
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
