import { Routes, Route, Navigate } from "react-router-dom";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { PairPage } from "./pages/PairPage";
import { VaultPage } from "./pages/VaultPage";
import { ClaimPage } from "./pages/ClaimPage";
import { useCrypto } from "./contexts/CryptoContext";

export function App() {
  const { keys } = useCrypto();

  return (
    <Routes>
      <Route
        path="/pair"
        element={keys ? <Navigate to="/vault" replace /> : <PairPage />}
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
      <Route
        path="/"
        element={<Navigate to={keys ? "/vault" : "/pair"} replace />}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
