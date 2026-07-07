// ---------------------------------------------------------------------------
// Plugin configSchema — static JSON-schema for the OpenClaw plugin config.
// Extracted from index.ts; pure data, no runtime state.
// ---------------------------------------------------------------------------

export const CONFIG_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    publicUrl: {
      type: 'string',
      description:
        "Public gateway URL for QR pairing (e.g. 'https://gateway.example.com:18789'). Overrides the auto-resolution cascade in buildPairingUrl.",
    },
    extraction: {
      type: 'object',
      additionalProperties: false,
      properties: {
        enabled: {
          type: 'boolean',
          description: 'Enable/disable auto-extraction (default: true)',
        },
        model: {
          type: 'string',
          description:
            "Shorthand: override just the extraction model (e.g., 'glm-4.5-flash', 'gpt-4.1-mini'). For a full provider override use extraction.llm.",
        },
        interval: {
          type: 'number',
          description: 'Number of turns between automatic extractions (default: 3)',
        },
        maxFactsPerExtraction: {
          type: 'number',
          description: 'Hard cap on facts extracted per turn (default: 15)',
        },
        llm: {
          type: 'object',
          additionalProperties: false,
          description:
            'Explicit LLM override block. Highest-priority tier in the extraction-provider cascade. Any subset of provider+apiKey is enough to pin a provider.',
          properties: {
            provider: {
              type: 'string',
              description:
                "Provider name: zai | openai | anthropic | gemini | google | mistral | groq | deepseek | openrouter | xai | together | cerebras.",
            },
            model: {
              type: 'string',
              description: 'Explicit model id. If omitted, deriveCheapModel(provider) picks a sensible default.',
            },
            apiKey: {
              type: 'string',
              description: 'API key for the selected provider. Required for the override to take effect.',
            },
            baseUrl: {
              type: 'string',
              description: 'Override the provider base URL (self-hosted / custom gateway setups).',
            },
          },
        },
      },
    },
  },
} as const;
