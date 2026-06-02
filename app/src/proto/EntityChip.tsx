import { Link } from "react-router-dom";

const CLS =
  "rounded-pill border border-hairline bg-surface px-2.5 py-1 text-xs font-semibold text-ink-muted transition duration-150 ease-keeper hover:border-clay/40 hover:bg-clay-tint hover:text-clay-deep focus:outline-none focus-visible:ring-2 focus-visible:ring-clay focus-visible:ring-offset-2";

interface Props {
  label: string;
  /** When provided, the chip becomes a filter button instead of a link to the mind-map. */
  onClick?: (label: string) => void;
}

export function EntityChip({ label, onClick }: Props) {
  if (onClick) {
    return (
      <button type="button" className={CLS} onClick={() => onClick(label)}>
        {label}
      </button>
    );
  }
  return (
    <Link to="/proto/kg" className={CLS}>
      {label}
    </Link>
  );
}
