import { useCallback, useEffect, useRef, useState } from "react";
import {
  Badge,
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
  Statistic,
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
  InfoCircleOutlined,
  ReloadOutlined,
  RetweetOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import {
  knowledgeApi,
  type KnowledgeDocument,
  type KnowledgeDocumentStats,
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

function getFileExtension(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() || "";
}

function formatFileSize(bytes?: number | null): string {
  if (bytes == null) return "-";
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* ── Preview Components ────────────────────────────────────────── */

function PdfPreview({ documentId }: { documentId: string }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const startedGuard = useRef(false);

  const callbackRef = useCallback((node: HTMLDivElement | null) => {
    if (node && !startedGuard.current) {
      startedGuard.current = true;
      void renderPdf(node);
    }
  }, [documentId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function renderPdf(container: HTMLDivElement) {
    const controller = new AbortController();
    const watchdog = window.setTimeout(() => {
      controller.abort();
      setError("PDF 加载超时，请下载后查看");
      setLoading(false);
    }, 120000);
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

      const contentLength = Number(response.headers.get("content-length") || 0);
      if (contentLength > 50 * 1024 * 1024) {
        setError("PDF 文件过大（>50MB），请下载后查看");
        setLoading(false);
        return;
      }

      const arrayBuffer = await response.arrayBuffer();
      const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;

      container.innerHTML = "";
      // render first page synchronously then clear the spinner
      const maxPages = Math.min(pdf.numPages, 50);
      for (let i = 1; i <= maxPages; i++) {
        const page = await pdf.getPage(i);
        const scale = 1.2;
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
        container.appendChild(canvas);
        if (i === 1) setLoading(false);
      }

      if (maxPages < pdf.numPages) {
        const hint = document.createElement("div");
        hint.style.cssText = "text-align:center;padding:16px;color:#999;font-size:13px;";
        hint.textContent = `（仅显示前 ${maxPages} 页，共 ${pdf.numPages} 页）`;
        container.appendChild(hint);
      }

      setLoading(false);
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : "PDF loading failed");
        setLoading(false);
      }
    } finally {
      window.clearTimeout(watchdog);
    }
  }

  if (error) return <Typography.Text type="danger">PDF 加载失败: {error}</Typography.Text>;
  return (
    <div style={{ position: "relative" }}>
      <div ref={callbackRef} className={styles.pdfPreview} />
      {loading && (
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(255,255,255,0.7)", borderRadius: 8 }}>
          <Spin size="large" tip="正在解析 PDF..." />
        </div>
      )}
    </div>
  );
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

/* ── Stats Drawer ──────────────────────────────────────────────── */

function StatsDrawer({ documentId, filename, open, onClose }: {
  documentId: string;
  filename: string;
  open: boolean;
  onClose: () => void;
}) {
  const [stats, setStats] = useState<KnowledgeDocumentStats | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !documentId) return;
    setLoading(true);
    knowledgeApi.getDocumentStats(documentId)
      .then(setStats)
      .catch(() => setStats(null))
      .finally(() => setLoading(false));
  }, [open, documentId]);

  return (
    <Drawer title={`详情：${filename}`} placement="right" width={400} open={open} onClose={onClose} destroyOnClose>
      {loading ? (
        <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>
      ) : stats ? (
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Row gutter={[16, 16]}>
            <Col span={12}><Statistic title="文件大小" value={formatFileSize(stats.size)} /></Col>
            <Col span={12}><Statistic title="状态" value={statusMeta[stats.status as KnowledgeDocument["status"]]?.label || stats.status} /></Col>
            <Col span={12}><Statistic title="分片数" value={stats.chunk_count} /></Col>
            <Col span={12}><Statistic title="实体数" value={stats.entity_count} /></Col>
            <Col span={12}><Statistic title="关系数" value={stats.relation_count} /></Col>
          </Row>
        </Space>
      ) : (
        <Typography.Text type="secondary">无法加载统计信息</Typography.Text>
      )}
    </Drawer>
  );
}

/* ── Main Page ─────────────────────────────────────────────────── */

export default function KnowledgePage() {
  const [loading, setLoading] = useState(true);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [uploading, setUploading] = useState(false);
  const [reprocessingId, setReprocessingId] = useState<string>();
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [filenameFilter, setFilenameFilter] = useState("");
  const [keywordFilter, setKeywordFilter] = useState("");
  const [querying, setQuerying] = useState(false);

  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewDocument, setPreviewDocument] = useState<KnowledgeDocument | null>(null);

  const [statsOpen, setStatsOpen] = useState(false);
  const [statsDocument, setStatsDocument] = useState<KnowledgeDocument | null>(null);

  const activeJobCount = documents.filter((d) => d.status === "queued" || d.status === "processing").length;

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

  const batchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const result = await knowledgeApi.deleteDocuments(selectedRowKeys as string[]);
      setSelectedRowKeys([]);
      await loadDocuments();
      message.success(`已删除 ${result.deleted} 个文件`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "批量删除失败");
    }
  };

  /* ── Preview ───────────────────────────────────────────────── */

  const openPreview = (doc: KnowledgeDocument) => {
    if (doc.status !== "completed") {
      message.warning("文档尚未处理完成，无法预览");
      return;
    }
    setPreviewDocument(doc);
    setPreviewOpen(true);
  };

  /* ── Stats ─────────────────────────────────────────────────── */

  const openStats = (doc: KnowledgeDocument) => {
    setStatsDocument(doc);
    setStatsOpen(true);
  };

  /* ── Table Columns ─────────────────────────────────────────── */

  const columns: ColumnsType<KnowledgeDocument> = [
    {
      title: "序号",
      key: "index",
      width: 60,
      render: (_: unknown, __: KnowledgeDocument, index: number) => index + 1,
    },
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
      title: "大小",
      dataIndex: "size",
      key: "size",
      width: 100,
      render: (val: number) => formatFileSize(val),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: KnowledgeDocument["status"]) => {
        const meta = statusMeta[status];
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    {
      title: "上传时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (val: string) => val ? dayjs(val).format("YYYY-MM-DD HH:mm") : "-",
    },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_: unknown, record: KnowledgeDocument) => (
        <Space size={4}>
          <Tooltip title="详情">
            <Button type="text" size="small" icon={<InfoCircleOutlined />} onClick={() => openStats(record)} />
          </Tooltip>
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
              {selectedRowKeys.length > 0 && (
                <Popconfirm
                  title={`确定删除选中的 ${selectedRowKeys.length} 个文件？`}
                  onConfirm={batchDelete}
                  okText="删除"
                  cancelText="取消"
                >
                  <Button danger icon={<DeleteOutlined />}>
                    批量删除 ({selectedRowKeys.length})
                  </Button>
                </Popconfirm>
              )}
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
                <Badge count={activeJobCount} offset={[-5, 0]} size="small">
                  <Button type="primary" icon={<UploadOutlined />} loading={uploading}>
                    上传资料
                  </Button>
                </Badge>
              </Upload>
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
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
          }}
          locale={{ emptyText: <Empty description="暂无资料" /> }}
          pagination={{ pageSize: 20, showTotal: (total) => `共 ${total} 个文件` }}
          size="middle"
        />
      </div>

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

      {/* ── Stats Drawer ────────────────────────────────────────── */}
      {statsDocument && (
        <StatsDrawer
          documentId={statsDocument.id}
          filename={statsDocument.filename}
          open={statsOpen}
          onClose={() => { setStatsOpen(false); setStatsDocument(null); }}
        />
      )}
    </section>
  );
}
