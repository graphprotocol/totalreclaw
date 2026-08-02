import { Navigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { ReactNode } from "react";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { status } = useCrypto();
  if (status === "loading") return null;
  // Both "no-vault" (no local VaultRecord at all) and "locked" (a record
  // exists but needs a passkey assertion) land on /unlock — its four-way
  // choice (spa-passkey-unlock.md §6.3) is the right destination for a
  // no-vault device too: it offers escrow/file recovery before "create a
  // new vault", which App.tsx's top-level redirect also does. Matching
  // that here (rather than going straight to /bootstrap) matters because a
  // session can transition to "no-vault" mid-visit — e.g. storage cleared,
  // or forgetDevice() — while sitting on a protected route.
  if (status === "no-vault" || status === "locked") return <Navigate to="/unlock" replace />;
  return <>{children}</>;
}
