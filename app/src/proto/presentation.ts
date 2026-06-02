// Presentation A/B: "type" (taxonomy-forward) vs "source" (provenance-forward, type demoted).
export type Presentation = "type" | "source";

export function sourceLabel(source: string): string {
  switch (source) {
    case "user":
      return "From you";
    case "user-inferred":
      return "Your agent inferred this";
    case "assistant":
      return "From the assistant";
    case "external":
      return "Imported";
    case "derived":
      return "Derived";
    default:
      return source;
  }
}

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
      return source;
  }
}

/** The few type distinctions with plain-language value; everything else is just "memory". */
export function typeBucket(type: string): { label: string; tone: "rule" | "todo" | "pref" } | null {
  switch (type) {
    case "directive":
      return { label: "rule", tone: "rule" };
    case "preference":
      return { label: "preference", tone: "pref" };
    case "commitment":
      return { label: "to-do", tone: "todo" };
    default:
      return null;
  }
}
