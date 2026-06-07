import { lazy, Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { BootstrapPage } from "./pages/BootstrapPage";
import { UnlockPage } from "./pages/UnlockPage";
import { MemoryPage } from "./pages/MemoryPage";
import { SessionDetailPage } from "./pages/SessionDetailPage";
import { ReviewPage } from "./pages/ReviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { useCrypto } from "./contexts/CryptoContext";

// Lazy: keeps the reactflow graph engine out of the initial bundle.
const LineagePage = lazy(() =>
  import("./pages/LineagePage").then((m) => ({ default: m.LineagePage })),
);

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
      <Route
        path="/review"
        element={
          <ProtectedRoute>
            <ReviewPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/lineage/:id"
        element={
          <ProtectedRoute>
            <Suspense
              fallback={
                <div className="flex min-h-screen items-center justify-center bg-warm-white">
                  <div className="text-sm text-ink-muted">Loading lineage…</div>
                </div>
              }
            >
              <LineagePage />
            </Suspense>
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <SettingsPage />
          </ProtectedRoute>
        }
      />
      <Route path="/" element={<Navigate to={home} replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
