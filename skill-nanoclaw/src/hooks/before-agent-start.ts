import type { TotalReclaw } from '@totalreclaw/client';
import {
  getBillingContext,
  fetchBillingStatus,
  getQuotaWarning,
  checkWelcomeBack,
} from '../billing.js';

export interface BeforeAgentStartInput {
  userMessage: string;
  groupFolder: string;
  sessionId?: string;
}

export interface BeforeAgentStartOutput {
  contextString?: string;
  memories: Array<{
    text: string;
    score: number;
    type: string;
  }>;
  latencyMs: number;
}

export async function beforeAgentStart(
  client: TotalReclaw,
  input: BeforeAgentStartInput,
  maxMemories: number = 8
): Promise<BeforeAgentStartOutput> {
  const startTime = Date.now();

  try {
    const results = await client.recall(input.userMessage, maxMemories);

    const filtered = results.filter(r => {
      const tags = r.fact.metadata.tags || [];
      return tags.includes(`namespace:${input.groupFolder}`) ||
             (!tags.some(t => t.startsWith('namespace:')) && input.groupFolder === 'default');
    });

    const memories = filtered.map(r => ({
      text: r.fact.text,
      score: r.score,
      type: r.fact.metadata.tags?.find(t =>
        ['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary'].includes(t)
      ) || 'fact',
    }));

    // --- Billing check ---
    let billingWarning = '';
    let welcomeBack = '';
    try {
      const ctx = await getBillingContext();
      if (ctx) {
        // Fetch (or read cached) billing status.
        const cache = await fetchBillingStatus(ctx);
        billingWarning = getQuotaWarning(cache);

        // One-time welcome-back for returning Pro users (first conversation after import).
        if (!cache && ctx.walletAddress) {
          welcomeBack = await checkWelcomeBack(ctx);
        }
      }
    } catch {
      // Best-effort -- don't block on billing check failure.
    }

    const contextString = memories.length > 0
      ? formatMemoriesForContext(memories) + welcomeBack + billingWarning
      : (welcomeBack || billingWarning)
        ? (welcomeBack + billingWarning).trim()
        : undefined;

    return {
      contextString,
      memories,
      latencyMs: Date.now() - startTime,
    };
  } catch (error) {
    console.error('beforeAgentStart error:', error);
    return {
      memories: [],
      latencyMs: Date.now() - startTime,
    };
  }
}

function formatMemoriesForContext(memories: Array<{ text: string; score: number; type: string }>): string {
  const lines = ['## Relevant Memories\n'];
  for (const m of memories) {
    lines.push(`- [${m.type}] ${m.text}`);
  }
  return lines.join('\n');
}
