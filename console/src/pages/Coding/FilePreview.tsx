/**
 * FilePreview – renders a non-code file in the editor area.
 *
 * Supported types (auto-detected by extension):
 *   • image  – PNG / JPG / GIF / WebP / SVG / ICO / BMP
 *   • pdf    – inline <embed>
 *   • markdown – react-markdown with GFM
 *   • html   – read-only sandboxed document
 *   • csv    – parsed table
 */

import { ExternalLink, FileWarning, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { workspaceApi } from "../../api/modules/workspace";
import { buildAuthHeaders } from "../../api/authHeaders";
import { RenderableCodeBlock } from "../../components/RenderableCodeBlock";
import type { WorkspaceRoot } from "../../features/files-workspace/types";
import { ExternalMarkdownLink } from "../../components/Markdown/externalLinkComponents";
import { useAgentStore } from "../../stores/agentStore";
import { openHtmlFile } from "../../utils/openHtmlFile";
import { parseMarkdownFrontmatter } from "../../utils/markdown";
import styles from "./FilePreview.module.less";

// ---------------------------------------------------------------------------
// Type detection
// ---------------------------------------------------------------------------

const IMAGE_EXTS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  "ico",
  "bmp",
]);

export type PreviewType =
  | "image"
  | "pdf"
  | "markdown"
  | "html"
  | "csv"
  | "docx"
  | "xlsx"
  | "json"
  | "none";

export function getPreviewType(filePath: string): PreviewType {
  const ext = filePath.split(".").pop()?.toLowerCase() ?? "";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (ext === "md" || ext === "mdx") return "markdown";
  if (ext === "html" || ext === "htm") return "html";
  if (ext === "csv") return "csv";
  if (ext === "docx") return "docx";
  if (ext === "xlsx" || ext === "xlsm") return "xlsx";
  if (ext === "json") return "json";
  return "none";
}

export function isPreviewable(filePath: string): boolean {
  return getPreviewType(filePath) !== "none";
}

// ---------------------------------------------------------------------------
// CSV parser (no external dep)
// ---------------------------------------------------------------------------

