import { Routes, Route, Navigate } from "react-router-dom";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { PairPage } from "./pages/PairPage";
import { VaultPage } from "./pages/VaultPage";
import { ClaimPage } from "./pages/ClaimPage";
import { TimelineView } from "./proto/TimelineView";
import { ReviewView } from "./proto/ReviewView";
import { LineageView } from "./proto/LineageView";
import { MindMapView } from "./proto/MindMapView";
import { ExploreView } from "./proto/ExploreView";
import { SessionDetailView } from "./proto/SessionDetailView";
import { ProtoIndex } from "./proto/ProtoIndex";
import { ProtoPair } from "./proto/ProtoPair";
import { ProtoOnboarding } from "./proto/ProtoOnboarding";
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
      {/* Look-and-feel prototype routes (seed data, no auth) — see /DESIGN.md */}
      <Route path="/proto" element={<ProtoIndex />} />
      <Route path="/proto/pair" element={<ProtoPair />} />
      <Route path="/proto/onboarding" element={<ProtoOnboarding />} />
      <Route path="/proto/timeline" element={<TimelineView />} />
      <Route path="/proto/review" element={<ReviewView />} />
      <Route path="/proto/lineage" element={<LineageView />} />
      <Route path="/proto/lineage/:id" element={<LineageView />} />
      <Route path="/proto/kg" element={<MindMapView />} />
      <Route path="/proto/explore" element={<ExploreView />} />
      <Route path="/proto/session/:id" element={<SessionDetailView />} />
      <Route
        path="/"
        element={<Navigate to={keys ? "/vault" : "/pair"} replace />}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
