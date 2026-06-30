import { clsx } from "clsx";

/** Pill segmented control — the Memory mode switcher + sub-toggles. Ported from
 *  the Keeper prototype (app/src/proto/MemoryRedesign.tsx). */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = "md",
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  size?: "md" | "sm";
  ariaLabel?: string;
}) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={clsx(
        "inline-flex items-center gap-0.5 rounded-pill border border-hairline bg-surface p-0.5",
        size === "sm" ? "text-xs" : "text-sm",
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={clsx(
              "rounded-pill font-semibold transition duration-150 ease-keeper focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-1",
              size === "sm" ? "px-3 py-1" : "px-4 py-1.5",
              active ? "bg-clay text-warm-white shadow-soft" : "text-ink-muted hover:text-ink",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