function parseCsv(raw: string): string[][] {
  const lines = raw.trimEnd().split(/\r?\n/);
  return lines.map((line) => {
    const cells: string[] = [];
    let cur = "";
    let inQuote = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (ch === '"') {
        if (inQuote && line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQuote = !inQuote;
        }
      } else if (ch === "," && !inQuote) {
        cells.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    cells.push(cur);
    return cells;
  });
}

// ---------------------------------------------------------------------------
// Authenticated blob loader — browser-native <img>/<embed> won't send
// X-Agent-Id, so we fetch with headers and create an object URL.
//
// ---------------------------------------------------------------------------

function useAuthBlobUrl(
  filePath: string,
  chatId?: string,
  binaryUrl?: string,
  root?: WorkspaceRoot,
): {
  blobUrl: string | null;
  loading: boolean;
  failed: boolean;
} {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const selectedAgent = useAgentStore((state) => state.selectedAgent);

  useEffect(() => {
    let revoked = false;
    setLoading(true);
    setFailed(false);

    const loadBlob = async (): Promise<Blob | null> => {
      const url = binaryUrl ?? workspaceApi.getFileDownloadUrl(filePath, root);
      const res = await fetch(url, {
        headers: {
          ...buildAuthHeaders(),
          ...(chatId ? { "X-Chat-Id": chatId } : {}),
        },
      });
      if (!res.ok) throw new Error(`${res.status}`);
      return res.blob();
    };

    loadBlob()
      .then((blob) => {
        if (revoked || !blob) return;
        setBlobUrl(URL.createObjectURL(blob));
        setLoading(false);
      })
      .catch(() => {
        if (!revoked) {
          setBlobUrl(null);
          setLoading(false);
          setFailed(true);
        }
      });

    return () => {
      revoked = true;
      setBlobUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
    };
  }, [binaryUrl, chatId, filePath, root, selectedAgent]);

  return { blobUrl, loading, failed };
}

// ---------------------------------------------------------------------------
// Sub-renderers
// ---------------------------------------------------------------------------

function ImagePreview({
  filePath,
  chatId,
  binaryUrl,
  root,
}: {
  filePath: string;
  chatId?: string;
  binaryUrl?: string;
  root?: WorkspaceRoot;
}) {
  const { t } = useTranslation();
  const { blobUrl, loading, failed } = useAuthBlobUrl(
    filePath,
    chatId,
    binaryUrl,
    root,
  );
  if (loading) {
    return (
      <PreviewStatus icon={<LoaderCircle size={18} />} spinning>
        {t("common.loading")}
      </PreviewStatus>
    );
  }
  if (failed || !blobUrl) {
    return (
      <PreviewStatus icon={<FileWarning size={18} />}>
        {t("files.loadFailed")}
      </PreviewStatus>
    );
  }
  return (
    <div className={styles.imageWrap}>
      <img
        src={blobUrl}
        alt={filePath.split("/").pop()}
        className={styles.image}
      />
    </div>
  );
}

function PdfPreview({
  filePath,
  chatId,
  binaryUrl,
  root,
}: {
  filePath: string;
  chatId?: string;
  binaryUrl?: string;
  root?: WorkspaceRoot;
}) {
  const { t } = useTranslation();
  const { blobUrl, loading, failed } = useAuthBlobUrl(
    filePath,
    chatId,
    binaryUrl,
    root,
  );
  if (loading) {
    return (
      <PreviewStatus icon={<LoaderCircle size={18} />} spinning>
        {t("common.loading")}
      </PreviewStatus>
    );
  }
  if (failed || !blobUrl) {
    return (
      <PreviewStatus icon={<FileWarning size={18} />}>
        {t("files.loadFailed")}
      </PreviewStatus>
    );
  }
  return (
    <embed
      src={blobUrl}
      type="application/pdf"
      className={styles.pdfEmbed}
      title={filePath.split("/").pop()}
    />
  );
}

function PreviewStatus({
  children,
  icon,
  spinning = false,
}: {
  children: React.ReactNode;
  icon: React.ReactNode;
  spinning?: boolean;
}) {
  return (
    <div className={styles.previewStatus}>
      <span className={spinning ? styles.spinning : undefined}>{icon}</span>
      <span>{children}</span>
    </div>
  );
}

const markdownComponents = {
  a: ExternalMarkdownLink,
  pre({ children }: { children?: React.ReactNode }) {
    return <>{children}</>;
  },
  code({
    node,
    inline,
    className,
    children,
    ...rest
  }: React.ComponentPropsWithoutRef<"code"> & {
    node?: unknown;
    inline?: boolean;
  }) {
    void node;
    void inline;
    const match = /language-([\w-]+)/.exec(className || "");
    return (
      <RenderableCodeBlock
        {...rest}
        block={!!match}
        className={className}
        lang={match?.[1]}
      >
        {children}
      </RenderableCodeBlock>
    );
  },
};

function MarkdownPreview({ content }: { content: string }) {
  const { body, entries } = useMemo(
    () => parseMarkdownFrontmatter(content),
    [content],
  );

  return (
    <div className={styles.markdownWrap}>
      {entries.length > 0 && (
        <dl className={styles.frontmatter} aria-label="Front matter">
          {entries.map(({ key, value }, index) => (
            <div className={styles.frontmatterRow} key={`${key}:${index}`}>
              <dt className={styles.frontmatterKey}>{key}</dt>
              <dd className={styles.frontmatterValue}>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownComponents}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}

function buildReadOnlyHtml(content: string): string {
  const document = new DOMParser().parseFromString(content, "text/html");
  document
    .querySelectorAll("script, meta[http-equiv='refresh']")
    .forEach((element) => element.remove());
  document.querySelectorAll("*").forEach((element) => {
    Array.from(element.attributes).forEach((attribute) => {
      if (attribute.name.toLowerCase().startsWith("on")) {
        element.removeAttribute(attribute.name);
      }
    });
    element.removeAttribute("contenteditable");
  });
  document.querySelectorAll("a").forEach((element) => {
    element.removeAttribute("href");
    element.removeAttribute("target");
    element.removeAttribute("download");
  });
  document.querySelectorAll("form").forEach((element) => {
    element.removeAttribute("action");
    element.removeAttribute("method");
  });
  document
    .querySelectorAll("button, input, select, textarea")
    .forEach((element) => element.setAttribute("disabled", ""));

  const style = document.createElement("style");
  style.textContent = [
    "a, button, input, select, textarea, form, iframe, object, embed,",
    "audio, video, [contenteditable] { pointer-events: none !important; }",
  ].join(" ");
  document.head.appendChild(style);
  return `<!doctype html>\n${document.documentElement.outerHTML}`;
}

function HtmlPreview({
  content,
  filePath,
  chatId,
  projectDirOverride,
  root,
  workspaceBacked,
}: FilePreviewProps) {
  const { t } = useTranslation();
  const readOnlyHtml = useMemo(() => buildReadOnlyHtml(content), [content]);

  return (
    <div className={styles.htmlWrap}>
      <div className={styles.htmlToolbar}>
        <button
          type="button"
          className={styles.htmlOpenButton}
          onClick={() =>
            openHtmlFile({
              content,
              filePath,
              chatId,
              projectDirOverride,
              root,
              workspaceBacked,
            })
          }
        >
          <ExternalLink size={14} />
          {t("files.openHtmlInBrowser")}
        </button>
      </div>
      <iframe
        className={styles.htmlFrame}
        srcDoc={readOnlyHtml}
        sandbox=""
        tabIndex={-1}
        title={t("files.htmlPreview")}
      />
    </div>
  );
}

const MAX_CSV_ROWS = 500;
const MAX_CSV_COLS = 50;

function CsvPreview({ content }: { content: string }) {
  const rows = useMemo(() => parseCsv(content), [content]);
  const header = rows[0] ?? [];
  const body = rows.slice(1, MAX_CSV_ROWS + 1);
  const truncatedCols = header.length > MAX_CSV_COLS;
  const truncatedRows = rows.length - 1 > MAX_CSV_ROWS;

  return (
    <div className={styles.csvWrap}>
      {(truncatedCols || truncatedRows) && (
        <div className={styles.csvNote}>
          {truncatedRows &&
            `Showing first ${MAX_CSV_ROWS} of ${rows.length - 1} rows. `}
          {truncatedCols &&
            `Showing first ${MAX_CSV_COLS} of ${header.length} columns.`}
        </div>
      )}
      <div className={styles.csvScroll}>
        <table className={styles.csvTable}>
          <thead>
            <tr>
              {header.slice(0, MAX_CSV_COLS).map((h, i) => (
                <th key={`${i}:${h}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={`${ri}:${row.join("\u0000")}`}>
                {row.slice(0, MAX_CSV_COLS).map((cell, ci) => (
                  <td key={`${ci}:${cell}`}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DOCX preview – mammoth converts the blob to HTML (no external requests).
// ---------------------------------------------------------------------------

function DocxPreview({
  filePath,
  chatId,
  binaryUrl,
  root,
}: {
  filePath: string;
  chatId?: string;
  binaryUrl?: string;
  root?: WorkspaceRoot;
}) {
  const { t } = useTranslation();
  const { blobUrl, loading, failed } = useAuthBlobUrl(
    filePath,
    chatId,
    binaryUrl,
    root,
  );
  const [html, setHtml] = useState<string | null>(null);
  const [convertFailed, setConvertFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setHtml(null);
    setConvertFailed(false);
    if (!blobUrl) return;
    (async () => {
      try {
        const mammoth = await import("mammoth");
        const res = await fetch(blobUrl);
        const arrayBuffer = await res.arrayBuffer();
        const result = await mammoth.convertToHtml(
          { arrayBuffer },
          { convertImage: mammoth.images.imgElement(async (image) => {
              const buf = await image.readAsBase64String();
              return { src: `data:${image.contentType};base64,${buf}` };
            }),
          },
        );
        if (!cancelled) setHtml(result.value);
      } catch {
        if (!cancelled) setConvertFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [blobUrl]);

  if (loading) {
    return (
      <PreviewStatus icon={<LoaderCircle size={18} />} spinning>
        {t("common.loading")}
      </PreviewStatus>
    );
  }
  if (failed || convertFailed || !html) {
    return (
      <PreviewStatus icon={<FileWarning size={18} />}>
        {t("files.loadFailed")}
      </PreviewStatus>
    );
  }
  return (
    <iframe
      className={styles.htmlFrame}
      srcDoc={buildReadOnlyHtml(wrapDocxBody(html))}
      sandbox=""
      tabIndex={-1}
      title={filePath.split("/").pop()}
    />
  );
}

/** Wrap mammoth HTML fragment in a minimal styled document for the iframe. */
function wrapDocxBody(fragment: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
           margin: 24px; color: #24292f; line-height: 1.7; }
    h1,h2,h3,h4,h5,h6 { margin: 0.8em 0 0.4em; line-height: 1.35; }
    table { border-collapse: collapse; margin: 0.8em 0; max-width: 100%; }
    th, td { border: 1px solid rgba(128,128,128,.4); padding: 6px 10px; font-size: 13px; }
    img { max-width: 100%; height: auto; }
    a { pointer-events: none; color: inherit; }
  </style></head><body>${fragment}</body></html>`;
}

// ---------------------------------------------------------------------------
// XLSX preview – SheetJS renders sheets as tables (no external requests).
// ---------------------------------------------------------------------------

const MAX_XLSX_ROWS = 500;
const MAX_XLSX_COLS = 60;

function XlsxPreview({
  filePath,
  chatId,
  binaryUrl,
  root,
}: {
  filePath: string;
  chatId?: string;
  binaryUrl?: string;
  root?: WorkspaceRoot;
}) {
  const { t } = useTranslation();
  const { blobUrl, loading, failed } = useAuthBlobUrl(
    filePath,
    chatId,
    binaryUrl,
    root,
  );
  const [sheets, setSheets] = useState<
    { name: string; rows: string[][] }[] | null
  >(null);
  const [active, setActive] = useState(0);
  const [convertFailed, setConvertFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSheets(null);
    setConvertFailed(false);
    if (!blobUrl) return;
    (async () => {
      try {
        const XLSX = await import("xlsx");
        const res = await fetch(blobUrl);
        const arrayBuffer = await res.arrayBuffer();
        const wb = XLSX.read(arrayBuffer, { type: "array" });
        const parsed = wb.SheetNames.map((name) => {
          const sheet = wb.Sheets[name];
          const rows = XLSX.utils.sheet_to_json<string[]>(sheet, {
            header: 1,
            raw: false,
            defval: "",
          });
          return {
            name,
            rows: rows.map((row) => row.map((cell) => String(cell ?? ""))),
          };
        });
        if (!cancelled) setSheets(parsed);
      } catch {
        if (!cancelled) setConvertFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [blobUrl]);

  if (loading) {
    return (
      <PreviewStatus icon={<LoaderCircle size={18} />} spinning>
        {t("common.loading")}
      </PreviewStatus>
    );
  }
  if (failed || convertFailed || !sheets) {
    return (
      <PreviewStatus icon={<FileWarning size={18} />}>
        {t("files.loadFailed")}
      </PreviewStatus>
    );
  }
  const sheet = sheets[active] ?? sheets[0];
  const header = sheet.rows[0] ?? [];
  const body = sheet.rows.slice(1, MAX_XLSX_ROWS + 1);
  const truncated =
    sheet.rows.length - 1 > MAX_XLSX_ROWS || header.length > MAX_XLSX_COLS;

  return (
    <div className={styles.csvWrap}>
      {sheets.length > 1 && (
        <div className={styles.csvNote}>
          {sheets.map((s, i) => (
            <button
              key={s.name}
              type="button"
              onClick={() => setActive(i)}
              style={{
                marginRight: 8,
                fontWeight: i === active ? 600 : 400,
                cursor: "pointer",
              }}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      {truncated && (
        <div className={styles.csvNote}>
          {`Showing first ${MAX_XLSX_ROWS} rows / ${MAX_XLSX_COLS} columns.`}
        </div>
      )}
      <div className={styles.csvScroll}>
        <table className={styles.csvTable}>
          <thead>
            <tr>
              {header.slice(0, MAX_XLSX_COLS).map((h, i) => (
                <th key={`${i}:${h}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={`${ri}:${row.join("\u0000")}`}>
                {row.slice(0, MAX_XLSX_COLS).map((cell, ci) => (
                  <td key={`${ci}:${cell}`}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// JSON preview – pretty-printed read-only text.
// ---------------------------------------------------------------------------

function JsonPreview({ content }: { content: string }) {
  const pretty = useMemo(() => {
    try {
      return JSON.stringify(JSON.parse(content), null, 2);
    } catch {
      return content;
    }
  }, [content]);
  return <pre className={styles.textPreview}>{pretty}</pre>;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface FilePreviewProps {
  filePath: string;
  /** Text content – used by Markdown and CSV renderers. */
  content: string;
  chatId?: string;
  binaryUrl?: string;
  root?: WorkspaceRoot;
  projectDirOverride?: string;
  workspaceBacked?: boolean;
}

export default function FilePreview({
  filePath,
  content,
  chatId,
  binaryUrl,
  root,
  projectDirOverride,
  workspaceBacked,
}: FilePreviewProps) {
  const type = getPreviewType(filePath);

  if (type === "image") {
    return (
      <ImagePreview
        filePath={filePath}
        chatId={chatId}
        binaryUrl={binaryUrl}
        root={root}
      />
    );
  }
  if (type === "pdf") {
    return (
      <PdfPreview
        filePath={filePath}
        chatId={chatId}
        binaryUrl={binaryUrl}
        root={root}
      />
    );
  }
  if (type === "markdown") return <MarkdownPreview content={content} />;
  if (type === "html") {
    return (
      <HtmlPreview
        filePath={filePath}
        content={content}
        chatId={chatId}
        root={root}
        projectDirOverride={projectDirOverride}
        workspaceBacked={workspaceBacked}
      />
    );
  }
  if (type === "csv") return <CsvPreview content={content} />;
  if (type === "docx") {
    return (
      <DocxPreview
        filePath={filePath}
        chatId={chatId}
        binaryUrl={binaryUrl}
        root={root}
      />
    );
  }
  if (type === "xlsx") {
    return (
      <XlsxPreview
        filePath={filePath}
        chatId={chatId}
        binaryUrl={binaryUrl}
        root={root}
      />
    );
  }
  if (type === "json") return <JsonPreview content={content} />;
  return null;
}
