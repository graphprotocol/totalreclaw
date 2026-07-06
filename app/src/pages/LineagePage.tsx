import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import ReactFlow, { Background, Controls, type Node, type Edge } from "reactflow";
import "reactflow/dist/style.css";
import { useCrypto } from "../contexts/CryptoContext";
import { useVaultHistory } from "../hooks/useVault";
import { buildClaimLineage } from "../lib/vault/lineage";
import { AppHeader } from "../components/AppHeader";
import { relativeDate } from "../lib/format";
import type { VaultItem } from "../lib/types";

function nodeLabel(item: VaultItem) {
  return (
    <div className="text-left">
      <div className="mb-1 flex items-center gap-2">
        <span
          className={`rounded-pill px-2 py-0.5 text-[10px] font-semibold ${
            item.isActive ? "bg-clay-tint text-clay-deep" : "bg-warm-white text-ink-muted ring-1 ring-hairline"
          }`}
        >
          {item.isActive ? "current" : "superseded"}
        </span>
        <span className="font-mono text-[10px] text-ink-muted">{relativeDate(item.createdAt)}</span>
      </div>
      <p className="font-display text-sm leading-snug text-ink">{item.claim.text}</p>
    </div>
  );
}

export function LineagePage() {
  const { id } = useParams();
  const { keys } = useCrypto();
  const { data: history = [], isLoading } = useVaultHistory(keys);

  const chain = useMemo(() => buildClaimLineage(history, id ?? ""), [history, id]);

  const nodes: Node[] = useMemo(
    () =>
      chain.map((it, i) => ({
        id: it.claim.id,
        position: { x: 0, y: i * 150 },
        data: { label: nodeLabel(it) },
        style: {
          width: 320,
          borderRadius: 16,
          border: "1px solid #E7E3DF",
          background: it.isActive ? "#FFFFFF" : "#FBFAF8",
          padding: 14,
          boxShadow: "0 1px 2px rgba(43,40,36,0.04), 0 2px 8px rgba(43,40,36,0.06)",
        },
      })),
    [chain],
  );

  const edges: Edge[] = useMemo(
    () =>
      chain.slice(1).map((it, i) => ({
        id: `${chain[i].claim.id}->${it.claim.id}`,
        source: chain[i].claim.id,
        target: it.claim.id,
        label: "replaced by",
        animated: true,
        style: { stroke: "#C16240" },
        labelStyle: { fill: "#685E57", fontSize: 11 },
      })),
    [chain],
  );

  return (
    <div className="min-h-screen bg-warm-white">
      <AppHeader />
      <main className="mx-auto max-w-2xl px-5 py-6">
        <Link to="/review" className="text-sm font-semibold text-ink-muted hover:text-ink">
          ← Review
        </Link>
        <h1 className="mt-3 font-display text-2xl font-semibold text-ink">Lineage</h1>
        <p className="mt-1 text-ink-muted">How this belief evolved over time.</p>

        {isLoading && <p className="mt-8 text-sm text-ink-muted">Tracing…</p>}

        {!isLoading && chain.length === 0 && (
          <p className="mt-8 text-sm text-ink-muted">No history found for this belief.</p>
        )}

        {chain.length > 0 && (
          <div className="mt-6 h-[70vh] rounded-card border border-hairline bg-surface">
            <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
              <Background color="#E7E3DF" gap={20} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        )}
      </main>
    </div>
  );
}
