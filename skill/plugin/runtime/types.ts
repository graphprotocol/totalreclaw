// ---------------------------------------------------------------------------
// Shared plugin runtime types
// ---------------------------------------------------------------------------
//
// Extracted from index.ts so the composing entry point AND the domain modules
// carved out of it (runtime/format-helpers, import/import-runtime, …) can share
// one definition of the OpenClaw plugin-API surface and the internal row shapes
// without a circular import back through index.ts.
//
// Nothing here reads the environment or performs I/O — pure type declarations.

/** OpenClaw Plugin API type (defined locally to avoid an SDK dependency). */
export interface OpenClawPluginApi {
  logger: {
    info(...args: unknown[]): void;
    warn(...args: unknown[]): void;
    error(...args: unknown[]): void;
  };
  config?: {
    agents?: {
      defaults?: {
        model?: {
          primary?: string;
        };
      };
    };
    models?: {
      providers?: Record<string, {
        baseUrl: string;
        apiKey?: string;
        api?: string;
        models?: Array<{ id: string; [k: string]: unknown }>;
        [k: string]: unknown;
      }>;
      [k: string]: unknown;
    };
    [key: string]: unknown;
  };
  pluginConfig?: Record<string, unknown>;
  registerTool(tool: unknown, opts?: { name?: string; names?: string[] }): void;
  registerService(service: { id: string; start(): void; stop?(): void }): void;
  on(hookName: string, handler: (...args: unknown[]) => unknown, opts?: { priority?: number }): void;
  /**
   * 3.2.0 — register a top-level `openclaw <cmd>` subcommand. The handler
   * receives a commander `Command` to attach subcommands to. Output goes
   * straight to the user's TTY; nothing touches the LLM or the transcript.
   * We deliberately type `program` as `unknown` at this boundary because
   * we don't import the SDK's full types; the runtime shape is commander's
   * `Command` which we cast at the call site.
   */
  registerCli?(
    registrar: (ctx: { program: unknown; config?: unknown; workspaceDir?: string; logger?: unknown }) => void | Promise<void>,
    opts?: { commands?: string[] },
  ): void;
  /**
   * 3.2.0 — register a slash command (e.g. `/totalreclaw`). The handler
   * runs before the agent; its reply is delivered via the channel adapter.
   * Reply text IS appended to the session transcript (see gateway-cli
   * L9300-9312), so we only emit non-secret pointers.
   */
  registerCommand?(command: {
    name: string;
    description: string;
    acceptsArgs?: boolean;
    requireAuth?: boolean;
    handler: (ctx: {
      senderId?: string;
      channel?: string;
      args?: string;
      commandBody?: string;
      isAuthorizedSender?: boolean;
      config?: unknown;
    }) => { text: string } | Promise<{ text: string }>;
  }): void;
  /**
   * 3.3.0 — register an HTTP route on the gateway's HTTP server.
   * Used by the QR-pairing flow to serve the pairing page + the
   * encrypted-payload respond endpoint. Path is exact-match against
   * `new URL(req.url, ...).pathname`; no params supported.
   */
  registerHttpRoute?(params: {
    path: string;
    handler: (req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse) => Promise<void> | void;
    /** OpenClaw 2026.4.2+ — required; loader silently drops the route if absent. */
    auth: 'gateway' | 'plugin';
  }): void;
}

/** Row shape returned by the migration/export GraphQL helpers. */
export interface MigrationFact {
  id: string;
  owner: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  isActive: boolean;
  contentFp: string;
  source: string;
  agentId: string;
  version: number;
  timestamp: string;
}

/** Smart import result containing profile, triage decisions, and enriched system prompt. */
export interface SmartImportContext {
  /** JSON-serialized UserProfile (for WASM calls that require profile_json) */
  profileJson: string;
  /** Triage decisions indexed by chunk_index */
  decisions: Array<{ chunk_index: number; decision: string; reason: string }>;
  /** Enriched system prompt for extraction (profile context injected) */
  enrichedSystemPrompt: string;
  /** Number of chunks marked for extraction */
  extractCount: number;
  /** Number of chunks marked for skipping */
  skipCount: number;
  /** Duration of the profiling + triage pipeline in ms */
  durationMs: number;
}
