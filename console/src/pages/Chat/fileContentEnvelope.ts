/**
 * 折叠后端注入的「文件正文」封套。
 *
 * 上传文档时后端会把解析出的正文注入到那条消息里，用一对标记括起来，且**分语言**：
 *
 *   用户上传了文件：订单.csv
 *   --- 文件内容开始 ---
 *   <整份文件，可能几万行>
 *   --- 文件内容结束 ---
 *
 *    User uploaded file: orders.csv
 *   --- FILE CONTENT START ---
 *   <the whole file>
 *   --- FILE CONTENT END ---
 *
 * 这段文本有两个存在理由：模型要读它（这就是这个功能的意义），而且它被持久化了，
 * 所以后续轮次模型仍然看得到文件内容。**两个理由都不构成「展示它」的理由**：同一条
 * 消息里还有文件本身，控制台会把它渲染成卡片，而几万行正文会让整个会话没法用。
 *
 * 实时视图里看不到它是巧合——控制台渲染的是自己刚发出的那条消息，注入的那份只存在于
 * 服务端，重开会话才会出现。所以这个缺陷只在重载后可见。
 *
 * 正文**保留在历史里**（只是不展示）：把它从 transcript 里删掉的话，模型每轮都得
 * 重新读一遍文件。
 */
export interface InjectedFileContent {
  name: string;
  body: string;
}

interface EnvelopeSpec {
  header: RegExp;
  start: string;
  end: string;
}

const ENVELOPE_SPECS: EnvelopeSpec[] = [
  {
    header: /^用户上传了文件：(.+)$/,
    start: "--- 文件内容开始 ---",
    end: "--- 文件内容结束 ---",
  },
  {
    header: /^User uploaded file: (.+)$/,
    start: "--- FILE CONTENT START ---",
    end: "--- FILE CONTENT END ---",
  },
];

/**
 * 识别一段文本是不是注入的文件正文；是则返回文件名与正文。
 *
 * 要求头部行与两个标记都出现，宁可漏判不可误判：真正由用户手写的消息里出现
 * 「用户上传了文件：…」加一对完整标记的概率极低，而误判会**吞掉用户自己的内容**，
 * 那是不可接受的。
 */
export function parseInjectedFileContent(
  text: string,
): InjectedFileContent | null {
  if (!text || typeof text !== "string") return null;

  const lines = text.split("\n");
  // 头部在第一行（注入时总是如此），先按它挑出语言。
  const firstLine = (lines[0] ?? "").trim();
  const spec = ENVELOPE_SPECS.find((s) => s.header.test(firstLine));
  if (!spec) return null;

  const name = firstLine.match(spec.header)?.[1]?.trim() ?? "";
  if (!name) return null;

  const startIndex = lines.findIndex((line) => line.trim() === spec.start);
  if (startIndex < 0) return null;
  const endIndex = lines.findIndex(
    (line, index) => index > startIndex && line.trim() === spec.end,
  );
  if (endIndex < 0) return null;

  return { name, body: lines.slice(startIndex + 1, endIndex).join("\n") };
}

/**
 * 把内容项列表里的注入正文折叠掉。
 *
 * 同一个文件在一条消息里有两份表示：文件本身（渲染成卡片，可下载、可预览）和这段
 * 正文。**同一条消息里已经有文件项时直接丢弃正文**，只留卡片；没有文件项时（历史里
 * 文件块丢失的老消息）退化成一张只有文件名的卡片，也比把正文摊开强。
 */
export function collapseInjectedFileContentParts<
  T extends { type: string; [key: string]: unknown },
>(parts: T[]): T[] {
  const hasFilePart = parts.some((part) => part.type === "file");
  const out: T[] = [];
  for (const part of parts) {
    if (part.type !== "text" || typeof part.text !== "string") {
      out.push(part);
      continue;
    }
    const envelope = parseInjectedFileContent(part.text);
    if (!envelope) {
      out.push(part);
      continue;
    }
    if (hasFilePart) continue;
    // The fallback card is a plain shape, not a `T` — the constraint only exists
    // to keep the pass-through branches typed, so go through `unknown`.
    out.push({ type: "file", file_name: envelope.name } as unknown as T);
  }
  return out;
}
