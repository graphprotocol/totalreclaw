import type { Config } from "tailwindcss";

/**
 * "The Keeper" design system — see /DESIGN.md (repo root).
 * Warm-white field, one clay accent, soft & tactile surfaces.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        clay: "#C16240",
        "clay-deep": "#A54B2E",
        "clay-tint": "#F7E7DD",
        "warm-white": "#FBFAF8",
        surface: "#FFFFFF",
        ink: "#2B2824",
        "ink-muted": "#685E57",
        hairline: "#E7E3DF",
        // Memory-type palette (soft warm tint + accessible ink). Color reinforces
        // the mandatory text label; never the sole signal.
        "type-claim": "#EFEBE7",
        "type-claim-ink": "#574F49",
        "type-preference": "#E6EDF6",
        "type-preference-ink": "#3F5B7A",
        "type-directive": "#ECE6F4",
        "type-directive-ink": "#6A4E86",
        "type-commitment": "#F6E9CE",
        "type-commitment-ink": "#7A5A24",
        "type-episode": "#ECE9E5",
        "type-episode-ink": "#655F58",
        "type-summary": "#DDEFE2",
        "type-summary-ink": "#3E6B4E",
      },
      fontFamily: {
        display: ["Fraunces", "Georgia", "serif"],
        sans: ["Figtree", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "ui-monospace", "monospace"],
      },
      borderRadius: {
        control: "12px",
        card: "16px",
        pill: "9999px",
      },
      boxShadow: {
        soft: "0 1px 2px rgba(43,40,36,0.04), 0 2px 8px rgba(43,40,36,0.06)",
        raised: "0 4px 16px rgba(43,40,36,0.10)",
        overlay: "0 8px 32px rgba(43,40,36,0.14)",
      },
      transitionTimingFunction: {
        keeper: "cubic-bezier(0.165, 0.84, 0.44, 1)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "page-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.4s cubic-bezier(0.165,0.84,0.44,1) both",
        "page-in": "page-in 0.35s cubic-bezier(0.165,0.84,0.44,1) both",
      },
    },
  },
  plugins: [],
} satisfies Config;
