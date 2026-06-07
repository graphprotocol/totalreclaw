/** A quiet entity reference pill (read-only in A.1; filtering arrives later). */
export function EntityChip({ label }: { label: string }) {
  return (
    <span className="rounded-pill bg-warm-white px-2.5 py-1 font-mono text-xs text-ink-muted ring-1 ring-hairline">
      {label}
    </span>
  );
}
