import { describe, expect, it } from "vitest";

import { extractReferences, visibleReferences } from "./references";

function toolMessage(
  type: string,
  data: Record<string, unknown>,
) {
  return {
    type,
    role: type.endsWith("_output") ? "tool" : "assistant",
    content: [{ type: "data", data }],
  };
}

describe("extractReferences", () => {
  it("reads structured references from tool metadata", () => {
    const output = [
      toolMessage("plugin_call_output", {
        call_id: "call-1",
        name: "query_knowledge",
        output: "",
        metadata: {
          references: [
            { kind: "knowledge", id: "doc-1", name: "采购合同.pdf" },
            { kind: "knowledge", id: "doc-2", name: "验收单.docx" },
          ],
        },
      }),
    ];

    expect(extractReferences(output)).toEqual([
      { kind: "knowledge", id: "doc-1", name: "采购合同.pdf" },
      { kind: "knowledge", id: "doc-2", name: "验收单.docx" },
    ]);
  });

  it("falls back to the human-readable knowledge result for old turns", () => {
    const output = [
      toolMessage("plugin_call_output", {
        call_id: "call-1",
        name: "query_knowledge",
        output: [
          "知识空间：默认",
          "找到 2 个相关文档，共 3 个相关片段：",
          "[1] 采购合同.pdf（2 个相关片段，涉及页码：3）",
          "    文档ID: doc-1",
          "[2] 验收单.docx（1 个相关片段）",
        ].join("\n"),
      }),
    ];

    expect(extractReferences(output)).toEqual([
      { kind: "knowledge", name: "采购合同.pdf" },
      { kind: "knowledge", name: "验收单.docx" },
    ]);
  });

  it("extracts table names from query_data_table SQL", () => {
    const output = [
      toolMessage("plugin_call", {
        call_id: "call-2",
        name: "query_data_table",
        arguments: JSON.stringify({
          sql: "WITH recent AS (SELECT * FROM tb_scores) SELECT * FROM recent JOIN tb_people p ON true",
        }),
      }),
    ];

    expect(extractReferences(output)).toEqual([
      { kind: "data_table", name: "tb_scores" },
      { kind: "data_table", name: "tb_people" },
    ]);
  });

  it("de-duplicates the same document reported by metadata and fallback", () => {
    const output = [
      toolMessage("plugin_call_output", {
        call_id: "call-1",
        name: "query_knowledge",
        output: "[1] 采购合同.pdf（2 个相关片段）",
        metadata: {
          references: [
            { kind: "knowledge", id: "doc-1", name: "采购合同.pdf" },
          ],
        },
      }),
    ];

    expect(extractReferences(output)).toEqual([
      { kind: "knowledge", id: "doc-1", name: "采购合同.pdf" },
    ]);
  });
});

describe("visibleReferences", () => {
  const references = Array.from({ length: 5 }, (_, index) => ({
    kind: "knowledge",
    name: `${index + 1}.pdf`,
  }));

  it("shows three by default and all when expanded", () => {
    expect(visibleReferences(references, false)).toHaveLength(3);
    expect(visibleReferences(references, true)).toHaveLength(5);
  });
});
