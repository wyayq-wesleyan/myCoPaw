/**
 * Chat reference extraction.
 *
 * The assistant's "参考资料" footer is built from structured metadata attached
 * to knowledge/data tool results. Older persisted turns predate that metadata,
 * so the extractor also understands the human-readable tool output formats:
 *
 *   [1] 文件.pdf（3 个相关片段...）
 *   文档：文件.pdf（共 N 个片段）
 *
 * The fallback is intentionally narrow; it only runs for the two retrieval
 * tools, never for arbitrary tool text.
 */

export type ChatReferenceKind = "knowledge" | "data_table" | string;

export interface ChatReference {
  kind: ChatReferenceKind;
  id?: string;
  name: string;
  space?: string;
}

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function nonEmpty(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizeReference(value: unknown): ChatReference | null {
  const record = asRecord(value);
  if (!record) return null;
  const name = nonEmpty(record.name);
  if (!name) return null;
  const kind = nonEmpty(record.kind) || "knowledge";
  const id = nonEmpty(record.id);
  const space = nonEmpty(record.space);
  return {
    kind,
    name,
    ...(id ? { id } : {}),
    ...(space ? { space } : {}),
  };
}

function referenceKey(reference: ChatReference): string {
  return `${reference.kind}:${reference.id || reference.name}`;
}

function collectMetadataReferences(
  metadata: unknown,
  add: (reference: ChatReference) => void,
): void {
  const record = asRecord(metadata);
  if (!record) return;
  for (const item of asArray(record.references)) {
    const reference = normalizeReference(item);
    if (reference) add(reference);
  }
}

function parseKnowledgeText(text: string): ChatReference[] {
  const references: ChatReference[] = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    // get_document_detail: 文档：采购合同.pdf（共 12 个片段）
    const detail = line.match(/^文档：(.+?)(?:（|$)/);
    if (detail?.[1]) {
      references.push({ kind: "knowledge", name: detail[1].trim() });
      continue;
    }

    // query_knowledge: [1] 采购合同.pdf（3 个相关片段，涉及页码：2）
    const search = line.match(/^\[\d+\]\s+(.+?)(?:（|$)/);
    if (search?.[1]) {
      references.push({ kind: "knowledge", name: search[1].trim() });
    }
  }
  return references;
}

const SQL_TABLE_RE = /\b(?:FROM|JOIN)\s+([`"[]?[\w.]+[`"\]]?)/gi;
const SQL_CTE_RE = /(?:\bWITH\b|,)\s*([`"[]?[\w]+[`"\]]?)\s+AS\s*\(/gi;

function parseSqlTableNames(sql: string): ChatReference[] {
  const cteNames = new Set<string>();
  for (const match of sql.matchAll(SQL_CTE_RE)) {
    cteNames.add(match[1].replace(/[`"[\]]/g, "").toLowerCase());
  }

  const references: ChatReference[] = [];
  const seen = new Set<string>();
  for (const match of sql.matchAll(SQL_TABLE_RE)) {
    const raw = match[1].replace(/[`"[\]]/g, "").trim();
    const name = raw.split(".").pop()?.replace(/[`"[\]]/g, "").trim() ?? "";
    const key = name.toLowerCase();
    if (!name || cteNames.has(key) || seen.has(key)) continue;
    seen.add(key);
    references.push({ kind: "data_table", name });
  }
  return references;
}

function sqlFromArguments(value: unknown): string {
  let parsed: unknown = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      return "";
    }
  }
  const record = asRecord(parsed);
  return nonEmpty(record?.sql);
}

/**
 * Extract de-duplicated references from one assistant response's runtime
 * output. Unknown tools are ignored; only query_knowledge,
 * get_document_detail and query_data_table can contribute.
 */
export function extractReferences(output: unknown): ChatReference[] {
  const references: ChatReference[] = [];
  const seen = new Set<string>();
  const add = (reference: ChatReference) => {
    // Metadata is authoritative; the text fallback repeats the same filename
    // without an id. Treat either form as the same reference so the footer
    // does not list the document twice.
    const keys = [
      referenceKey(reference),
      `${reference.kind}:${reference.name}`,
    ];
    if (keys.some((key) => seen.has(key))) return;
    for (const key of keys) seen.add(key);
    references.push(reference);
  };

  for (const rawMessage of asArray(output)) {
    const message = asRecord(rawMessage);
    if (!message) continue;

    collectMetadataReferences(message.metadata, add);

    for (const rawBlock of asArray(message.content)) {
      const block = asRecord(rawBlock);
      if (!block) continue;
      collectMetadataReferences(block.metadata, add);

      const data = asRecord(block.data);
      if (!data) continue;

      collectMetadataReferences(data.metadata, add);

      const toolName = nonEmpty(data.name);
      if (toolName === "query_knowledge" || toolName === "get_document_detail") {
        const text = nonEmpty(data.output);
        if (text) {
          for (const reference of parseKnowledgeText(text)) add(reference);
        }
      }

      if (toolName === "query_data_table") {
        const sql =
          sqlFromArguments(data.arguments) || sqlFromArguments(data.input);
        if (sql) {
          for (const reference of parseSqlTableNames(sql)) add(reference);
        }
      }
    }
  }

  return references;
}

/** The first three are shown by default; the rest are behind 更多. */
export const REFERENCE_PREVIEW_COUNT = 3;

export function visibleReferences(
  references: ChatReference[],
  expanded: boolean,
): ChatReference[] {
  return expanded
    ? references
    : references.slice(0, REFERENCE_PREVIEW_COUNT);
}
