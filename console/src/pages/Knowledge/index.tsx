import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  DatePicker,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Row,
  Col,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import {
  DatabaseOutlined,
  DeleteOutlined,
  FileSearchOutlined,
  FolderViewOutlined,
  ReloadOutlined,
  RetweetOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import {
  knowledgeApi,
  type KnowledgeDocument,
  type KnowledgeJobItem,
} from "../../api/modules/knowledge";
import styles from "./index.module.less";

const { RangePicker } = DatePicker;

const ACCEPTED_FILES = ".pdf,.doc,.docx,.xls,.xlsx,.xlsm,.csv,.txt,.md,.json";
const MAX_FILE_SIZE = 10 * 1024 * 1024;

const statusMeta: Record<KnowledgeDocument["status"], { color: string; label: string }> = {
  queued: { color: "default", label: "等待处理" },
  processing: { color: "processing", label: "正在解析" },
  completed: { color: "success", label: "已入库" },
  failed: { color: "error", label: "处理失败" },
};

const jobStatusMeta: Record<KnowledgeJobItem["status"], { color: string; label: string }> = {
  queued: { color: "default", label: "等待处理" },
  processing: { color: "processing", label: "正在解析" },
  completed: { color: "success", label: "已完成" },
  failed: { color: "error", label: "处理失败" },
};

function getFileExtension(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() || "";
}

/* ── Preview Components ────────────────────────────────────────── */

function PdfPreview({ documentId }: { documentId: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function renderPdf() {
      if (!containerRef.current) return;
      try {
        const pdfjsLib = await import("pdfjs-dist");
        pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();

        const url = knowledgeApi.getFileUrl(documentId);
        const headers = knowledgeApi.getFileHeaders();
        const response = await fetch(url, { headers, signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const arrayBuffer = await response.arrayBuffer();
        const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;

        if (cancelled) return;
        containerRef.current!.innerHTML = "";

        for (let i = 1; i <= pdf.numPages; i++) {
          if (cancelled) return;
          const page = await pdf.getPage(i);
          const scale = 1.5;
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          canvas.style.width = "100%";
          canvas.style.height = "auto";
          canvas.style.display = "block";
          canvas.style.marginBottom = "8px";
          canvas.style.borderRadius = "4px";
          const ctx = canvas.getContext("2d")!;
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          await page.render({ canvasContext: ctx, viewport, canvas } as any).promise;
          containerRef.current!.appendChild(canvas);
        }

        if (!cancelled) setLoading(false);
      } catch (err) {
        if (!cancelled && !controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "PDF loading failed");
          setLoading(false);
        }
      }
    }

    void renderPdf();
    return () => { cancelled = true; controller.abort(); };
  }, [documentId]);

  if (error) return <Typography.Text type="danger">PDF 加载失败: {error}</Typography.Text>;
  if (loading) return <div style={{ textAlign: "center", padding: "40px 0" }}><Spin size="large" /></div>;
  return <div ref={containerRef} className={styles.pdfPreview} />;
}

async function renderDocx(
  container: HTMLDivElement,
  documentId: string,
  setError: (msg: string | null) => void,
  setStarted: (v: boolean) => void,
) {
  try {
    const url = knowledgeApi.getFileUrl(documentId);
    const headers = knowledgeApi.getFileHeaders();
    const response = await fetch(url, { headers });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const arrayBuffer = await response.arrayBuffer();
    const mammoth = await import("mammoth");
    const result = await mammoth.convertToHtml(
      { arrayBuffer },
      { convertImage: mammoth.images.dataUri },
    );
    container.innerHTML = result.value || "<p>文档内容为空</p>";
    setStarted(true);
  } catch (err) {
    setError(err instanceof Error ? err.message : "DOCX loading failed");
  }
}

function DocxPreview({ documentId }: { documentId: string }) {
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const startedGuard = useRef(false);

  const callbackRef = useCallback((node: HTMLDivElement | null) => {
    if (node && !startedGuard.current) {
      startedGuard.current = true;
      void renderDocx(node, documentId, setError, setStarted);
    }
  }, [documentId]);

  if (error) return <Typography.Text type="danger">DOCX 加载失败: {error}</Typography.Text>;
  return (
    <div style={{ position: "relative" }}>
      <div ref={callbackRef} className={styles.docxPreview} />
      {!started && (
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(255,255,255,0.7)", borderRadius: 8 }}>
          <Spin size="large" tip="正在解析文档..." />
        </div>
      )}
    </div>
  );
}

function TextPreview({ documentId }: { documentId: string }) {
  const [content, setContent] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const url = knowledgeApi.getFileUrl(documentId);
    const headers = knowledgeApi.getFileHeaders();

    fetch(url, { headers, signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => { if (!cancelled) setContent(text); })
      .catch((err) => { if (!cancelled) setContent(`加载失败: ${err instanceof Error ? err.message : "unknown"}`); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; controller.abort(); };
  }, [documentId]);

  if (loading) return <div style={{ textAlign: "center", padding: "40px 0" }}><Spin size="large" /></div>;
  return <pre className={styles.textContent}>{content}</pre>;
}

function DocumentPreview({ documentId, filename }: { documentId: string; filename: string }) {
  const ext = getFileExtension(filename);
  if (ext === "pdf") return <PdfPreview documentId={documentId} />;
  if (ext === "docx" || ext === "doc") return <DocxPreview documentId={documentId} />;
  return <TextPreview documentId={documentId} />;
}

/* ── Main Page ─────────────────────────────────────────────────── */

export default function KnowledgePage() {
  const [loading, setLoading] = useState(true);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [uploading, setUploading] = useState(false);
  const [reprocessingId, setReprocessingId] = useState<string>();

  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [filenameFilter, setFilenameFilter] = useState("");
  const [keywordFilter, setKeywordFilter] = useState("");
  const [querying, setQuerying] = useState(false);

  const [jobsDrawerOpen, setJobsDrawerOpen] = useState(false);
  const [jobs, setJobs] = useState<KnowledgeJobItem[]>([]);
  const [jobsLoading, setJobsLoading] = useState(false);

  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewDocument, setPreviewDocument] = useState<KnowledgeDocument | null>(null);

  /* ── Data Loading ──────────────────────────────────────────── */

  const loadDocuments = async (params?: {
    filename?: string;
    created_after?: string;
    created_before?: string;
    doc_ids?: string;
  }) => {
    try {
      const result = await knowledgeApi.listDocuments(params);
      setDocuments(result);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "无法加载文档列表");
    }
  };

  const refresh = async () => {
    setLoading(true);
    try {
      const health = await knowledgeApi.health();
      setConfigured(health.configured);
      if (health.configured) await loadDocuments();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "知识服务连接失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Polling for active jobs ───────────────────────────────── */

  useEffect(() => {
    if (!documents.some((d) => d.status === "queued" || d.status === "processing")) return;
    const timer = window.setInterval(() => void loadDocuments(), 2500);
    return () => window.clearInterval(timer);
  }, [documents]);

  /* ── Query / Filter ────────────────────────────────────────── */

  const handleQuery = async () => {
    setQuerying(true);
    try {
      let docIds: string[] | undefined;

      if (keywordFilter.trim()) {
        const filterResult = await knowledgeApi.searchFilter(keywordFilter.trim());
        docIds = filterResult.document_ids;
        if (docIds.length === 0) {
          setDocuments([]);
          return;
        }
      }

      const params: {
        filename?: string;
        created_after?: string;
        created_before?: string;
        doc_ids?: string;
      } = {};

      if (filenameFilter.trim()) params.filename = filenameFilter.trim();
      if (dateRange && dateRange[0]) params.created_after = dateRange[0].startOf("day").toISOString();
      if (dateRange && dateRange[1]) params.created_before = dateRange[1].endOf("day").toISOString();
      if (docIds) params.doc_ids = docIds.join(",");

      await loadDocuments(params);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "检索失败");
    } finally {
      setQuerying(false);
    }
  };

  const handleReset = () => {
    setDateRange(null);
    setFilenameFilter("");
    setKeywordFilter("");
    void loadDocuments();
  };

  /* ── Upload ────────────────────────────────────────────────── */

  const handleUpload = async (file: File) => {
    if (file.size >= MAX_FILE_SIZE) {
      message.warning("文件大小不能超过10MB");
      return false;
    }
    setUploading(true);
    try {
      await knowledgeApi.upload(file);
      await loadDocuments();
      message.success("文件已上传，正在后台解析");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
    return false;
  };

  /* ── Jobs Drawer ───────────────────────────────────────────── */

  const loadJobs = async () => {
    setJobsLoading(true);
    try {
      setJobs(await knowledgeApi.listJobs());
    } catch {
      message.error("加载上传队列失败");
    } finally {
      setJobsLoading(false);
    }
  };

  const openJobsDrawer = () => {
    setJobsDrawerOpen(true);
    void loadJobs();
  };

  useEffect(() => {
    if (!jobsDrawerOpen) return;
    const hasActive = jobs.some((j) => j.status === "queued" || j.status === "processing");
    if (!hasActive) return;
    const timer = window.setInterval(() => void loadJobs(), 3000);
    return () => window.clearInterval(timer);
  }, [jobsDrawerOpen, jobs]);

  /* ── Preview ───────────────────────────────────────────────── */

  const openPreview = (doc: KnowledgeDocument) => {
    if (doc.status !== "completed") {
      message.warning("文档尚未处理完成，无法预览");
      return;
    }
    setPreviewDocument(doc);
    setPreviewOpen(true);
  };

  /* ── Reprocess ─────────────────────────────────────────────── */

  const reprocess = async (doc: KnowledgeDocument) => {
    setReprocessingId(doc.id);
    try {
      await knowledgeApi.reprocessDocument(doc.id);
      await loadDocuments();
      message.success("已加入重新处理队列");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重新处理失败");
    } finally {
      setReprocessingId(undefined);
    }
  };

  /* ── Delete ────────────────────────────────────────────────── */

  const deleteDocument = async (doc: KnowledgeDocument) => {
    try {
      await knowledgeApi.deleteDocument(doc.id);
      await loadDocuments();
      message.success("文件已删除");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  /* ── Table Columns ─────────────────────────────────────────── */

  const columns = [
    {
      title: "文件名",
      dataIndex: "filename",
      key: "filename",
      render: (text: string, record: KnowledgeDocument) => (
        <Typography.Link
          onClick={() => openPreview(record)}
          style={{ cursor: record.status === "completed" ? "pointer" : "default" }}
        >
          <FileSearchOutlined style={{ marginRight: 6 }} />
          {text}
        </Typography.Link>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: KnowledgeDocument["status"]) => {
        const meta = statusMeta[status];
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "上传时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (val: string) => val ? dayjs(val).format("YYYY-MM-DD HH:mm") : "-",
    },
    {
      title: "操作",
      key: "actions",
      width: 160,
      render: (_: unknown, record: KnowledgeDocument) => (
        <Space size={4}>
          <Tooltip title="按当前解析规则重新入库">
            <Button
              type="text"
              size="small"
              icon={<RetweetOutlined />}
              loading={reprocessingId === record.id}
              onClick={() => void reprocess(record)}
            />
          </Tooltip>
          <Popconfirm
            title="确定删除此文件？"
            onConfirm={() => void deleteDocument(record)}
            okText="删除"
            cancelText="取消"
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  /* ── Render ────────────────────────────────────────────────── */

  if (loading) {
    return <div className={styles.loading}><Spin size="large" /></div>;
  }

  if (configured === false) {
    return (
      <section className={styles.page}>
        <Typography.Paragraph type="secondary">
          知识服务尚未启用。管理员需要先部署独立知识服务，并为汇智小助手配置 QWENPAW_KNOWLEDGE_URL 与 QWENPAW_KNOWLEDGE_API_KEY。
        </Typography.Paragraph>
      </section>
    );
  }

  return (
    <section className={styles.page}>
      <header className={styles.hero}>
        <div className={styles.titleBlock}>
          <div className={styles.mark}><DatabaseOutlined /></div>
          <div>
            <Typography.Title level={3}>知识空间</Typography.Title>
            <Typography.Text>将业务资料变为可追溯、可关联、可供智能体检索的本地知识。</Typography.Text>
          </div>
        </div>
        <Tooltip title="刷新列表">
          <Button type="text" icon={<ReloadOutlined />} onClick={() => void refresh()} />
        </Tooltip>
      </header>

      {/* ── Query Conditions ────────────────────────────────────── */}
      <div className={styles.queryBar}>
        <Row gutter={[12, 12]} align="middle">
          <Col>
            <RangePicker
              value={dateRange as [dayjs.Dayjs | null, dayjs.Dayjs | null] | null}
              onChange={(dates) => setDateRange(dates as [dayjs.Dayjs | null, dayjs.Dayjs | null] | null)}
              placeholder={["开始日期", "结束日期"]}
            />
          </Col>
          <Col>
            <Input
              placeholder="文件名"
              value={filenameFilter}
              onChange={(e) => setFilenameFilter(e.target.value)}
              style={{ width: 160 }}
              allowClear
            />
          </Col>
          <Col>
            <Input
              placeholder="检索问答关键词"
              value={keywordFilter}
              onChange={(e) => setKeywordFilter(e.target.value)}
              style={{ width: 200 }}
              allowClear
              onPressEnter={() => void handleQuery()}
            />
          </Col>
          <Col>
            <Space>
              <Button type="primary" icon={<SearchOutlined />} loading={querying} onClick={() => void handleQuery()}>
                查询
              </Button>
              <Button onClick={handleReset}>重置</Button>
            </Space>
          </Col>
          <Col flex="auto" />
          <Col>
            <Space>
              <Upload
                accept={ACCEPTED_FILES}
                showUploadList={false}
                multiple
                beforeUpload={(_file, fileList) => {
                  if (fileList.length <= 1) return handleUpload(fileList[0]);
                  (async () => {
                    setUploading(true);
                    let success = 0;
                    let fail = 0;
                    for (const f of fileList) {
                      if (f.size >= MAX_FILE_SIZE) { fail++; continue; }
                      try { await knowledgeApi.upload(f); success++; } catch { fail++; }
                    }
                    await loadDocuments();
                    setUploading(false);
                    if (fail === 0) message.success(`${success} 个文件已上传`);
                    else message.warning(`${success} 个成功，${fail} 个失败`);
                  })();
                  return false;
                }}
                disabled={uploading}
              >
                <Button type="primary" icon={<UploadOutlined />} loading={uploading}>
                  上传资料
                </Button>
              </Upload>
              <Button icon={<FolderViewOutlined />} onClick={openJobsDrawer}>
                上传队列
              </Button>
            </Space>
          </Col>
        </Row>
        <div className={styles.hint}>支持 PDF、DOC/DOCX、XLS/XLSX、CSV、TXT、Markdown、JSON，单文件不超过 10MB。</div>
      </div>

      {/* ── Document Table ──────────────────────────────────────── */}
      <div className={styles.tableWrap}>
        <Table
          dataSource={documents}
          columns={columns}
          rowKey="id"
          locale={{ emptyText: <Empty description="暂无资料" /> }}
          pagination={{ pageSize: 20, showTotal: (total) => `共 ${total} 个文件` }}
          size="middle"
        />
      </div>

      {/* ── Upload Queue Drawer ─────────────────────────────────── */}
      <Drawer
        title="上传队列"
        placement="right"
        width={560}
        open={jobsDrawerOpen}
        onClose={() => setJobsDrawerOpen(false)}
        destroyOnClose
      >
        <Table
          dataSource={jobs}
          loading={jobsLoading}
          rowKey="id"
          pagination={false}
          size="small"
          columns={[
            { title: "文件名", dataIndex: "filename", key: "filename", ellipsis: true },
            {
              title: "状态",
              dataIndex: "status",
              key: "status",
              width: 100,
              render: (status: KnowledgeJobItem["status"]) => {
                const meta = jobStatusMeta[status];
                return <Tag color={meta.color}>{meta.label}</Tag>;
              },
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              key: "created_at",
              width: 160,
              render: (val: string) => val ? dayjs(val).format("YYYY-MM-DD HH:mm") : "-",
            },
            {
              title: "错误",
              dataIndex: "error",
              key: "error",
              ellipsis: true,
              render: (val: string | null) => val ? <Typography.Text type="danger">{val}</Typography.Text> : "-",
            },
          ]}
        />
      </Drawer>

      {/* ── Preview Drawer ──────────────────────────────────────── */}
      <Drawer
        title={previewDocument?.filename || "文档预览"}
        placement="left"
        width={720}
        open={previewOpen}
        onClose={() => { setPreviewOpen(false); setPreviewDocument(null); }}
        destroyOnClose
      >
        {previewDocument && (
          <DocumentPreview documentId={previewDocument.id} filename={previewDocument.filename} />
        )}
      </Drawer>
    </section>
  );
}
