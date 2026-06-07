import { Routes, Route, Navigate } from "react-router-dom";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { BootstrapPage } from "./pages/BootstrapPage";
import { UnlockPage } from "./pages/UnlockPage";
import { MemoryPage } from "./pages/MemoryPage";
import { SessionDetailPage } from "./pages/SessionDetailPage";
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
    status === "unlocked" ? "/memory" : status === "no-vault" ? "/bootstrap" : "/unlock";

  return (
    <Routes>
      <Route
        path="/bootstrap"
        element={status === "unlocked" ? <Navigate to="/memory" replace /> : <BootstrapPage />}
      />
      <Route
        path="/unlock"
        element={status === "unlocked" ? <Navigate to="/memory" replace /> : <UnlockPage />}
      />
      <Route
        path="/memory"
        element={
          <ProtectedRoute>
            <MemoryPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/memory/session/:slug"
        element={
          <ProtectedRoute>
            <SessionDetailPage />
          </ProtectedRoute>
        }
      />
      <Route path="/" element={<Navigate to={home} replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
