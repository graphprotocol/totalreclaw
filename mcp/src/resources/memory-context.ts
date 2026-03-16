import { TotalReclaw, RerankedResult } from '@totalreclaw/client';

// ── Resource Definition ──────────────────────────────────────────────────────

export const memoryContextResource = {
  uri: 'memory://context/summary',
  name: 'Memory Summary',
  title: 'Your TotalReclaw Context',
  description:
    'A summary of your most important and recent memories. Include this for personalized responses.',
  mimeType: 'text/markdown',
  annotations: {
    audience: ['assistant'] as string[],
    priority: 0.9,
  },
};

// ── Cached content ───────────────────────────────────────────────────────────

interface CacheEntry {
  content: string;
  timestamp: number;
}

const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

let cachedSummary: CacheEntry | null = null;

/** Invalidate the cache (call after remember or forget). */
export function invalidateMemoryContextCache(): void {
  cachedSummary = null;
}

// ── Generate the resource content ────────────────────────────────────────────

export async function readMemoryContext(
  client: TotalReclaw,
): Promise<string> {
  // Return cached version if still valid
  if (cachedSummary && Date.now() - cachedSummary.timestamp < CACHE_TTL_MS) {
    return cachedSummary.content;
  }

  try {
    // Fetch top ~20 facts by importance (recall with a broad query, large k)
    const results = await client.recall('*', 50);

    if (results.length === 0) {
      const empty = `## Your Memory Context\n\n*No memories stored yet. Memories will appear here as you share information across conversations.*\n`;
      cachedSummary = { content: empty, timestamp: Date.now() };
      return empty;
    }

    // Sort by importance (descending), then by recency (newest first)
    const sorted = results
      .map((r: RerankedResult) => ({
        text: r.fact.text,
        importance: Math.round((r.fact.metadata.importance ?? 0.5) * 10),
        createdAt: r.fact.createdAt,
        score: r.score,
      }))
      .sort((a: { importance: number; createdAt: Date }, b: { importance: number; createdAt: Date }) => {
        // Primary: importance descending
        if (b.importance !== a.importance) return b.importance - a.importance;
        // Secondary: recency descending
        return b.createdAt.getTime() - a.createdAt.getTime();
      })
      .slice(0, 20);

    // Partition into high-priority and recent
    const highPriority = sorted.filter((f: { importance: number }) => f.importance >= 7);
    const recent = sorted
      .filter((f: { importance: number }) => f.importance < 7)
      .sort((a: { createdAt: Date }, b: { createdAt: Date }) => b.createdAt.getTime() - a.createdAt.getTime())
      .slice(0, 10);

    const lines: string[] = ['## Your Memory Context', ''];

    if (highPriority.length > 0) {
      lines.push('### High Priority');
      for (const f of highPriority) {
        lines.push(`- ${f.text} (importance: ${f.importance}/10)`);
      }
      lines.push('');
    }

    if (recent.length > 0) {
      lines.push('### Recent');
      for (const f of recent) {
        const age = formatAge(f.createdAt);
        lines.push(`- ${f.text} (${age})`);
      }
      lines.push('');
    }

    const totalStored = results.length;
    lines.push(
      `*${totalStored} total memories stored. Use totalreclaw_recall for specific searches.*`
    );

    const content = lines.join('\n');
    cachedSummary = { content, timestamp: Date.now() };
    return content;
  } catch (error) {
    const msg = error instanceof Error ? error.message : 'Unknown error';
    return `## Your Memory Context\n\n*Error loading memories: ${msg}*\n`;
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatAge(date: Date): string {
  const diffMs = Date.now() - date.getTime();
  const diffMins = Math.floor(diffMs / (1000 * 60));
  if (diffMins < 60) return `${diffMins} minutes ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours} hours ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays === 1) return '1 day ago';
  if (diffDays < 30) return `${diffDays} days ago`;
  const diffMonths = Math.floor(diffDays / 30);
  if (diffMonths === 1) return '1 month ago';
  return `${diffMonths} months ago`;
}
