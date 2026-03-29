import { useMemo } from "react";
import { Alert, Button, Collapse, Space, Typography } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getApiUrl } from "../../../api/config";
import { chatApi } from "../../../api/modules/chat";
import { toStoredName } from "../utils";

type ParsedToolBlock = {
  type: "text" | "file" | "image" | "audio" | "video";
  text?: string;
  url?: string;
  filename?: string;
};

type ToolPayload = {
  data?: {
    arguments?: string;
    output?: string;
  };
};

function normalizeToolUrl(value: string): string {
  if (!value) return "";
  if (value.startsWith("http://") || value.startsWith("https://")) {
    return value;
  }
  if (value.startsWith("/api/")) {
    return getApiUrl(value);
  }
  return chatApi.fileUrl(toStoredName(value));
}

function rewriteMarkdownLinks(text: string): string {
  return text.replace(/\]\((\/api\/[^)]+)\)/g, (_match, url: string) => {
    return `](${getApiUrl(url)})`;
  });
}

function parseToolOutput(output?: string): ParsedToolBlock[] {
  if (!output) return [];

  try {
    const parsed = JSON.parse(output);
    const items = Array.isArray(parsed) ? parsed : [parsed];

    return items.flatMap((item): ParsedToolBlock[] => {
      if (!item || typeof item !== "object") {
        return [];
      }

      if (item.type === "text" && typeof item.text === "string") {
        return [{ type: "text", text: rewriteMarkdownLinks(item.text) }];
      }

      const sourceUrl =
        item.source?.url ||
        item.file_url ||
        item.image_url ||
        item.audio_url ||
        item.video_url ||
        item.data;

      if (
        (item.type === "file" ||
          item.type === "image" ||
          item.type === "audio" ||
          item.type === "video") &&
        typeof sourceUrl === "string"
      ) {
        return [
          {
            type: item.type,
            url: normalizeToolUrl(sourceUrl),
            filename:
              item.filename ||
              item.file_name ||
              toStoredName(sourceUrl) ||
              "file",
          },
        ];
      }

      return [];
    });
  } catch {
    return [{ type: "text", text: output }];
  }
}

function getActionLabel(block: ParsedToolBlock): string {
  if (block.type === "image") return "打开图片";
  if (block.type === "audio") return "下载音频";
  if (block.type === "video") return "下载视频";
  return "下载文件";
}

export function SendFileToolCard({ data }: { data: { content?: ToolPayload[] } }) {
  const payloads = Array.isArray(data?.content) ? data.content : [];
  const input = payloads[0]?.data?.arguments || "";
  const output = payloads[1]?.data?.output || "";

  const blocks = useMemo(() => parseToolOutput(output), [output]);
  const textBlocks = blocks.filter((block) => block.type === "text");
  const downloadBlocks = blocks.filter((block) => block.url);
  const hasError = textBlocks.some((block) =>
    (block.text || "").toLowerCase().includes("error:"),
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        style={{
          border: "1px solid rgba(22, 119, 255, 0.18)",
          borderRadius: 12,
          padding: 16,
          background: "rgba(22, 119, 255, 0.04)",
        }}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Typography.Text strong>
            {downloadBlocks.length > 0 ? "文件已准备好，可直接下载" : "文件发送结果"}
          </Typography.Text>

          {downloadBlocks.length > 0 ? (
            <Space wrap>
              {downloadBlocks.map((block, index) => (
                <Button
                  key={`${block.filename || "file"}-${index}`}
                  type="primary"
                  href={block.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {getActionLabel(block)}
                  {block.filename ? `: ${block.filename}` : ""}
                </Button>
              ))}
            </Space>
          ) : null}

          {textBlocks.map((block, index) =>
            hasError ? (
              <Alert
                key={`text-${index}`}
                type="error"
                showIcon
                message={block.text}
              />
            ) : (
              <div
                key={`text-${index}`}
                style={{ color: "rgba(0, 0, 0, 0.85)", lineHeight: 1.7 }}
              >
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ node: _node, ...props }) => (
                      <a {...props} target="_blank" rel="noreferrer" />
                    ),
                  }}
                >
                  {block.text || ""}
                </ReactMarkdown>
              </div>
            ),
          )}

          {!blocks.length && output ? (
            <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>
              {output}
            </Typography.Paragraph>
          ) : null}
        </Space>
      </div>

      <Collapse
        size="small"
        items={[
          {
            key: "details",
            label: "查看工具详情",
            children: (
              <pre
                style={{
                  margin: 0,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  fontSize: 12,
                }}
              >
                {`arguments:\n${input || "(empty)"}\n\noutput:\n${output || "(empty)"}`}
              </pre>
            ),
          },
        ]}
      />
    </div>
  );
}
