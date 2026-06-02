import { ProtoHeader } from "./ProtoHeader";
import { KgFlow } from "./KgFlow";

/** Mind-map / KG — React Flow ("designed nodes"). */
export function MindMapView() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-24 pt-8">
        <div className="mb-5">
          <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">How it connects</h1>
          <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
            Your topics and entities as a living graph. Drag a node, scroll or pinch to zoom, hover
            to focus a cluster.
          </p>
        </div>

        <div className="relative h-[460px] w-full overflow-hidden rounded-card bg-surface shadow-soft">
          <KgFlow />
        </div>

        <div className="mt-3 flex items-center gap-4 text-xs text-ink-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-clay" /> Topic
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full border border-hairline bg-surface" /> Entity
          </span>
        </div>
      </main>
    </div>
  );
}
