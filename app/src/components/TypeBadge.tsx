import { clsx } from "clsx";
import { TYPE_COLORS } from "../lib/types";

interface Props {
  type: string;
  className?: string;
}

export function TypeBadge({ type, className }: Props) {
  const colors = TYPE_COLORS[type] ?? "bg-type-claim text-type-claim-ink";
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-pill px-2.5 py-0.5 font-sans text-xs font-semibold",
        colors,
        className,
      )}
    >
      {type}
    </span>
  );
}
