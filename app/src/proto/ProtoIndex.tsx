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
        <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Prototype gallery</h1>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
          The warm "Keeper" direction — every screen, seed data, no login. Variants noted on the
          right.
        </p>

        <div className="mt-6 space-y-4">
          <Group title="Review — the Keeper watches your memory" desc="Memory health: conflicts, stale facts, what changed, secrets caught. The reason to open the app.">
            <Row to="/proto/review" label="Your review" hint="hero · needs-you feed" />
            <Row to="/proto/lineage/where-pedro-works" label="Lineage — why a belief changed" hint="conflict thread" />
            <Row to="/proto/lineage/july-trip" label="Lineage — a plan that evolved" hint="supersede thread" />
          </Group>

          <Group title="Memory — see everything, clearly" desc="The trust foundation: session timeline + keyword filter + Crystal headlines + curation.">
            <Row to="/proto/timeline" label="Timeline" hint="filter + by source" />
            {SEED_SESSIONS.map((s) => (
              <Row
                key={s.id}
                to={`/proto/session/${s.id}`}
                label={`${s.crystal.narrative.slice(0, 44)}…`}
              />
            ))}
          </Group>

          <Group title="Cold start — day-1, empty vault" desc="An empty memory vault is an activation surface, not a dead end. The on-ramp that fills it.">
            <Row to="/proto/timeline?empty" label="Memory — empty" hint="on-ramp + glimpse" />
            <Row to="/proto/timeline?first" label="Memory — first memory (aha)" hint="confirm / correct" />
            <Row to="/proto/timeline?warming" label="Memory — warming up" hint="taking shape" />
            <Row to="/proto/review?empty" label="Review — fresh (never reviewed)" hint="teaches the cards" />
            <Row to="/proto/pair-agent" label="Pair an agent" hint="stub · not the real auth" />
            <Row to="/proto/import" label="Import guide (ChatGPT / Gemini / Claude)" hint="SPA shows how · agent does it" />
          </Group>

          <Group title="First run & auth" desc="Non-functional — every button just continues to the vault.">
            <Row to="/proto/onboarding" label="Create a vault (onboarding)" hint="generate + back up" />
            <Row to="/proto/pair" label="Unlock" hint="passkey + recovery" />
          </Group>

          <Group title="Earlier explorations" desc="Demoted from primary nav — the global graph is a glance, not a workspace. Lineage replaced it.">
            <Row to="/proto/kg" label="Mind-map (global graph)" hint="ambient only" />
            <Row to="/proto/explore" label="Explore (graph-first drill)" hint="superseded by Review" />
          </Group>
        </div>
      </main>
    </div>
  );
}
