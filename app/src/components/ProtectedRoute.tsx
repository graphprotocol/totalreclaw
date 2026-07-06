import { Navigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { ReactNode } from "react";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { status } = useCrypto();
  if (status === "loading") return null;
  if (status === "no-vault") return <Navigate to="/bootstrap" replace />;
  if (status === "locked") return <Navigate to="/unlock" replace />;
  return <>{children}</>;
}
