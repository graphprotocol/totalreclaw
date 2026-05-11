import { useState, useCallback } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { clsx } from "clsx";
import { useVault, useDeleteFact, useUpdateClaim } from "../hooks/useVault";
import { useCrypto } from "../contexts/CryptoContext";
import { TypeBadge } from "../components/TypeBadge";
import { MEMORY_TYPES_V1, MemoryTypeV1, MemoryClaimV1 } from "../lib/types";

export function ClaimPage() {
  const { id } = useParams<{ id: string }>();
  const { keys } = useCrypto();
  const navigate = useNavigate();

  const { data: items = [], isLoading } = useVault(keys);
  const deleteFact = useDeleteFact(keys!);
  const updateClaim = useUpdateClaim(keys!);

  const item = items.find((it) => it.id === id);

  const [selectedType, setSelectedType] = useState<MemoryTypeV1 | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const handlePin = useCallback(async () => {
    if (!item) return;
    const updated: MemoryClaimV1 = {
      ...item.claim,
      pin_status: item.pinned ? "unpinned" : "pinned",
    };
    await updateClaim.mutateAsync({ item, updatedClaim: updated });
  }, [item, updateClaim]);

  const handleRetype = useCallback(async () => {
    if (!item || !selectedType || selectedType === item.type) return;
    const updated: MemoryClaimV1 = {
      ...item.claim,
      type: selectedType,
      tags: [
        selectedType,
        ...(item.claim.tags?.slice(1) ?? []),
      ],
    };
    await updateClaim.mutateAsync({ item, updatedClaim: updated });
    setSelectedType(null);
  }, [item, selectedType, updateClaim]);

  const handleDelete = useCallback(async () => {
    if (!item) return;
    await deleteFact.mutateAsync(item.id);
    navigate("/vault", { replace: true });
  }, [item, deleteFact, navigate]);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-sm text-gray-500">Loading…</div>
      </div>
    );
  }

  if (!item) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-gray-50 gap-3">
        <p className="text-sm text-gray-500">Claim not found.</p>
        <Link to="/vault" className="text-sm text-blue-600 hover:underline">
          ← Back to vault
        </Link>
      </div>
    );
  }

  const typeChanged = selectedType && selectedType !== item.type;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-3">
        <Link
          to="/vault"
          className="text-sm text-gray-500 hover:text-gray-700"
        >
          ← Vault
        </Link>
        <span className="flex-1" />
        <TypeBadge type={String(item.type)} />
        {item.pinned && (
          <span className="text-xs text-amber-600">📌</span>
        )}
      </header>

      <div className="max-w-2xl mx-auto px-4 py-6 space-y-5">
        {/* Claim text */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <p className="text-gray-800 leading-relaxed whitespace-pre-wrap">
            {item.claim.text}
          </p>
          <div className="mt-3 pt-3 border-t border-gray-100 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400">
            <span>Created {item.createdAt.toLocaleString()}</span>
            {item.claim.source && (
              <span>Source: {item.claim.source}</span>
            )}
            {item.claim.importance !== undefined && (
              <span>Importance: {(item.claim.importance * 10).toFixed(1)}/10</span>
            )}
            {item.claim.scope && (
              <span>Scope: {item.claim.scope}</span>
            )}
          </div>
        </div>

        {/* Reasoning (if any) */}
        {item.claim.reasoning && (
          <div className="bg-amber-50 border border-amber-100 rounded-xl p-4">
            <p className="text-xs font-medium text-amber-700 mb-1">Reasoning</p>
            <p className="text-sm text-amber-900">{item.claim.reasoning}</p>
          </div>
        )}

        {/* Actions */}
        <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
          <h2 className="text-sm font-medium text-gray-700">Actions</h2>

          {/* Retype */}
          <div>
            <label className="text-xs text-gray-500 block mb-1.5">
              Retype claim
            </label>
            <div className="flex gap-2 flex-wrap">
              {MEMORY_TYPES_V1.map((t) => (
                <button
                  key={t}
                  onClick={() =>
                    setSelectedType(t === item.type ? null : t)
                  }
                  className={clsx(
                    "px-3 py-1 rounded-full text-xs font-medium transition-colors border",
                    t === (selectedType ?? item.type)
                      ? "bg-blue-600 text-white border-blue-600"
                      : "bg-gray-50 text-gray-600 border-gray-200 hover:border-blue-400",
                  )}
                >
                  {t}
                </button>
              ))}
            </div>
            {typeChanged && (
              <button
                onClick={handleRetype}
                disabled={updateClaim.isPending}
                className="mt-3 text-sm bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                {updateClaim.isPending
                  ? "Saving…"
                  : `Change to "${selectedType}"`}
              </button>
            )}
            {updateClaim.isSuccess && !updateClaim.isPending && (
              <p className="mt-2 text-xs text-green-600">Type updated.</p>
            )}
            {updateClaim.isError && (
              <p className="mt-2 text-xs text-red-600">
                {updateClaim.error instanceof Error
                  ? updateClaim.error.message
                  : "Update failed"}
              </p>
            )}
          </div>

          {/* Pin */}
          <div className="flex items-center justify-between pt-2 border-t border-gray-100">
            <div>
              <p className="text-sm font-medium text-gray-700">
                {item.pinned ? "Pinned" : "Not pinned"}
              </p>
              <p className="text-xs text-gray-400">
                Pinned claims are immune to auto-supersede
              </p>
            </div>
            <button
              onClick={handlePin}
              disabled={updateClaim.isPending}
              className={clsx(
                "px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50",
                item.pinned
                  ? "bg-amber-100 text-amber-800 hover:bg-amber-200"
                  : "bg-gray-100 text-gray-700 hover:bg-gray-200",
              )}
            >
              {item.pinned ? "Unpin" : "Pin"}
            </button>
          </div>

          {/* Delete */}
          <div className="flex items-center justify-between pt-2 border-t border-gray-100">
            <div>
              <p className="text-sm font-medium text-gray-700">Delete claim</p>
              <p className="text-xs text-gray-400">Soft-deleted; 30-day recovery window</p>
            </div>
            {!confirmDelete ? (
              <button
                onClick={() => setConfirmDelete(true)}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-gray-100 text-red-600 hover:bg-red-50"
              >
                Delete
              </button>
            ) : (
              <div className="flex gap-2">
                <button
                  onClick={() => setConfirmDelete(false)}
                  className="px-3 py-2 rounded-lg text-sm text-gray-600 hover:bg-gray-100"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  disabled={deleteFact.isPending}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
                >
                  {deleteFact.isPending ? "Deleting…" : "Confirm delete"}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Debug: raw tags */}
        {item.claim.tags && item.claim.tags.length > 0 && (
          <details className="text-xs text-gray-400">
            <summary className="cursor-pointer hover:text-gray-600">
              Raw tags
            </summary>
            <pre className="mt-1 font-mono">
              {JSON.stringify(item.claim.tags, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </div>
  );
}
