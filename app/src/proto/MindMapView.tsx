import { lazy, Suspense, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";

// Lazy + isolated: picking one engine never loads the other's bundle.
const KgFlow = lazy(() => import("./KgFlow").then((m) => ({ default: m.KgFlow })));
const KgForceGraph = lazy(() =>
  import("./KgForceGraph").then((m) => ({ default: m.KgForceGraph })),
);

type Engine = "flow" | "force";

/** Prototype KG explorer — A/B between React Flow (designed nodes) and a canvas force graph. */
export function MindMapView() {
  const [params] = useSearchParams();
  const initial: Engine =
    params.get("engine") === "force" || params.get("engine") === "reagraph" ? "force" : "flow";
  const [engine, setEngine] = useState<Engine>(initial);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-5">
          <h1 className="font-display text-[2rem] leading-tight text-ink">How it connects</h1>
          <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
            Your topics and entities, as a living graph. Drag a node, scroll or pinch to zoom,
            hover to focus a cluster.
          </p>
        </div>

        <div className="mb-3 inline-flex rounded-pill p-1 ring-1 ring-hairline">
          {(["flow", "force"] as Engine[]).map((e) => (
            <button
              key={e}
              onClick={() => setEngine(e)}
              className={clsx(
                "rounded-pill px-3.5 py-1.5 text-sm font-semibold transition duration-150 ease-keeper",
                engine === e
                  ? "bg-clay text-warm-white shadow-soft"
                  : "text-ink-muted hover:text-ink",
              )}
            >
              {e === "flow" ? "Designed nodes" : "Cinematic"}
            </button>
          ))}
        </div>

        <div className="relative h-[460px] w-full overflow-hidden rounded-card bg-surface shadow-soft">
          <Suspense
            fallback={
              <div className="flex h-full items-center justify-center text-sm text-ink-muted">
                Loading graph…
              </div>
            }
          >
            {engine === "flow" ? <KgFlow /> : <KgForceGraph />}
          </Suspense>
        </div>

        <div className="mt-3 flex items-center gap-4 text-xs text-ink-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-clay" /> Topic
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full border border-hairline bg-surface" /> Entity
          </span>
          <span className="ml-auto font-mono">
            {engine === "flow" ? "React Flow + d3-force" : "react-force-graph · canvas"}
          </span>
        </div>
      </main>
    </div>
  );
}
