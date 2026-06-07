import {
  useState,
  useMemo,
  useCallback,
  useRef,
  useEffect,
  ChangeEvent,
} from "react";
import { Link, useNavigate } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { clsx } from "clsx";
import { useVault, useBatchDelete } from "../hooks/useVault";
import { useCrypto } from "../contexts/CryptoContext";
import { TypeBadge } from "../components/TypeBadge";
import { MEMORY_TYPES_V1, VaultItem } from "../lib/types";

const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;

type SortKey = "newest" | "oldest" | "type" | "pinned";

export function VaultPage() {
  const { keys, lock } = useCrypto();
  const navigate = useNavigate();
  const { data: items = [], isLoading, error } = useVault(keys);
  const batchDelete = useBatchDelete(keys!);

  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [pinFilter, setPinFilter] = useState<"all" | "pinned" | "unpinned">("all");
  const [olderThanDays, setOlderThanDays] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("newest");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const parentRef = useRef<HTMLDivElement>(null);

  const cutoff = olderThanDays
    ? new Date(Date.now() - olderThanDays * 24 * 60 * 60 * 1000)
    : null;

  const filtered = useMemo(() => {
    let out = items;
    if (typeFilter !== "all") out = out.filter((it) => it.type === typeFilter);
    if (pinFilter === "pinned") out = out.filter((it) => it.pinned);
    if (pinFilter === "unpinned") out = out.filter((it) => !it.pinned);
    if (cutoff) out = out.filter((it) => it.createdAt < cutoff);
    if (search.trim()) {
      const q = search.toLowerCase();
      out = out.filter(
        (it) =>
          it.claim.text.toLowerCase().includes(q) ||
          it.type.toLowerCase().includes(q),
      );
    }
    return out;
  }, [items, typeFilter, pinFilter, cutoff, search]);

  const sorted = useMemo(() => {
    const copy = [...filtered];
    switch (sortKey) {
      case "newest":
        return copy.sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
      case "oldest":
        return copy.sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime());
      case "type":
        return copy.sort((a, b) => String(a.type).localeCompare(String(b.type)));
      case "pinned":
        return copy.sort((a, b) => Number(b.pinned) - Number(a.pinned));
    }
  }, [filtered, sortKey]);

  const rowVirtualizer = useVirtualizer({
    count: sorted.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 72,
    overscan: 20,
  });

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    if (selected.size === sorted.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(sorted.map((it) => it.id)));
    }
  }, [selected.size, sorted]);

  const handleBatchDelete = useCallback(async () => {
    if (selected.size === 0) return;
    const ids = Array.from(selected);
    const ok = confirm(
      `Delete ${ids.length} claim${ids.length > 1 ? "s" : ""}? This cannot be undone.`,
    );
    if (!ok) return;
    await batchDelete.mutateAsync(ids);
    setSelected(new Set());
  }, [selected, batchDelete]);

  const handleSignOut = useCallback(() => {
    lock();
    navigate("/unlock", { replace: true });
  }, [lock, navigate]);

  // Clear selection when filter changes
  useEffect(() => {
    setSelected(new Set());
  }, [typeFilter, pinFilter, olderThanDays, search]);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-sm text-gray-500">Loading vault…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-sm text-red-600">
          Failed to load vault: {error instanceof Error ? error.message : "Unknown error"}
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-3">
        <h1 className="text-base font-semibold text-gray-900 flex-1">
          Vault
          <span className="ml-2 text-xs font-normal text-gray-400">
            {items.length.toLocaleString()} claims
          </span>
        </h1>
        <button
          onClick={handleSignOut}
          className="text-xs text-gray-500 hover:text-gray-700"
        >
          Sign out
        </button>
      </header>

      {/* Filters */}
      <div className="bg-white border-b border-gray-100 px-4 py-2 flex flex-wrap gap-2 items-center">
        <input
          type="search"
          placeholder="Search…"
          value={search}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setSearch(e.target.value)}
          className="text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500 w-40"
        />

        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="all">All types</option>
          {MEMORY_TYPES_V1.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        <select
          value={pinFilter}
          onChange={(e) =>
            setPinFilter(e.target.value as "all" | "pinned" | "unpinned")
          }
          className="text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="all">All pins</option>
          <option value="pinned">Pinned</option>
          <option value="unpinned">Unpinned</option>
        </select>

        <select
          value={olderThanDays ?? ""}
          onChange={(e) =>
            setOlderThanDays(e.target.value ? Number(e.target.value) : null)
          }
          className="text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="">Any age</option>
          <option value="7">Older than 7d</option>
          <option value="30">Older than 30d</option>
          <option value="90">Older than 90d</option>
        </select>

        <select
          value={sortKey}
          onChange={(e) => setSortKey(e.target.value as SortKey)}
          className="text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-500"
        >
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="type">By type</option>
          <option value="pinned">Pinned first</option>
        </select>

        <span className="flex-1" />

        {selected.size > 0 && (
          <button
            onClick={handleBatchDelete}
            disabled={batchDelete.isPending}
            className="text-xs bg-red-600 text-white px-3 py-1.5 rounded-md hover:bg-red-700 disabled:opacity-50"
          >
            {batchDelete.isPending
              ? "Deleting…"
              : `Delete ${selected.size} selected`}
          </button>
        )}
      </div>

      {/* Quick-filter: episode > 30d (hero workflow shortcut) */}
      <div className="bg-white border-b border-gray-100 px-4 py-1.5 flex gap-2">
        <button
          onClick={() => {
            setTypeFilter("episode");
            setOlderThanDays(THIRTY_DAYS_MS / (24 * 60 * 60 * 1000));
          }}
          className="text-xs text-gray-500 hover:text-blue-600 underline-offset-2 hover:underline"
        >
          Episodes &gt; 30d
        </button>
        <button
          onClick={() => {
            setTypeFilter("all");
            setOlderThanDays(null);
            setPinFilter("all");
            setSearch("");
          }}
          className="text-xs text-gray-400 hover:text-gray-600"
        >
          Clear filters
        </button>
        <span className="text-xs text-gray-400 ml-auto">
          {sorted.length.toLocaleString()} shown
        </span>
      </div>

      {/* Virtual list */}
      <div ref={parentRef} className="flex-1 overflow-auto">
        {sorted.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-sm text-gray-400">
            No claims match the current filters.
          </div>
        ) : (
          <div
            style={{ height: rowVirtualizer.getTotalSize() }}
            className="relative"
          >
            {/* Select-all row */}
            <div className="sticky top-0 z-10 bg-gray-50 border-b border-gray-100 px-4 py-1.5 flex items-center gap-2">
              <input
                type="checkbox"
                checked={
                  sorted.length > 0 && selected.size === sorted.length
                }
                onChange={toggleSelectAll}
                className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span className="text-xs text-gray-500">Select all visible</span>
            </div>

            {rowVirtualizer.getVirtualItems().map((virtualRow) => {
              const item = sorted[virtualRow.index]!;
              return (
                <VaultRow
                  key={item.id}
                  item={item}
                  selected={selected.has(item.id)}
                  onSelect={toggleSelect}
                  style={{
                    position: "absolute",
                    top: virtualRow.start,
                    left: 0,
                    right: 0,
                    height: virtualRow.size,
                  }}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

interface RowProps {
  item: VaultItem;
  selected: boolean;
  onSelect: (id: string) => void;
  style: React.CSSProperties;
}

function VaultRow({ item, selected, onSelect, style }: RowProps) {
  return (
    <div
      style={style}
      className={clsx(
        "flex items-start gap-3 px-4 py-3 border-b border-gray-100 hover:bg-white transition-colors",
        selected && "bg-blue-50 hover:bg-blue-50",
      )}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={() => onSelect(item.id)}
        onClick={(e) => e.stopPropagation()}
        className="mt-1 rounded border-gray-300 text-blue-600 focus:ring-blue-500 shrink-0"
      />
      <Link
        to={`/claim/${item.id}`}
        className="flex-1 min-w-0 group"
      >
        <div className="flex items-center gap-2 mb-0.5">
          <TypeBadge type={String(item.type)} />
          {item.pinned && (
            <span className="text-xs text-amber-600">📌 pinned</span>
          )}
          <span className="text-xs text-gray-400 ml-auto shrink-0">
            {item.createdAt.toLocaleDateString()}
          </span>
        </div>
        <p className="text-sm text-gray-700 line-clamp-2 group-hover:text-gray-900">
          {item.claim.text}
        </p>
      </Link>
    </div>
  );
}
