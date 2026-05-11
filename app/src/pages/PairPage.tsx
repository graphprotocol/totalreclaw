import { useState, useRef, useCallback, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import { isMnemonicValid, deriveSessionKeys } from "../lib/crypto";
import { useCrypto } from "../contexts/CryptoContext";

const WORD_COUNT = 12;

export function PairPage() {
  const [words, setWords] = useState<string[]>(Array(WORD_COUNT).fill(""));
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);
  const { setKeys } = useCrypto();
  const navigate = useNavigate();

  const handleWordChange = useCallback(
    (index: number, value: string) => {
      // Handle paste of full phrase into any word slot
      const trimmed = value.trim();
      const parts = trimmed.split(/\s+/);
      if (parts.length === WORD_COUNT) {
        setWords(parts.map((w) => w.toLowerCase()));
        inputRefs.current[WORD_COUNT - 1]?.focus();
        return;
      }
      setWords((prev) => {
        const next = [...prev];
        next[index] = value.toLowerCase().replace(/[^a-z]/g, "");
        return next;
      });
      setError(null);
    },
    [],
  );

  const handleKeyDown = useCallback(
    (index: number, e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === " " || e.key === "Tab") {
        e.preventDefault();
        const next = inputRefs.current[index + 1];
        if (next) next.focus();
      } else if (e.key === "Backspace" && words[index] === "" && index > 0) {
        e.preventDefault();
        inputRefs.current[index - 1]?.focus();
      }
    },
    [words],
  );

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setError(null);

      const phrase = words.join(" ").trim();
      if (words.some((w) => !w)) {
        setError("Please enter all 12 words.");
        return;
      }
      if (!isMnemonicValid(phrase)) {
        setError(
          "Invalid recovery phrase. Check each word against the BIP-39 wordlist.",
        );
        return;
      }

      setLoading(true);
      try {
        const keys = await deriveSessionKeys(phrase);
        setKeys(keys);
        navigate("/vault", { replace: true });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Key derivation failed");
      } finally {
        setLoading(false);
      }
    },
    [words, setKeys, navigate],
  );

  const allFilled = words.every((w) => w.length > 0);
  const phrase = words.join(" ");
  const isValid = allFilled && isMnemonicValid(phrase);

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <h1 className="text-2xl font-semibold text-gray-900">
            TotalReclaw
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            Enter your 12-word recovery phrase to access your vault
          </p>
        </div>

        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <form onSubmit={handleSubmit}>
            <div className="grid grid-cols-3 gap-2 mb-5">
              {Array.from({ length: WORD_COUNT }, (_, i) => (
                <div key={i} className="relative">
                  <span className="absolute left-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 select-none w-4 text-right">
                    {i + 1}
                  </span>
                  <input
                    ref={(el) => {
                      inputRefs.current[i] = el;
                    }}
                    type="text"
                    autoComplete="off"
                    autoCorrect="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    value={words[i]}
                    onChange={(e) => handleWordChange(i, e.target.value)}
                    onKeyDown={(e) => handleKeyDown(i, e)}
                    placeholder={`word ${i + 1}`}
                    className={clsx(
                      "w-full pl-7 pr-2 py-1.5 text-sm font-mono border rounded-md",
                      "focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent",
                      "placeholder:text-gray-300",
                      words[i] && !isValid && allFilled
                        ? "border-red-300 bg-red-50"
                        : "border-gray-300 bg-gray-50",
                    )}
                  />
                </div>
              ))}
            </div>

            {error && (
              <p className="text-sm text-red-600 mb-4">{error}</p>
            )}

            <button
              type="submit"
              disabled={!allFilled || loading}
              className={clsx(
                "w-full py-2.5 px-4 rounded-lg text-sm font-medium transition-colors",
                allFilled && !loading
                  ? "bg-blue-600 text-white hover:bg-blue-700 active:bg-blue-800"
                  : "bg-gray-100 text-gray-400 cursor-not-allowed",
              )}
            >
              {loading ? "Deriving keys…" : "Access vault"}
            </button>
          </form>
        </div>

        <p className="text-center text-xs text-gray-400 mt-4">
          Phrase is held in memory only. Never written to disk or storage.
        </p>
      </div>
    </div>
  );
}
