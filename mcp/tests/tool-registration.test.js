/**
 * @jest-environment node
 */

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

const {
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
} = require('../dist/tools/index.js');

const ALL_TOOL_DEFINITIONS = [
  rememberToolDefinition,
  recallToolDefinition,
  forgetToolDefinition,
  exportToolDefinition,
  importToolDefinition,
];

const EXPECTED_TOOL_NAMES = [
  'totalreclaw_remember',
  'totalreclaw_recall',
  'totalreclaw_forget',
  'totalreclaw_export',
  'totalreclaw_import',
];

describe('Tool Registration & Routing', () => {
  describe('all tools have required fields', () => {
    it.each(ALL_TOOL_DEFINITIONS.map(t => [t.name, t]))(
      '%s has name, description, and inputSchema',
      (_name, tool) => {
        expect(typeof tool.name).toBe('string');
        expect(tool.name.length).toBeGreaterThan(0);

        expect(typeof tool.description).toBe('string');
        expect(tool.description.length).toBeGreaterThan(0);

        expect(tool.inputSchema).toBeDefined();
        expect(tool.inputSchema.type).toBe('object');
        expect(tool.inputSchema.properties).toBeDefined();
        expect(typeof tool.inputSchema.properties).toBe('object');
      }
    );
  });

  describe('ListTools completeness', () => {
    it('should expose exactly 5 tools', () => {
      expect(ALL_TOOL_DEFINITIONS).toHaveLength(5);
    });

    it('every expected tool name has a matching definition', () => {
      const names = ALL_TOOL_DEFINITIONS.map(t => t.name);
      for (const expected of EXPECTED_TOOL_NAMES) {
        expect(names).toContain(expected);
      }
    });

    it('no duplicate tool names', () => {
      const names = ALL_TOOL_DEFINITIONS.map(t => t.name);
      const unique = new Set(names);
      expect(unique.size).toBe(names.length);
    });
  });

  describe('all tools have annotations', () => {
    it.each(ALL_TOOL_DEFINITIONS.map(t => [t.name, t]))(
      '%s has readOnlyHint annotation',
      (_name, tool) => {
        expect(tool.annotations).toBeDefined();
        expect(typeof tool.annotations.readOnlyHint).toBe('boolean');
      }
    );

    it.each(ALL_TOOL_DEFINITIONS.map(t => [t.name, t]))(
      '%s has destructiveHint annotation',
      (_name, tool) => {
        expect(tool.annotations).toBeDefined();
        expect(typeof tool.annotations.destructiveHint).toBe('boolean');
      }
    );

    it.each(ALL_TOOL_DEFINITIONS.map(t => [t.name, t]))(
      '%s has idempotentHint annotation',
      (_name, tool) => {
        expect(tool.annotations).toBeDefined();
        expect(typeof tool.annotations.idempotentHint).toBe('boolean');
      }
    );
  });

  describe('annotation semantics', () => {
    it('recall and export are read-only', () => {
      expect(recallToolDefinition.annotations.readOnlyHint).toBe(true);
      expect(exportToolDefinition.annotations.readOnlyHint).toBe(true);
    });

    it('remember, forget, and import are NOT read-only', () => {
      expect(rememberToolDefinition.annotations.readOnlyHint).toBe(false);
      expect(forgetToolDefinition.annotations.readOnlyHint).toBe(false);
      expect(importToolDefinition.annotations.readOnlyHint).toBe(false);
    });

    it('forget is destructive', () => {
      expect(forgetToolDefinition.annotations.destructiveHint).toBe(true);
    });

    it('remember, recall, export, and import are NOT destructive', () => {
      expect(rememberToolDefinition.annotations.destructiveHint).toBe(false);
      expect(recallToolDefinition.annotations.destructiveHint).toBe(false);
      expect(exportToolDefinition.annotations.destructiveHint).toBe(false);
      expect(importToolDefinition.annotations.destructiveHint).toBe(false);
    });

    it('import is NOT idempotent (can create duplicates)', () => {
      expect(importToolDefinition.annotations.idempotentHint).toBe(false);
    });
  });

  describe('tool handler lookup', () => {
    const { handleRemember } = require('../dist/tools/remember.js');
    const { handleRecall } = require('../dist/tools/recall.js');
    const { handleForget } = require('../dist/tools/forget.js');
    const { handleExport } = require('../dist/tools/export.js');
    const { handleImport } = require('../dist/tools/import.js');

    const handlerMap = {
      totalreclaw_remember: handleRemember,
      totalreclaw_recall: handleRecall,
      totalreclaw_forget: handleForget,
      totalreclaw_export: handleExport,
      totalreclaw_import: handleImport,
    };

    it('every tool in ListTools has a matching handler', () => {
      for (const tool of ALL_TOOL_DEFINITIONS) {
        expect(handlerMap[tool.name]).toBeDefined();
        expect(typeof handlerMap[tool.name]).toBe('function');
      }
    });

    it('unknown tool name is not in the handler map', () => {
      expect(handlerMap['totalreclaw_unknown']).toBeUndefined();
    });
  });

  describe('tool schema details', () => {
    it('recall requires query field', () => {
      expect(recallToolDefinition.inputSchema.required).toContain('query');
    });

    it('import requires content field', () => {
      expect(importToolDefinition.inputSchema.required).toContain('content');
    });

    it('remember does not strictly require any single field (fact OR facts)', () => {
      // Neither fact nor facts is in required -- the handler validates the union
      const required = rememberToolDefinition.inputSchema.required;
      expect(required).toBeUndefined();
    });

    it('forget does not require any single field (fact_id OR query)', () => {
      const required = forgetToolDefinition.inputSchema.required;
      expect(required).toBeUndefined();
    });

    it('remember.importance has min 1 and max 10', () => {
      const impProp = rememberToolDefinition.inputSchema.properties.importance;
      expect(impProp.minimum).toBe(1);
      expect(impProp.maximum).toBe(10);
    });

    it('recall.k has default of 16', () => {
      const kProp = recallToolDefinition.inputSchema.properties.k;
      expect(kProp.default).toBe(16);
    });

    it('export.format enum contains markdown and json', () => {
      const formatProp = exportToolDefinition.inputSchema.properties.format;
      expect(formatProp.enum).toContain('markdown');
      expect(formatProp.enum).toContain('json');
    });

    it('import.merge_strategy enum contains all three strategies', () => {
      const msProp = importToolDefinition.inputSchema.properties.merge_strategy;
      expect(msProp.enum).toContain('skip_existing');
      expect(msProp.enum).toContain('overwrite');
      expect(msProp.enum).toContain('merge');
    });
  });
});
