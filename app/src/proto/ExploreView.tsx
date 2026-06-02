import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { ExploreGraph } from "./ExploreGraph";
import { NodeDetail } from "./NodeDetail";
import { ExploreSessions } from "./ExploreSessions";

type Mode = "graph" | "workspace";

/** A/B of two exploration navigation models, swappable via the in-page toggle. */
export function ExploreView() {
  const [params] = useSearchParams();
  const initial: Mode = params.get("mode") === "workspace" ? "workspace" : "graph";
  const [mode, setMode] = useState<Mode>(initial);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-6xl px-4 pb-24 pt-6">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="font-display text-[1.9rem] leading-tight text-ink">Explore</h1>
            <p className="mt-1 text-sm text-ink-muted">
              Tap a topic or entity — see its facts and the sessions it touches.
            </p>
          </div>
          <div className="inline-flex rounded-pill p-1 ring-1 ring-hairline">
            {(["graph", "workspace"] as Mode[]).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={clsx(
                  "rounded-pill px-3.5 py-1.5 text-sm font-semibold transition duration-150 ease-keeper",
                  mode === m ? "bg-clay text-warm-white shadow-soft" : "text-ink-muted hover:text-ink",
                )}
              >
                {m === "graph" ? "Graph-first" : "Workspace"}
              </button>
            ))}
          </div>
        </div>

        {mode === "graph" ? (
          <div className="grid gap-4 md:grid-cols-[1fr_360px]">
            <div className="relative h-[55vh] min-h-[360px] overflow-hidden rounded-card bg-surface shadow-soft md:h-[72vh]">
              <ExploreGraph selectedId={selectedId} onSelect={setSelectedId} />
            </div>
            <div className="rounded-card bg-surface shadow-soft md:h-[72vh] md:overflow-y-auto">
              <NodeDetail nodeId={selectedId} onSelect={setSelectedId} />
              <div className="border-t border-hairline p-5">
                <ExploreSessions nodeId={selectedId} onClear={() => setSelectedId(null)} compact />
              </div>
            </div>
          </div>
        ) : (
          <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr_0.9fr]">
            <div className="relative h-[44vh] min-h-[320px] overflow-hidden rounded-card bg-surface shadow-soft lg:h-[74vh]">
              <ExploreGraph selectedId={selectedId} onSelect={setSelectedId} />
            </div>
            <div className="rounded-card bg-surface p-4 shadow-soft lg:h-[74vh] lg:overflow-y-auto">
              <ExploreSessions nodeId={selectedId} onClear={() => setSelectedId(null)} />
            </div>
            <div className="rounded-card bg-surface shadow-soft lg:h-[74vh] lg:overflow-y-auto">
              <NodeDetail nodeId={selectedId} onSelect={setSelectedId} />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
