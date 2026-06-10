// Source-forward presentation (the type/source A/B toggle was dropped — source
// is the default lens; type is just a badge). Mirrors the Keeper prototype.

export function sourceShort(source: string): string {
  switch (source) {
    case "user":
      return "You";
    case "user-inferred":
      return "Inferred";
    case "assistant":
      return "Assistant";
    case "external":
      return "Imported";
    case "derived":
      return "Derived";
    default:
      return source || "Unknown";
  }
}

export const cap = (s: string): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
