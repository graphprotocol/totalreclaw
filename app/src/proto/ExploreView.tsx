import { useState } from "react";
import { ProtoHeader } from "./ProtoHeader";
import { ExploreGraph } from "./ExploreGraph";
import { ExplorePanel } from "./ExplorePanel";

/** Graph-first exploration: tap a node → its sessions; open a session → its memories (in place). */
export function ExploreView() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-5xl px-4 pb-24 pt-6">
        <div className="mb-4">
          <h1 className="font-display text-[1.9rem] leading-tight text-ink">Explore</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Tap a topic or entity, then open a session to read its memories.
          </p>
        </div>

        <div className="grid gap-4 md:grid-cols-[1fr_380px]">
          <div className="relative h-[55vh] min-h-[360px] overflow-hidden rounded-card bg-surface shadow-soft md:h-[74vh]">
            <ExploreGraph selectedId={selectedId} onSelect={setSelectedId} />
          </div>
          <div className="rounded-card bg-surface shadow-soft md:h-[74vh] md:overflow-y-auto">
            <ExplorePanel nodeId={selectedId} onSelectNode={setSelectedId} />
          </div>
        </div>
      </main>
    </div>
  );
}
