import {
  createContext,
  useContext,
  useState,
  useCallback,
  ReactNode,
} from "react";
import { SessionKeys } from "../lib/types";

interface CryptoContextValue {
  keys: SessionKeys | null;
  setKeys: (keys: SessionKeys) => void;
  clearKeys: () => void;
}

const CryptoContext = createContext<CryptoContextValue | null>(null);

export function CryptoProvider({ children }: { children: ReactNode }) {
  const [keys, setKeysState] = useState<SessionKeys | null>(null);

  const setKeys = useCallback((k: SessionKeys) => {
    // Never persist to storage — phrase stays in RAM only
    setKeysState(k);
  }, []);

  const clearKeys = useCallback(() => {
    setKeysState(null);
  }, []);

  return (
    <CryptoContext.Provider value={{ keys, setKeys, clearKeys }}>
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
