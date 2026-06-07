import { Routes, Route, Navigate } from "react-router-dom";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { BootstrapPage } from "./pages/BootstrapPage";
import { UnlockPage } from "./pages/UnlockPage";
import { VaultPage } from "./pages/VaultPage";
import { ClaimPage } from "./pages/ClaimPage";
import { useCrypto } from "./contexts/CryptoContext";

export function App() {
  const { status } = useCrypto();

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-warm-white">
        <div className="text-sm text-ink-muted">Opening your vault…</div>
      </div>
    );
  }

  const home =
    status === "unlocked" ? "/vault" : status === "no-vault" ? "/bootstrap" : "/unlock";

  return (
    <Routes>
      <Route
        path="/bootstrap"
        element={status === "unlocked" ? <Navigate to="/vault" replace /> : <BootstrapPage />}
      />
      <Route
        path="/unlock"
        element={status === "unlocked" ? <Navigate to="/vault" replace /> : <UnlockPage />}
      />
      <Route
        path="/vault"
        element={
          <ProtectedRoute>
            <VaultPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/claim/:id"
        element={
          <ProtectedRoute>
            <ClaimPage />
          </ProtectedRoute>
        }
      />
      <Route path="/" element={<Navigate to={home} replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
