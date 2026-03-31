import type { TotalReclaw, FactMetadata } from '@totalreclaw/client';
import type { ExtractedFact } from '../extraction/prompts';
import {
  POST_TURN_PROMPT,
  validateExtractionResponse,
  formatConversationHistory,
} from '../extraction/prompts';
import {
  getExtractInterval,
  getMaxFactsPerExtraction,
  handleQuotaError,
} from '../billing.js';

export interface AgentEndInput {
  conversationHistory: Array<{
    role: 'user' | 'assistant';
    content: string;
  }>;
  groupFolder: string;
  turnCount: number;
}

export interface AgentEndOutput {
  factsExtracted: number;
  factsStored: number;
}

export type LLMClient = {
  generate: (system: string, user: string, options?: { responseFormat?: { type: string } }) => Promise<string>;
};

const MIN_IMPORTANCE = parseInt(process.env.TOTALRECLAW_MIN_IMPORTANCE || '6', 10);

export async function agentEnd(
  client: TotalReclaw,
  llmClient: LLMClient | null,
  input: AgentEndInput
): Promise<AgentEndOutput> {
  const extractInterval = getExtractInterval();
  if (input.turnCount % extractInterval !== 0) {
    return { factsExtracted: 0, factsStored: 0 };
  }

  if (!llmClient) {
    return { factsExtracted: 0, factsStored: 0 };
  }

  try {
    const history = formatConversationHistory(
      input.conversationHistory.map((t) => ({
        role: t.role,
        content: t.content,
        timestamp: new Date(),
      }))
    );

    const existingMemories = await client.recall('*history*', 20);
    const namespaceMemories = existingMemories.filter(r => {
      const tags = r.fact.metadata.tags || [];
      return tags.includes(`namespace:${input.groupFolder}`);
    });

    const existingMemoriesStr = namespaceMemories.length > 0
      ? namespaceMemories.map(m => `[ID: ${m.fact.id}] ${m.fact.text}`).join('\n')
      : '(No existing memories)';

    const prompt = POST_TURN_PROMPT.format({
      conversationHistory: history,
      existingMemories: existingMemoriesStr,
    });

    const response = await llmClient.generate(prompt.system, prompt.user, {
      responseFormat: { type: 'json_object' },
    });

    const parsed = JSON.parse(response);
    const validation = validateExtractionResponse(parsed);

    if (!validation.valid) {
      console.error('Extraction validation failed:', validation.errors);
      return { factsExtracted: 0, factsStored: 0 };
    }

    let factsStored = 0;
    const maxFacts = getMaxFactsPerExtraction();
    const factsToProcess = validation.facts!.slice(0, maxFacts);
    if (validation.facts!.length > maxFacts) {
      console.log(`Capped extraction from ${validation.facts!.length} to ${maxFacts} facts`);
    }
    for (const fact of factsToProcess) {
      if (fact.action === 'ADD' && fact.importance >= MIN_IMPORTANCE) {
        const metadata: FactMetadata = {
          importance: fact.importance / 10,
          source: 'agent_end_extraction',
          tags: [
            `namespace:${input.groupFolder}`,
            fact.type,
          ],
        };

        try {
          await client.remember(fact.text, metadata);
          factsStored++;
        } catch (err: unknown) {
          // Check for 403 / quota exceeded -- invalidate billing cache so next
          // before_agent_start re-fetches and warns the user.
          if (handleQuotaError(err)) {
            break; // Stop trying to store remaining facts -- they'll all fail too
          }
          // Otherwise skip this fact and continue with the rest
          console.warn(`Failed to store fact: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }

    return {
      factsExtracted: validation.facts!.length,
      factsStored,
    };
  } catch (error) {
    // Check for quota errors at the top level too (e.g. batch submit failures).
    handleQuotaError(error);
    console.error('agentEnd error:', error);
    return { factsExtracted: 0, factsStored: 0 };
  }
}
