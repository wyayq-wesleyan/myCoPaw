import {
  DownloadOutlined,
  FileExcelOutlined,
  FileImageOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FileWordOutlined,
} from "@ant-design/icons";
import { Button, Drawer, Tag, message } from "antd";

export interface PreviewFile {
  name: string;
  size?: string;
  updatedAt?: string;
}

interface FilePreviewDrawerProps {
  file: PreviewFile | null;
  onClose: () => void;
}

type PreviewKind =
  | "image"
  | "pdf"
  | "markdown"
  | "csv"
  | "word"
  | "excel"
  | "json"
  | "text"
  | "unknown";

function previewKind(name: string): PreviewKind {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext)) {
    return "image";
  }
  if (ext === "pdf") return "pdf";
  if (ext === "md" || ext === "mdx") return "markdown";
  if (ext === "csv") return "csv";
  if (ext === "doc" || ext === "docx") return "word";
  if (ext === "xls" || ext === "xlsx") return "excel";
  if (ext === "json") return "json";
  if (["txt", "log", "xml", "yaml", "yml"].includes(ext)) return "text";
  return "unknown";
}

function kindIcon(kind: PreviewKind) {
  if (kind === "image") return <FileImageOutlined />;
  if (kind === "pdf") return <FilePdfOutlined />;
  if (kind === "word") return <FileWordOutlined />;
  if (kind === "excel" || kind === "csv") return <FileExcelOutlined />;
  return <FileTextOutlined />;
}

const kindLabels: Record<PreviewKind, string> = {
  image: "图片",
  pdf: "PDF 文档",
  markdown: "Markdown 文档",
  csv: "CSV 表格",
  word: "Word 文档",
  excel: "Excel 表格",
  json: "JSON 文件",
  text: "文本文件",
  unknown: "二进制文件",
};

const sampleCsv = [
  ["序号", "单位", "姓名", "完成时限", "状态"],
  ["1", "情报研判中心", "张三", "11-18", "进行中"],
  ["2", "反诈中心", "李四", "11-20", "待开始"],
  ["3", "经侦支队", "王五", "11-22", "已完成"],
];

function PreviewBody({ file, kind }: { file: PreviewFile; kind: PreviewKind }) {
  if (kind === "image") {
    return (
      <div className="dl-preview-image">
        <div className="dl-preview-image-frame">
          <FileImageOutlined />
          <span>图片预览区域</span>
          <small>接入后由文件接口返回图片流，按原始比例缩放</small>
        </div>
      </div>
    );
  }

  if (kind === "pdf") {
    return (
      <div className="dl-preview-pdf">
        <div className="dl-preview-pdf-page">
          <div className="dl-preview-pdf-head">{file.name}</div>
          <div className="dl-preview-pdf-line" style={{ width: "72%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "90%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "84%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "60%" }} />
          <div className="dl-preview-pdf-gap" />
          <div className="dl-preview-pdf-line" style={{ width: "88%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "76%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "92%" }} />
          <div className="dl-preview-pdf-line" style={{ width: "54%" }} />
        </div>
        <span className="dl-preview-pager">第 1 / 6 页</span>
      </div>
    );
  }

  if (kind === "markdown") {
    return (
      <article className="dl-preview-markdown">
        <h1>信息核查工作规范</h1>
        <p>
          本文档用于规范信息核查流程。核查前应先确认目标是否属于当前案件在控目标，
          查询权限由查询中心统一判定。
        </p>
        <h2>一、核查步骤</h2>
        <ol>
          <li>解析用户输入的目标信息；</li>
          <li>逐目标执行权限校验；</li>
          <li>按查询批次返回结果或审批需求。</li>
        </ol>
        <h2>二、结果要求</h2>
        <blockquote>每条结论必须标注证据来源，不得展示无权限数据。</blockquote>
        <pre>
          <code>{`{
  "target": "139****0001",
  "status": "controlled"
}`}</code>
        </pre>
      </article>
    );
  }

  if (kind === "csv" || kind === "excel") {
    return (
      <div className="dl-preview-sheet">
        <div className="dl-preview-sheet-tabs">
          <span className="dl-preview-sheet-tab-active">Sheet1</span>
          <span>Sheet2</span>
        </div>
        <table className="dl-preview-table">
          <thead>
            <tr>
              {sampleCsv[0].map((cell) => (
                <th key={cell}>{cell}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sampleCsv.slice(1).map((row) => (
              <tr key={row.join("-")}>
                {row.map((cell, index) => (
                  <td key={`${cell}-${index}`}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="dl-preview-sheet-foot">
          共 3 行 · 5 列 · 原型仅展示前几行
        </div>
      </div>
    );
  }

  if (kind === "word") {
    return (
      <article className="dl-preview-word">
        <h1>{file.name.replace(/\.(docx?|DOCX?)$/, "")}</h1>
        <p className="dl-preview-word-meta">情报研判中心 · 2026 年 11 月</p>
        <p>
          一、总体要求。各单位要按照统一部署，明确责任分工，确保核查工作规范、
          有序开展。
        </p>
        <p>
          二、工作步骤。受理目标后，先完成目标解析和权限校验，再按批次执行查询；
          涉及非在控目标的，按规定发起审批。
        </p>
        <p>三、结果运用。核查结果统一归档，作为后续研判和处置的依据。</p>
      </article>
    );
  }

  if (kind === "json") {
    return (
      <pre className="dl-preview-code">
        {`{
  "targetType": "mobile",
  "target": "139****0001",
  "controlled": true,
  "source": "案件在控目标",
  "result": {
    "name": "陈某某",
    "carrier": "中国移动",
    "lastActive": "2026-11-16 08:12"
  }
}`}
      </pre>
    );
  }

  if (kind === "text") {
    return (
      <pre className="dl-preview-code">
        {`回执识别结果
------------------------------
识别时间：2026-11-16 10:44
文件类型：扫描件
识别字段：12 项
置信度：98.2%
处理状态：成功`}
      </pre>
    );
  }

  return (
    <div className="dl-preview-unknown">
      <FileTextOutlined />
      <strong>该类型暂不支持在线预览</strong>
      <span>可以下载后用本地程序打开。</span>
    </div>
  );
}

export function FilePreviewDrawer({ file, onClose }: FilePreviewDrawerProps) {
  const kind = file ? previewKind(file.name) : "unknown";

  return (
    <Drawer
      open={Boolean(file)}
      onClose={onClose}
      width={760}
      className="dl-preview-drawer"
      title={
        file ? (
          <div className="dl-preview-title">
            <span className={`dl-preview-title-icon dl-preview-title-${kind}`}>
              {kindIcon(kind)}
            </span>
            <div>
              <strong>{file.name}</strong>
              <span>
                {kindLabels[kind]}
                {file.size ? ` · ${file.size}` : ""}
                {file.updatedAt ? ` · ${file.updatedAt}` : ""}
              </span>
            </div>
          </div>
        ) : null
      }
      extra={
        <div className="dl-preview-actions">
          <Tag>只读预览</Tag>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => message.info("原型：下载原文件")}
          >
            下载
          </Button>
        </div>
      }
    >
      {file ? (
        <div className="dl-preview-body">
          <PreviewBody file={file} kind={kind} />
        </div>
      ) : null}
    </Drawer>
  );
}

export default FilePreviewDrawer;
