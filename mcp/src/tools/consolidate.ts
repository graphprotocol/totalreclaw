import { TotalReclaw, RerankedResult } from '@totalreclaw/client';
import {
  clusterFacts,
  getConsolidationThreshold,
  type DecryptedCandidate,
} from '../consolidation.js';

export interface ConsolidateInput {
  dry_run?: boolean;
}

export const consolidateToolDefinition = {
  name: 'totalreclaw_consolidate',
  description: `Scan all stored memories and merge near-duplicates.

Keeps the most important/recent version and removes redundant copies.
Use this to clean up your memory vault after extended use.

WHEN TO USE:
- User asks to clean up or deduplicate memories
- After importing a large batch of memories
- Periodically for memory hygiene

PARAMETERS:
- dry_run: Preview consolidation without deleting (default: false)`,
  inputSchema: {
    type: 'object',
    properties: {
      dry_run: {
        type: 'boolean',
        description: 'Preview consolidation without deleting (default: false)',
      },
    },
    additionalProperties: false,
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: true,
    idempotentHint: false,
  },
};

/**
 * Handle consolidate in HTTP mode.
 *
 * Flow:
 *   1. Export all facts via recall('*', 1000)
 *   2. Build DecryptedCandidate[] from results (embeddings already decrypted)
 *   3. Cluster by cosine similarity
 *   4. Batch-delete duplicates (unless dry_run)
 */
export async function handleConsolidate(
  client: TotalReclaw,
  args: unknown,
  _defaultNamespace: string,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = (args || {}) as ConsolidateInput;
  const dryRun = input.dry_run ?? false;

  try {
    // 1. Fetch all facts. The client's recall('*', N) returns up to N results.
    //    We use a large k to get as many as possible.
    const results = await client.recall('*', 1000);

    if (results.length === 0) {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            success: true,
            message: 'No memories found to consolidate.',
            scanned: 0,
            clusters: 0,
            duplicates: 0,
            dry_run: dryRun,
          }),
        }],
      };
    }

    // 2. Convert to DecryptedCandidate[] for the clustering algorithm.
    const allDecrypted: DecryptedCandidate[] = results.map((r: RerankedResult) => ({
      id: r.fact.id,
      text: r.fact.text,
      embedding: r.fact.embedding && r.fact.embedding.length > 0 ? r.fact.embedding : null,
      importance: Math.round((r.fact.metadata.importance ?? 0.5) * 10),
      decayScore: r.decayAdjustedScore,
      createdAt: r.fact.createdAt.getTime(),
      version: 1,
    }));

    // 3. Cluster by cosine similarity.
    const threshold = getConsolidationThreshold();
    const clusters = clusterFacts(allDecrypted, threshold);

    if (clusters.length === 0) {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            success: true,
            message: `Scanned ${allDecrypted.length} memories -- no near-duplicates found.`,
            scanned: allDecrypted.length,
            clusters: 0,
            duplicates: 0,
            dry_run: dryRun,
          }),
        }],
      };
    }

    // 4. Build report.
    const totalDuplicates = clusters.reduce((sum, c) => sum + c.duplicates.length, 0);
    const reportLines: string[] = [
      `Scanned ${allDecrypted.length} memories.`,
      `Found ${clusters.length} cluster(s) with ${totalDuplicates} duplicate(s).`,
      '',
    ];

    const displayClusters = clusters.slice(0, 10);
    for (let i = 0; i < displayClusters.length; i++) {
      const cluster = displayClusters[i];
      const repText = cluster.representative.text.length > 80
        ? cluster.representative.text.slice(0, 80) + '...'
        : cluster.representative.text;
      reportLines.push(`Cluster ${i + 1}: KEEP "${repText}"`);
      for (const dup of cluster.duplicates) {
        const dupText = dup.text.length > 80
          ? dup.text.slice(0, 80) + '...'
          : dup.text;
        reportLines.push(`  - REMOVE "${dupText}" (ID: ${dup.id})`);
      }
    }
    if (clusters.length > 10) {
      reportLines.push(`... and ${clusters.length - 10} more cluster(s).`);
    }

    // 5. If not dry_run, delete duplicates.
    let totalDeleted = 0;
    if (!dryRun) {
      const idsToDelete = clusters.flatMap((c) => c.duplicates.map((d) => d.id));
      for (const id of idsToDelete) {
        try {
          await client.forget(id);
          totalDeleted++;
        } catch {
          // Skip individual delete failures
        }
      }

      reportLines.push('');
      reportLines.push(`Deleted ${totalDeleted} duplicate memories.`);
    } else {
      reportLines.push('');
      reportLines.push('DRY RUN -- no memories were deleted. Run without dry_run to apply.');
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          message: reportLines.join('\n'),
          scanned: allDecrypted.length,
          clusters: clusters.length,
          duplicates: totalDuplicates,
          deleted: dryRun ? 0 : totalDeleted,
          dry_run: dryRun,
        }),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error occurred';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: `Failed to consolidate memories: ${message}`,
        }),
      }],
    };
  }
}
