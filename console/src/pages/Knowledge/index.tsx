import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Pagination,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import {
  DatabaseOutlined,
  FileSearchOutlined,
  FolderAddOutlined,
  ReloadOutlined,
  RetweetOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  knowledgeApi,
  type KnowledgeDocument,
  type KnowledgeSearchResult,
  type KnowledgeSpace,
} from "../../api/modules/knowledge";
import styles from "./index.module.less";

const ACCEPTED_FILES = ".pdf,.doc,.docx,.xls,.xlsx,.xlsm,.csv,.txt,.md";

const statusMeta: Record<KnowledgeDocument["status"], { color: string; label: string }> = {
  queued: { color: "default", label: "等待处理" },
  processing: { color: "processing", label: "正在解析" },
  completed: { color: "success", label: "已入库" },
  failed: { color: "error", label: "处理失败" },
};

function getFileExtension(filename: string): string {
  return filename.split(".").pop()?.toLowerCase() || "";
}

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

  if (ext === "pdf") {
    return <PdfPreview documentId={documentId} />;
  }
  if (ext === "docx" || ext === "doc") {
    return <DocxPreview documentId={documentId} />;
  }
  return <TextPreview documentId={documentId} />;
}

export default function KnowledgePage() {
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]);
  const [activeSpaceId, setActiveSpaceId] = useState<string>();
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [reprocessingId, setReprocessingId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [newSpaceName, setNewSpaceName] = useState("");
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewDocument, setPreviewDocument] = useState<KnowledgeDocument | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const loadSpaces = async (preferredId?: string) => {
    const nextSpaces = await knowledgeApi.listSpaces();
    setSpaces(nextSpaces);
    setActiveSpaceId((current) => {
      if (preferredId && nextSpaces.some((space) => space.id === preferredId)) return preferredId;
      if (current && nextSpaces.some((space) => space.id === current)) return current;
      return nextSpaces[0]?.id;
    });
  };

  const loadDocuments = async (spaceId = activeSpaceId) => {
    if (!spaceId) {
      setDocuments([]);
      return;
    }
    setDocuments(await knowledgeApi.listDocuments(spaceId));
  };

  const refresh = async () => {
    setLoading(true);
    try {
      const health = await knowledgeApi.health();
      setConfigured(health.configured);
      if (health.configured) await loadSpaces();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "知识服务连接失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    setCurrentPage(1);
    void loadDocuments().catch((error) => {
      message.error(error instanceof Error ? error.message : "无法加载文档列表");
    });
  }, [activeSpaceId]);

  useEffect(() => {
    if (!activeSpaceId || !documents.some((item) => item.status === "queued" || item.status === "processing")) {
      return undefined;
    }
    const timer = window.setInterval(() => void loadDocuments(), 2500);
    return () => window.clearInterval(timer);
  }, [activeSpaceId, documents]);

  const createSpace = async () => {
    const name = newSpaceName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const space = await knowledgeApi.createSpace(name);
      setNewSpaceName("");
      setModalOpen(false);
      await loadSpaces(space.id);
      message.success("知识空间已创建");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "创建知识空间失败");
    } finally {
      setCreating(false);
    }
  };

  const upload = async (file: File) => {
    if (!activeSpaceId) {
      message.warning("请先创建或选择一个知识空间");
      return false;
    }
    setUploading(true);
    try {
      await knowledgeApi.upload(activeSpaceId, file);
      await loadDocuments(activeSpaceId);
      message.success("文件已上传，正在后台串行解析");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
    return false;
  };

  const search = async () => {
    if (!activeSpaceId || !query.trim()) return;
    setSearching(true);
    setSearchResult(null);

    try {
      const result = await knowledgeApi.search(activeSpaceId, query.trim());
      setSearchResult(result);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "知识检索失败");
    } finally {
      setSearching(false);
    }
  };

  const openPreview = (doc: KnowledgeDocument) => {
    if (doc.status !== "completed") {
      message.warning("文档尚未处理完成，无法预览");
      return;
    }
    setPreviewDocument(doc);
    setPreviewOpen(true);
  };

  const reprocess = async (doc: KnowledgeDocument) => {
    setReprocessingId(doc.id);
    try {
      await knowledgeApi.reprocessDocument(doc.id);
      await loadDocuments(activeSpaceId);
      message.success("已加入重新处理队列");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重新处理失败");
    } finally {
      setReprocessingId(undefined);
    }
  };

  const selectedSpace = spaces.find((item) => item.id === activeSpaceId);
  const paginatedDocuments = documents.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (loading) {
    return <div className={styles.loading}><Spin size="large" /></div>;
  }

  if (configured === false) {
    return (
      <section className={styles.page}>
        <Alert
          type="info"
          showIcon
          message="知识服务尚未启用"
          description="管理员需要先部署独立知识服务，并为汇智小助手配置 QWENPAW_KNOWLEDGE_URL 与 QWENPAW_KNOWLEDGE_API_KEY。"
        />
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
        <Tooltip title="刷新空间与处理状态">
          <Button type="text" icon={<ReloadOutlined />} onClick={() => void refresh()} />
        </Tooltip>
      </header>

      <div className={styles.newLayout}>
        <div className={styles.leftPanel}>
          <Card className={styles.controlCard} bordered={false}>
            <div className={styles.spaceBar}>
              <Select
                className={styles.spaceSelect}
                value={activeSpaceId}
                placeholder="选择知识空间"
                options={spaces.map((space) => ({ value: space.id, label: space.name }))}
                onChange={setActiveSpaceId}
              />
              <Button icon={<FolderAddOutlined />} onClick={() => setModalOpen(true)}>新建空间</Button>
              <Upload
                accept={ACCEPTED_FILES}
                showUploadList={false}
                beforeUpload={upload}
                disabled={!activeSpaceId || uploading}
              >
                <Button type="primary" icon={<UploadOutlined />} loading={uploading} disabled={!activeSpaceId}>
                  上传资料
                </Button>
              </Upload>
            </div>
            <div className={styles.hint}>支持 PDF、Word、Excel、CSV、TXT、Markdown。</div>
          </Card>

          <Card className={styles.search} bordered={false} title="检索与问答">
            <Input.Search
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onSearch={() => void search()}
              placeholder="输入人物、事件、主题或写作需求"
              enterButton={<SearchOutlined />}
              loading={searching}
              disabled={!activeSpaceId}
            />
            {!searchResult ? (
              <div className={styles.searchEmpty}>检索结果会同时保留回答依据，方便核对原始材料。</div>
            ) : (
              <div className={styles.result}>
                {searchResult.documents.length === 0 ? (
                  <Typography.Text type="secondary">未找到相关文档</Typography.Text>
                ) : (
                  <>
                    <Typography.Text strong>命中的文档（{searchResult.document_count} 个）</Typography.Text>
                    <List
                      size="small"
                      dataSource={searchResult.documents}
                      renderItem={(item) => (
                        <List.Item
                          style={{ cursor: "pointer" }}
                          onClick={() => {
                            const doc = documents.find((d) => d.id === item.document_id);
                            if (doc) openPreview(doc);
                          }}
                        >
                          <div>
                            <Typography.Text strong>{item.filename}</Typography.Text>
                            <Typography.Text type="secondary">
                              {" "}· {item.snippet_count} 个相关片段
                            </Typography.Text>
                            {item.pages.length > 0 && (
                              <Typography.Text type="secondary">
                                {" "}· 涉及页码：{item.pages.slice(0, 5).join(", ")}
                                {item.pages.length > 5 ? "..." : ""}
                              </Typography.Text>
                            )}
                          </div>
                        </List.Item>
                      )}
                    />
                  </>
                )}
              </div>
            )}
          </Card>
        </div>

        <div className={styles.rightPanel}>
          <Card
            className={styles.documentsCard}
            bordered={false}
            title={selectedSpace ? `${selectedSpace.name} 的资料` : "资料列表"}
          >
            {!activeSpaceId ? (
              <Empty description='先创建一个知识空间，例如"案件资料"或"写作素材"' />
            ) : (
              <>
                <List
                  dataSource={paginatedDocuments}
                  locale={{ emptyText: "还没有资料" }}
                  renderItem={(item) => {
                    const meta = statusMeta[item.status];
                    return (
                      <List.Item
                        className={styles.documentItem}
                        style={{ cursor: item.status === "completed" ? "pointer" : "default" }}
                        onClick={() => openPreview(item)}
                      >
                        <div className={styles.documentName}>
                          <FileSearchOutlined />
                          <span>{item.filename}</span>
                        </div>
                        <Space size={8}>
                          {item.status === "processing" && <Spin size="small" />}
                          <Tag color={meta.color}>{meta.label}</Tag>
                          <Tooltip title="按当前解析规则重新入库">
                            <Button
                              type="text"
                              size="small"
                              icon={<RetweetOutlined />}
                              loading={reprocessingId === item.id}
                              onClick={(event) => {
                                event.stopPropagation();
                                void reprocess(item);
                              }}
                            >
                              重新处理
                            </Button>
                          </Tooltip>
                        </Space>
                        {item.status === "failed" && item.error ? <div className={styles.error}>{item.error}</div> : null}
                      </List.Item>
                    );
                  }}
                />
                {documents.length > pageSize && (
                  <div style={{ marginTop: 16, textAlign: "right" }}>
                    <Pagination
                      current={currentPage}
                      total={documents.length}
                      pageSize={pageSize}
                      onChange={setCurrentPage}
                      size="small"
                    />
                  </div>
                )}
              </>
            )}
          </Card>
        </div>
      </div>

      <Modal
        open={modalOpen}
        title="新建知识空间"
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
        onOk={() => void createSpace()}
        onCancel={() => setModalOpen(false)}
      >
        <Typography.Paragraph type="secondary">按业务边界分空间，例如"案件资料""日常写作素材"。同一账户的智能体可检索该账户的空间。</Typography.Paragraph>
        <Input autoFocus value={newSpaceName} onChange={(event) => setNewSpaceName(event.target.value)} onPressEnter={() => void createSpace()} placeholder="知识空间名称" />
      </Modal>

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
