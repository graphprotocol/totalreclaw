/** An entity reference pill. When `onClick` is given it filters the timeline. */
export function EntityChip({ label, onClick }: { label: string; onClick?: (label: string) => void }) {
  const cls =
    "rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline";
  if (!onClick) return <span className={cls}>{label}</span>;
  return (
    <button
      type="button"
      onClick={() => onClick(label)}
      className={`${cls} transition hover:text-ink hover:ring-clay/40`}
    >
      {label}
    </button>
  );
}
