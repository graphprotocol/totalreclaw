import { Link } from "react-router-dom";
import { ProtoHeader } from "./ProtoHeader";
import { SEED_SESSIONS } from "./seed";

function Row({ to, label, hint }: { to: string; label: string; hint?: string }) {
  return (
    <Link
      to={to}
      className="flex items-center justify-between gap-3 rounded-control px-3 py-2.5 transition duration-150 ease-keeper hover:bg-warm-white focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2"
    >
      <span className="text-sm font-semibold text-ink">{label}</span>
      <span className="flex items-center gap-2 text-xs text-ink-muted">
        {hint}
        <span className="text-clay">→</span>
      </span>
    </Link>
  );
}

function Group({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-card bg-surface p-5 shadow-soft">
      <h2 className="font-display text-xl text-ink">{title}</h2>
      {desc && <p className="mt-1 text-sm text-ink-muted">{desc}</p>}
      <div className="mt-3 divide-y divide-hairline">{children}</div>
    </section>
  );
}

/** Front door for the look-and-feel prototype: links to every screen + variant. */
export function ProtoIndex() {
  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-3xl px-4 pb-24 pt-8">
        <h1 className="font-display text-[2rem] leading-tight text-ink">Prototype gallery</h1>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
          The warm "Keeper" direction — every screen, seed data, no login. Variants noted on the
          right.
        </p>

        <div className="mt-6 space-y-4">
          <Group title="Timeline" desc="Session timeline with filters + presentation toggle.">
            <Row to="/proto/timeline" label="Timeline" hint="By type" />
            <Row to="/proto/timeline?view=source" label="Timeline" hint="By source" />
          </Group>

          <Group title="Session detail" desc="Crystal + curatable memories (pin / retype / delete + undo).">
            {SEED_SESSIONS.map((s) => (
              <Row
                key={s.id}
                to={`/proto/session/${s.id}`}
                label={`${s.crystal.narrative.slice(0, 48)}…`}
              />
            ))}
            <Row to="/proto/session/s1?view=source" label="Same session, source-forward" hint="By source" />
          </Group>

          <Group title="Mind-map (KG)" desc="Two graph engines, A/B toggle in-page.">
            <Row to="/proto/kg?engine=flow" label="Designed nodes" hint="React Flow" />
            <Row to="/proto/kg?engine=force" label="Cinematic" hint="force-graph" />
          </Group>

          <Group title="Explore" desc="Two navigation models, A/B toggle in-page.">
            <Row to="/proto/explore?mode=graph" label="Graph-first" hint="recommended" />
            <Row to="/proto/explore?mode=workspace" label="Workspace" hint="denser" />
          </Group>
        </div>
      </main>
    </div>
  );
}
