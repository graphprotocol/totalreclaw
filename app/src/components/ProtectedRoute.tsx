import { Navigate } from "react-router-dom";
import { useCrypto } from "../contexts/CryptoContext";
import { ReactNode } from "react";

export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { keys } = useCrypto();
  if (!keys) return <Navigate to="/pair" replace />;
  return <>{children}</>;
}
