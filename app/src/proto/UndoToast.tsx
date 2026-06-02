import { useEffect, useState } from "react";

interface Props {
  label: string;
  seconds?: number;
  onUndo: () => void;
  onExpire: () => void;
}

/** Calm, reversible delete (DESIGN.md): 10s countdown with Undo before it commits. */
export function UndoToast({ label, seconds = 10, onUndo, onExpire }: Props) {
  const [left, setLeft] = useState(seconds);

  useEffect(() => {
    if (left <= 0) {
      onExpire();
      return;
    }
    const id = setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => clearTimeout(id);
  }, [left, onExpire]);

  return (
    <div className="animate-fade-up fixed inset-x-0 bottom-5 z-50 mx-auto flex w-fit items-center gap-3 rounded-pill bg-ink px-4 py-2.5 text-sm text-warm-white shadow-overlay">
      <span>{label}</span>
      <button
        type="button"
        onClick={onUndo}
        className="font-semibold text-clay-tint transition hover:text-warm-white"
      >
        Undo ({left}s)
      </button>
    </div>
  );
}
