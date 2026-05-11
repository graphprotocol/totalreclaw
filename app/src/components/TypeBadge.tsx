import { clsx } from "clsx";
import { TYPE_COLORS } from "../lib/types";

interface Props {
  type: string;
  className?: string;
}

export function TypeBadge({ type, className }: Props) {
  const colors = TYPE_COLORS[type] ?? "bg-slate-100 text-slate-600";
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        colors,
        className,
      )}
    >
      {type}
    </span>
  );
}
