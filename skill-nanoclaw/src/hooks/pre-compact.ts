import type { TotalReclaw, FactMetadata } from '@totalreclaw/client';
import type { LLMClient } from './agent-end';
import type { ExtractedFact } from '../extraction/prompts';
import {
  PRE_COMPACTION_PROMPT,
  DEBRIEF_SYSTEM_PROMPT,
  validateExtractionResponse,
  parseDebriefResponse,
} from '../extraction/prompts';
import { handleQuotaError } from '../billing.js';

export interface PreCompactInput {
  transcript: string;
  groupFolder: string;
  claudeMdPath?: string;
}

export interface PreCompactOutput {
  factsExtracted: number;
  factsStored: number;
  claudeMdUpdated: boolean;
}

export async function preCompact(
  client: TotalReclaw,
  llmClient: LLMClient | null,
  input: PreCompactInput
): Promise<PreCompactOutput> {
  if (!llmClient) {
    return { factsExtracted: 0, factsStored: 0, claudeMdUpdated: false };
  }

  try {
    const existingMemories = await client.recall('*', 100);
    const namespaceMemories = existingMemories.filter(r => {
      const tags = r.fact.metadata.tags || [];
      return tags.includes(`namespace:${input.groupFolder}`);
    });

    const existingMemoriesStr = namespaceMemories.length > 0
      ? namespaceMemories.map(m => `[ID: ${m.fact.id}] ${m.fact.text}`).join('\n')
      : '(No existing memories)';

    const prompt = PRE_COMPACTION_PROMPT.format({
      conversationHistory: input.transcript,
      existingMemories: existingMemoriesStr,
    });

    const response = await llmClient.generate(prompt.system, prompt.user, {
      responseFormat: { type: 'json_object' },
    });

    const parsed = JSON.parse(response);
    const validation = validateExtractionResponse(parsed);

    if (!validation.valid) {
      console.error('Pre-compact validation failed:', validation.errors);
      return { factsExtracted: 0, factsStored: 0, claudeMdUpdated: false };
    }

    let factsStored = 0;
    let quotaExceeded = false;
    for (const fact of validation.facts!) {
      if (quotaExceeded) break;

      const metadata: FactMetadata = {
        importance: fact.importance / 10,
        source: 'pre_compaction',
        tags: [
          `namespace:${input.groupFolder}`,
          fact.type,
        ],
      };

      switch (fact.action) {
        case 'ADD':
          try {
            await client.remember(fact.text, metadata);
            factsStored++;
          } catch (err: unknown) {
            if (handleQuotaError(err)) {
              quotaExceeded = true;
            }
          }
          break;

        case 'UPDATE':
        case 'DELETE':
          if (fact.existingFactId) {
            try {
              await client.forget(fact.existingFactId);
              if (fact.action === 'UPDATE') {
                await client.remember(fact.text, metadata);
                factsStored++;
              }
            } catch (err: unknown) {
              if (handleQuotaError(err)) {
                quotaExceeded = true;
              }
              // Skip if delete/update fails for other reasons
            }
          }
          break;

        case 'NOOP':
          break;
      }
    }

    // Session debrief — after regular extraction
    let debriefStored = 0;
    if (llmClient && validation.facts && validation.facts.length > 0) {
      try {
        const storedTexts = validation.facts
          .filter(f => f.action === 'ADD' || f.action === 'UPDATE')
          .map(f => f.text);
        const alreadyStored = storedTexts.length > 0
          ? storedTexts.map(t => `- ${t}`).join('\n')
          : '(none)';
        const debriefSystemPrompt = DEBRIEF_SYSTEM_PROMPT.replace('{already_stored_facts}', alreadyStored);

        const debriefResponse = await llmClient.generate(
          debriefSystemPrompt,
          `Review this conversation and provide a debrief:\n\n${input.transcript}`,
        );

        const debriefItems = parseDebriefResponse(debriefResponse);
        for (const item of debriefItems) {
          if (quotaExceeded) break;
          const debriefMetadata: FactMetadata = {
            importance: item.importance / 10,
            source: 'nanoclaw_debrief',
            tags: [
              `namespace:${input.groupFolder}`,
              item.type,
            ],
          };
          try {
            await client.remember(item.text, debriefMetadata);
            debriefStored++;
          } catch (err: unknown) {
            if (handleQuotaError(err)) {
              quotaExceeded = true;
            }
          }
        }
        if (debriefStored > 0) {
          console.error(`Session debrief: stored ${debriefStored} items`);
        }
      } catch (debriefErr) {
        console.error('Pre-compact debrief failed:', debriefErr);
      }
    }

    let claudeMdUpdated = false;
    if (input.claudeMdPath) {
      claudeMdUpdated = await syncToClaudeMd(client, input.groupFolder, input.claudeMdPath);
    }

    return {
      factsExtracted: validation.facts!.length,
      factsStored: factsStored + debriefStored,
      claudeMdUpdated,
    };
  } catch (error) {
    handleQuotaError(error);
    console.error('preCompact error:', error);
    return { factsExtracted: 0, factsStored: 0, claudeMdUpdated: false };
  }
}

async function syncToClaudeMd(
  client: TotalReclaw,
  namespace: string,
  claudeMdPath: string
): Promise<boolean> {
  const fs = await import('fs/promises');

  try {
    const memories = await client.recall('*', { k: 50 } as any);

    const filtered = memories.filter(m => {
      const tags = m.fact.metadata.tags || [];
      return tags.includes(`namespace:${namespace}`) &&
             (m.fact.metadata.importance || 0.5) >= 0.7;
    });

    if (filtered.length === 0) return false;

    let existing = '';
    try {
      existing = await fs.readFile(claudeMdPath, 'utf-8');
    } catch {
      // File doesn't exist
    }

    if (!existing.includes('## TotalReclaw Sync')) {
      const section = '\n\n## TotalReclaw Sync\n\n' +
        filtered.map(m => `- ${m.fact.text}`).join('\n');

      await fs.writeFile(claudeMdPath, existing + section, 'utf-8');
      return true;
    }

    return false;
  } catch {
    return false;
  }
}
