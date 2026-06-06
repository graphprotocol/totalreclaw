import { useMemo, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { clsx } from "clsx";
import { ProtoHeader } from "./ProtoHeader";
import { count } from "./format";
import {
  REVIEW_ITEMS,
  BACKED_LABEL,
  type ReviewItem,
  type ConflictItem,
  type StaleItem,
  type ChangedItem,
  type SecretItem,
  type ReviewBase,
} from "./review-data";

/* ---------- shared chrome ---------- */

const BACKED_DOT: Record<ReviewBase["backed"], string> = {
  shipped: "bg-emerald",
  "needs-plumbing": "bg-amber",
  "needs-backend": "bg-clay",
};

type ChipTone = "urgent" | "neutral" | "safe";
const CHIP_TONE: Record<ChipTone, string> = {
  urgent: "bg-clay-tint text-clay-deep",
  neutral: "border border-hairline text-ink-muted",
  safe: "bg-type-summary text-type-summary-ink",
};

function Pill({
  children,
  tone = "ghost",
  onClick,
}: {
  children: React.ReactNode;
  tone?: "solid" | "ghost";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-pill px-3.5 py-1.5 font-sans text-sm font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2 active:scale-[0.97]",
        tone === "solid"
          ? "bg-clay text-warm-white shadow-soft hover:-translate-y-px hover:bg-clay-deep hover:shadow-raised"
          : "border border-hairline bg-warm-white text-ink hover:border-ink-muted/40 hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

function Kind({ icon, label, tone }: { icon: React.ReactNode; label: string; tone: ChipTone }) {
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-pill px-2.5 py-1 text-xs font-semibold", CHIP_TONE[tone])}>
      {icon}
      {label}
    </span>
  );
}

function Backed({ backed }: { backed: ReviewBase["backed"] }) {
  return (
    <p className="mt-3.5 flex items-center gap-1.5 border-t border-hairline pt-2.5 text-[0.7rem] italic text-ink-muted/80">
      <span className={clsx("h-1.5 w-1.5 rounded-full", BACKED_DOT[backed])} aria-hidden />
      {BACKED_LABEL[backed]}
    </p>
  );
}

function Card({ children, style }: { children: React.ReactNode; style?: CSSProperties }) {
  return (
    <article style={style} className="animate-fade-up rounded-card bg-surface p-5 shadow-soft">
      {children}
    </article>
  );
}

/* ---------- icons (stroke) ---------- */

const I = (d: React.ReactNode) => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {d}
  </svg>
);
const iconConflict = I(<><path d="M12 9v4" /><path d="M12 17h.01" /><path d="m3.6 18 7-13a1.6 1.6 0 0 1 2.8 0l7 13A1.6 1.6 0 0 1 21 20H4a1.6 1.6 0 0 1-1.4-2Z" /></>);
const iconStale = I(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>);
const iconChanged = I(<><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 4v4h4" /></>);
const iconSecret = I(<><rect x="4" y="10" width="16" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></>);

/* ---------- per-kind cards ---------- */

function ConflictCard({ item, style, onResolve }: { item: ConflictItem; style?: CSSProperties; onResolve: () => void }) {
  return (
    <Card style={style}>
      <Kind icon={iconConflict} label="Conflict" tone="urgent" />
      <p className="mt-3 font-sans text-sm text-ink-muted">Two things I believe disagree. Which is true?</p>
      <div className="mt-3 grid gap-2">
        {[item.a, item.b].map((c, i) => (
          <div
            key={i}
            className={clsx(
              "rounded-control border bg-warm-white px-3.5 py-3",
              c.pinned ? "border-clay/40" : "border-hairline",
            )}
          >
            <p className="font-display text-[1.02rem] leading-snug text-ink">{c.text}</p>
            <p className="mt-1 flex items-center gap-1.5 text-xs text-ink-muted">
              {c.pinned && <span className="font-semibold text-clay-deep">pinned ·</span>}
              {c.source === "user" ? "from you" : "your agent inferred this"} · {c.age}
            </p>
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Pill tone="solid" onClick={onResolve}>Keep the newer one</Pill>
        <Pill onClick={onResolve}>Keep both</Pill>
        <Pill onClick={onResolve}>Neither</Pill>
        <Link
          to={`/proto/lineage/${item.thread}`}
          className="ml-auto text-sm font-semibold text-clay-deep transition hover:underline"
        >
          See the full history →
        </Link>
      </div>
      <Backed backed={item.backed} />
    </Card>
  );
}

function StaleCard({ item, style, onResolve }: { item: StaleItem; style?: CSSProperties; onResolve: () => void }) {
  return (
    <Card style={style}>
      <Kind icon={iconStale} label="Still true?" tone="neutral" />
      <p className="mt-3 font-display text-[1.15rem] leading-snug text-ink">{item.text}</p>
      <p className="mt-1.5 text-xs text-ink-muted">You told me this {item.age}. I haven't heard since.</p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Pill tone="solid" onClick={onResolve}>Still true</Pill>
        <Pill onClick={onResolve}>Update</Pill>
        <Pill onClick={onResolve}>Forget it</Pill>
      </div>
      <Backed backed={item.backed} />
    </Card>
  );
}

function ChangedCard({ item, style, onResolve }: { item: ChangedItem; style?: CSSProperties; onResolve: () => void }) {
  return (
    <Card style={style}>
      <Kind icon={iconChanged} label="I changed my mind" tone="neutral" />
      <p className="mt-3 font-display text-[1.15rem] leading-snug text-ink">{item.summary}</p>
      <div className="mt-3 grid gap-1.5 text-sm">
        <p className="text-ink-muted line-through decoration-ink-muted/40">{item.from}</p>
        <p className="text-ink">{item.to}</p>
      </div>
      <p className="mt-2 text-xs text-ink-muted">{item.age} · old note kept in history</p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Pill onClick={onResolve}>Got it</Pill>
        <Pill onClick={onResolve}>Undo this change</Pill>
        <Link
          to={`/proto/lineage/${item.thread}`}
          className="ml-auto text-sm font-semibold text-clay-deep transition hover:underline"
        >
          See why →
        </Link>
      </div>
      <Backed backed={item.backed} />
    </Card>
  );
}

function SecretCard({ item, style, onResolve }: { item: SecretItem; style?: CSSProperties; onResolve: () => void }) {
  return (
    <Card style={style}>
      <Kind icon={iconSecret} label="Kept safe" tone="safe" />
      <p className="mt-3 font-display text-[1.15rem] leading-snug text-ink">
        I caught a {item.label} and locked it away.
      </p>
      <p className="mt-1.5 text-sm text-ink-muted">It was {item.context}.</p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Pill onClick={onResolve}>Good, keep it hidden</Pill>
        <Pill onClick={onResolve}>Reveal once</Pill>
        <Pill onClick={onResolve}>Delete it</Pill>
      </div>
      <Backed backed={item.backed} />
    </Card>
  );
}

function Row({ item, style, onResolve }: { item: ReviewItem; style?: CSSProperties; onResolve: () => void }) {
  switch (item.kind) {
    case "conflict":
      return <ConflictCard item={item} style={style} onResolve={onResolve} />;
    case "stale":
      return <StaleCard item={item} style={style} onResolve={onResolve} />;
    case "changed":
      return <ChangedCard item={item} style={style} onResolve={onResolve} />;
    case "secret":
      return <SecretCard item={item} style={style} onResolve={onResolve} />;
  }
}

/* ---------- view ---------- */

// "Needs you" = a human decision/confirmation. "Handled for you" = the Keeper
// already acted; you're just being kept in the loop.
const NEEDS_YOU: ReviewItem["kind"][] = ["conflict", "stale"];

function Section({
  title,
  items,
  leaving,
  resolve,
}: {
  title: string;
  items: ReviewItem[];
  leaving: string | null;
  resolve: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section>
      <h2 className="mb-3 flex items-baseline gap-2 font-display text-lg text-ink">
        {title}
        <span className="font-sans text-sm font-normal text-ink-muted">{items.length}</span>
      </h2>
      <div className="grid gap-3">
        {items.map((i, idx) => (
          <div
            key={i.id}
            className={clsx(
              "transition-all duration-200 ease-keeper",
              leaving === i.id && "scale-[0.98] opacity-0",
            )}
          >
            <Row item={i} style={{ animationDelay: `${idx * 55}ms` }} onResolve={() => resolve(i.id)} />
          </div>
        ))}
      </div>
    </section>
  );
}

export function ReviewView() {
  const [done, setDone] = useState<Record<string, true>>({});
  const [leaving, setLeaving] = useState<string | null>(null);

  const resolve = (id: string) => {
    setLeaving(id);
    setTimeout(() => {
      setDone((d) => ({ ...d, [id]: true }));
      setLeaving(null);
    }, 200);
  };

  const visible = useMemo(() => REVIEW_ITEMS.filter((i) => !done[i.id]), [done]);
  const needsYou = visible.filter((i) => NEEDS_YOU.includes(i.kind));
  const handled = visible.filter((i) => !NEEDS_YOU.includes(i.kind));

  return (
    <div className="min-h-screen bg-warm-white">
      <ProtoHeader />
      <main className="animate-page-in mx-auto w-full max-w-2xl px-4 pb-28 pt-8">
        <header className="mb-7">
          <h1 className="text-balance font-display text-[2rem] leading-tight text-ink">Your review</h1>
          <p className="mt-1.5 max-w-prose text-sm leading-relaxed text-ink-muted">
            I keep watch over your memory and bring you what needs a human.
            {visible.length > 0 && <> {count(needsYou.length, "thing")} to decide today.</>}
          </p>
        </header>

        {visible.length === 0 ? (
          <div className="animate-fade-up rounded-card bg-surface px-6 py-14 text-center shadow-soft">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-clay-tint">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#A54B2E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </div>
            <p className="font-display text-xl text-ink">All clear</p>
            <p className="mx-auto mt-1.5 max-w-xs text-sm leading-relaxed text-ink-muted">
              Nothing needs you right now. I'll keep watch and surface anything worth a look.
            </p>
            <Link
              to="/proto/timeline"
              className="mt-5 inline-block text-sm font-semibold text-clay-deep transition hover:underline"
            >
              Browse your memory →
            </Link>
          </div>
        ) : (
          <div className="grid gap-8">
            <Section title="Needs you" items={needsYou} leaving={leaving} resolve={resolve} />
            <Section title="Handled for you" items={handled} leaving={leaving} resolve={resolve} />
          </div>
        )}
      </main>
    </div>
  );
}
