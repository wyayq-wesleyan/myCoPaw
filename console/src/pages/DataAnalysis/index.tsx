import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  Badge,
  Card,
  Col,
  DatePicker,
  Drawer,
  Dropdown,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  UnorderedListOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import {
  batchDeleteTables,
  createTable,
  deleteTable,
  getImportJobs,
  downloadImportJobLog,
  getTableDetail,
  importFiles,
  listTables,
  previewTable,
} from "../../api/modules/dataAnalysis";
import type {
  DataImportJob,
  DataTableItem,
} from "../../api/modules/dataAnalysis";
import styles from "./index.module.less";

const { RangePicker } = DatePicker;
const { Dragger } = Upload;

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  pending: { color: "default", label: "等待中" },
  parsing: { color: "processing", label: "解析中" },
  loading: { color: "processing", label: "写入中" },
  completed: { color: "success", label: "已完成" },
  partial: { color: "warning", label: "部分成功" },
  failed: { color: "error", label: "失败" },
};

export default function DataAnalysisPage() {
  const [tables, setTables] = useState<DataTableItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState("");
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  // Create wizard
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [columnNotes, setColumnNotes] = useState<string[]>([""]);
  const [creating, setCreating] = useState(false);

  // Import
  const [importTableId, setImportTableId] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  // Ant Design fires beforeUpload once per selected file with the full list;
  // without this guard a selection is submitted once per file.
  const importBatchGuard = useRef(false);

  // Jobs
  const [jobs, setJobs] = useState<DataImportJob[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsPage, setJobsPage] = useState(1);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobsDrawerOpen, setJobsDrawerOpen] = useState(false);
  const [jobsFilterStatus, setJobsFilterStatus] = useState<string | undefined>(undefined);

  // Preview (inline in upload area)
  const [previewData, setPreviewData] = useState<{ columns: string[]; rows: Record<string, unknown>[]; total: number; initialized?: boolean; column_comments?: string[] } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewColumns, setPreviewColumns] = useState<Array<{ comment: string; column_name: string | null; type: string | null }>>([]);

  // Detail
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailData, setDetailData] = useState<DataTableItem | null>(null);

  const fetchTables = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page, page_size: pageSize };
      if (keyword) params.keyword = keyword;
      if (dateRange?.[0]) params.created_start = dateRange[0].toISOString();
      if (dateRange?.[1]) params.created_end = dateRange[1].toISOString();
      const data = await listTables(params);
      setTables(data.items);
      setTotal(data.total);
    } catch {
      message.error("加载数据表失败");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, keyword, dateRange]);

  useEffect(() => {
    fetchTables();
  }, [fetchTables]);

  useEffect(() => {
    if (!tables.some((table) => (table.pending_import_count || 0) > 0)) return;
    const timer = window.setInterval(() => void fetchTables(), 3000);
    return () => window.clearInterval(timer);
  }, [tables, fetchTables]);

  // Auto-fetch preview when import area opens for an initialized table
  useEffect(() => {
    if (!importTableId) return;
    const table = tables.find((t) => t.id === importTableId);
    if (table?.initialized) {
      setPreviewLoading(true);
      previewTable(importTableId, 100)
        .then((data) => { setPreviewData(data); setPreviewColumns(table.columns || []); })
        .catch(() => {})
        .finally(() => setPreviewLoading(false));
    } else {
      setPreviewData(null);
      setPreviewColumns(table?.columns || []);
    }
  }, [importTableId, tables]);

  // Fetch total jobs count for badge
  useEffect(() => {
    getImportJobs({ page: 1, page_size: 1 }).then((d) => setJobsTotal(d.total)).catch(() => {});
  }, []);

  const fetchJobs = useCallback(async (p: number, tableId?: string, status?: string) => {
    setJobsLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 10 };
      if (tableId) params.table_id = tableId;
      if (status) params.status = status;
      const data = await getImportJobs(params);
      setJobs(data.items);
      setJobsTotal(data.total);
    } catch {
      message.error("加载任务列表失败");
    } finally {
      setJobsLoading(false);
    }
  }, []);

  const handleDelete = async (id: string) => {
    try {
      await deleteTable(id);
      message.success("已删除");
      fetchTables();
    } catch {
      message.error("删除失败");
    }
  };

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      await batchDeleteTables(selectedRowKeys as string[]);
      message.success(`已删除 ${selectedRowKeys.length} 张表`);
      setSelectedRowKeys([]);
      fetchTables();
    } catch {
      message.error("批量删除失败");
    }
  };

  const handleShowDetail = async (id: string) => {
    try {
      const data = await getTableDetail(id);
      setDetailData(data);
      setDetailOpen(true);
    } catch {
      message.error("获取详情失败");
    }
  };

  const handleShowJobs = () => {
    setJobsPage(1);
    setJobsFilterStatus(undefined);
    setJobsDrawerOpen(true);
    fetchJobs(1);
  };

  // Create wizard
  const addColumnNote = () => {
    setColumnNotes([...columnNotes, ""]);
  };

  const updateColumnNote = (index: number, value: string) => {
    const updated = [...columnNotes];
    updated[index] = value;
    setColumnNotes(updated);
  };

  const removeColumnNote = (index: number) => {
    setColumnNotes(columnNotes.filter((_, i) => i !== index));
  };

  const handleCreate = async () => {
    try {
      const values = await createForm.validateFields();
      const notes = columnNotes.map((s) => s.trim());
      if (notes.length === 0) {
        message.warning("请至少添加一列并填写含义");
        return;
      }
      const emptyIdx = notes.findIndex((s) => !s);
      if (emptyIdx >= 0) {
        message.warning(`第 ${emptyIdx + 1} 列的含义不能为空，请填写后再提交`);
        return;
      }
      setCreating(true);
      await createTable({
        display_name: values.display_name || "",
        description: values.description || "",
        columns: notes,
      });
      message.success("数据表定义已创建，首次导入时将自动确定列名和类型");
      setCreateOpen(false);
      createForm.resetFields();
      setColumnNotes([""]);
      fetchTables();
    } catch (err) {
      if (err && typeof err === "object" && "errorFields" in err) return;
      message.error("创建失败");
    } finally {
      setCreating(false);
    }
  };

  const handleImport = async (file: File) => {
    if (!importTableId) return false;
    setImporting(true);
    try {
      await importFiles(importTableId, [file]);
      message.success("文件已上传，正在后台导入");
      fetchTables();
    } catch {
      message.error("上传失败");
    } finally {
      setImporting(false);
    }
    return false;
  };

  const handleImportMultiple = async (files: File[]) => {
    if (!importTableId || files.length === 0) return false;
    setImporting(true);
    let success = 0;
    let fail = 0;
    try {
      for (const file of files) {
        try {
          await importFiles(importTableId, [file]);
          success++;
        } catch {
          fail++;
        }
      }
      fetchTables();
      if (fail === 0) {
        message.success(`${success} 个文件已上传，正在后台导入`);
      } else {
        message.warning(`${success} 个成功，${fail} 个失败`);
      }
    } finally {
      setImporting(false);
    }
    return false;
  };

  const columns_def: ColumnsType<DataTableItem> = [
    {
      title: "表名",
      dataIndex: "display_name",
      key: "display_name",
      width: 200,
      render: (v: string, record) => v || record.table_name,
      sorter: (a: DataTableItem, b: DataTableItem) => (a.display_name || a.table_name).localeCompare(b.display_name || b.table_name),
    },
    {
      title: <Badge count={tables.reduce((sum, item) => sum + (item.pending_import_count || 0), 0)} overflowCount={999} offset={[8, 0]}><span>状态</span></Badge>,
      dataIndex: "initialized",
      key: "status",
      width: 100,
      render: (_: unknown, record: DataTableItem) => (
        <Tag color={record.initialized ? "success" : "default"}>
          {record.initialized ? "已导入" : "待导入"}
        </Tag>
      ),
      filters: [
        { text: "已导入", value: true },
        { text: "待导入", value: false },
      ],
      onFilter: (value: unknown, record: DataTableItem) => record.initialized === value,
      sorter: (a: DataTableItem, b: DataTableItem) => (a.initialized ? 1 : 0) - (b.initialized ? 1 : 0),
      defaultSortOrder: "ascend",
    },
    { title: "表描述", dataIndex: "description", key: "description", width: 200, ellipsis: true,
      sorter: (a: DataTableItem, b: DataTableItem) => (a.description || '').localeCompare(b.description || ''),
    },
    { title: "列数", dataIndex: "column_count", key: "column_count", width: 80, align: "center",
      sorter: (a: DataTableItem, b: DataTableItem) => a.column_count - b.column_count,
    },
    { title: "行数", dataIndex: "row_count", key: "row_count", width: 100, align: "right", render: (v: number) => v?.toLocaleString() || "0",
      sorter: (a: DataTableItem, b: DataTableItem) => a.row_count - b.row_count,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (v: string) => (v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "-"),
      sorter: (a: DataTableItem, b: DataTableItem) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
    },
    {
      title: "操作",
      key: "actions",
      width: 180,
      render: (_, record) => (
        <Space size="small">
          <Button size="small" icon={<EyeOutlined />} onClick={() => handleShowDetail(record.id)}>
            详情
          </Button>
          <Dropdown
            menu={{
              items: [
                {
                  key: "import",
                  label: "录入数据",
                  icon: <UploadOutlined />,
                  onClick: () => {
                    setImportTableId(record.id);
                  },
                },
              ],
            }}
            trigger={["click"]}
          >
            <Button size="small" icon={<EditOutlined />}>录入</Button>
          </Dropdown>
          <Popconfirm title={record.initialized ? "确定删除此数据表？将同时删除瀚高中真实表和全部数据。" : "确定删除此数据表定义？"} onConfirm={() => handleDelete(record.id)} okText="确定" cancelText="取消">
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const jobColumns: ColumnsType<DataImportJob> = [
    { title: "表名", dataIndex: "table_name", key: "table_name", width: 150, ellipsis: true,
      sorter: (a: DataImportJob, b: DataImportJob) => a.table_name.localeCompare(b.table_name),
    },
    { title: "文件名", dataIndex: "filename", key: "filename", width: 200, ellipsis: true,
      sorter: (a: DataImportJob, b: DataImportJob) => a.filename.localeCompare(b.filename),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => {
        const meta = STATUS_MAP[v] || { color: "default", label: v };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
      sorter: (a: DataImportJob, b: DataImportJob) => a.status.localeCompare(b.status),
    },
    { title: "总行数", dataIndex: "total_rows", key: "total_rows", width: 80, align: "right",
      sorter: (a: DataImportJob, b: DataImportJob) => a.total_rows - b.total_rows,
    },
    { title: "成功", dataIndex: "success_rows", key: "success_rows", width: 80, align: "right", render: (v: number) => <span style={{ color: "#52c41a" }}>{v}</span>,
      sorter: (a: DataImportJob, b: DataImportJob) => a.success_rows - b.success_rows,
    },
    { title: "失败", dataIndex: "failed_rows", key: "failed_rows", width: 80, align: "right", render: (v: number) => <span style={{ color: v > 0 ? "#ff4d4f" : undefined }}>{v}</span>,
      sorter: (a: DataImportJob, b: DataImportJob) => a.failed_rows - b.failed_rows,
    },
    {
      title: "处理时长",
      key: "processing_duration",
      width: 100,
      render: (_: unknown, record: DataImportJob) => {
        if (record.processing_duration != null) {
          return `${record.processing_duration.toFixed(1)}s`;
        }
        if (record.started_at && !record.finished_at) {
          return <span style={{ color: '#1890ff' }}>处理中...</span>;
        }
        return '-';
      },
      sorter: (a: DataImportJob, b: DataImportJob) => (a.processing_duration || 0) - (b.processing_duration || 0),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (v: string) => (v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "-"),
      sorter: (a: DataImportJob, b: DataImportJob) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
    },
    {
      title: "日志",
      key: "log",
      width: 70,
      render: (_: unknown, record: DataImportJob) => record.status === "failed"
        ? <Button type="text" icon={<DownloadOutlined />} title="下载失败日志" onClick={() => void downloadImportJobLog(record.id)} />
        : null,
    },
  ];

  return (
    <div className={styles.container}>
      <Card bordered={false}>
        <Row gutter={16} align="middle" style={{ marginBottom: 16 }}>
          <Col>
            <Input
              placeholder="搜索表名/描述"
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onPressEnter={() => { setPage(1); fetchTables(); }}
              style={{ width: 240 }}
              allowClear
            />
          </Col>
          <Col>
            <RangePicker
              value={dateRange as [dayjs.Dayjs, dayjs.Dayjs] | null}
              onChange={(dates) => setDateRange(dates as [dayjs.Dayjs | null, dayjs.Dayjs | null])}
            />
          </Col>
          <Col>
            <Space>
              <Button icon={<SearchOutlined />} onClick={() => { setPage(1); fetchTables(); }}>查询</Button>
              <Button icon={<ReloadOutlined />} onClick={() => { setKeyword(""); setDateRange(null); setPage(1); }}>重置</Button>
            </Space>
          </Col>
          <Col flex="1" />
          <Col>
            <Space>
              <Button icon={<UnorderedListOutlined />} onClick={handleShowJobs}>
                任务
              </Button>
              {selectedRowKeys.length > 0 && (
                <Popconfirm
                  title={`确定删除选中的 ${selectedRowKeys.length} 张表？`}
                  onConfirm={handleBatchDelete}
                  okText="确定"
                  cancelText="取消"
                >
                  <Button danger icon={<DeleteOutlined />}>
                    批量删除 ({selectedRowKeys.length})
                  </Button>
                </Popconfirm>
              )}
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                新建数据表
              </Button>
            </Space>
          </Col>
        </Row>

        <Table
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
          }}
          columns={columns_def}
          dataSource={tables}
          rowKey="id"
          loading={loading}
          onRow={(record) => ({
            onClick: (e) => {
              if ((e.target as HTMLElement).closest('button, .ant-checkbox-wrapper, .ant-dropdown')) return;
              setImportTableId(record.id);
            },
            style: { cursor: "pointer" },
          })}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
        />
      </Card>

      {/* Create Wizard */}
      <Modal
        title="新建数据表"
        open={createOpen}
        onCancel={() => { setCreateOpen(false); setColumnNotes([""]); }}
        width={800}
        footer={[
          <Button key="cancel" onClick={() => { setCreateOpen(false); setColumnNotes([""]); }}>取消</Button>,
          <Button key="create" type="primary" loading={creating} onClick={handleCreate}>创建</Button>,
        ]}
      >
        <Form form={createForm} layout="vertical">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="display_name" label="表名" rules={[{ required: true, message: "请输入表名" }]}>
                <Input placeholder="如：销售订单" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="description" label="表描述" rules={[{ required: true, message: "请输入表描述" }]}>
                <Input placeholder="该表所代表的数据含义，如：公司季度销售明细" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            label="数据描述（按数据文件列的顺序填写每列含义，不能为空）"
            required
            rules={[{
              validator: () => {
                const empty = columnNotes.findIndex((s) => !s.trim());
                if (columnNotes.length === 0) return Promise.reject(new Error("请至少添加一列"));
                if (empty >= 0) return Promise.reject(new Error(`第 ${empty + 1} 列含义不能为空`));
                return Promise.resolve();
              },
            }]}
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {columnNotes.map((note, idx) => {
                const isEmpty = !note.trim();
                return (
                  <div key={idx} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <span style={{ width: 24, textAlign: "center", color: isEmpty ? "#ff4d4f" : "#999" }}>{idx + 1}</span>
                    <Input
                      placeholder="这一列是什么含义，如：订单金额（元）"
                      value={note}
                      onChange={(e) => updateColumnNote(idx, e.target.value)}
                      status={isEmpty ? "error" : undefined}
                    />
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => removeColumnNote(idx)} />
                  </div>
                );
              })}
              <Button
                type="dashed"
                icon={<PlusOutlined />}
                onClick={addColumnNote}
                disabled={columnNotes.length > 0 && !columnNotes[columnNotes.length - 1].trim()}
              >
                ＋ 添加一列
              </Button>
            </div>
            <div style={{ color: "#999", fontSize: 12, marginTop: 4 }}>
              提示：每一列都必须填写中文含义，不能为空。AI 分析时将根据这些含义理解数据。
            </div>
          </Form.Item>
        </Form>
        <div style={{ color: "#999", fontSize: 12 }}>
          说明：您只需描述每列的含义，系统会在您上传真实数据文件时，根据文件内容自动确定列名、类型，并自动生成主键（id）。
        </div>
      </Modal>

      {/* Import + Preview area */}
      {importTableId && (() => {
        const table = tables.find((t) => t.id === importTableId);
        return (
          <Card
            title={`数据录入 — ${table?.display_name || ""}`}
            style={{ marginTop: 16 }}
            extra={<Button onClick={() => { setImportTableId(null); setPreviewData(null); setPreviewColumns([]); }}>关闭</Button>}
            bordered
          >
            <Spin spinning={importing} tip="正在上传并解析文件...">
              <Dragger
                accept=".csv,.xls,.xlsx"
                multiple
                showUploadList={false}
                beforeUpload={(file, fileList) => {
                  // Ant Design invokes beforeUpload once per selected file and
                  // passes the *whole* list to every call, so uploading the
                  // list each time submits an N-file selection N times (9
                  // files produced 81 import jobs). Only the first callback of
                  // a selection may run; the guard is set synchronously, before
                  // any await, so the sibling calls in the same tick see it.
                  if (importBatchGuard.current) return false;
                  importBatchGuard.current = true;
                  const batch = fileList.slice();
                  const run = batch.length <= 1
                    ? handleImport(file)
                    : handleImportMultiple(batch);
                  void Promise.resolve(run).finally(() => {
                    importBatchGuard.current = false;
                  });
                  return false;
                }}
                disabled={importing}
              >
                <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
                <p className="ant-upload-hint">支持 CSV、XLS、XLSX 格式，可多选文件，单文件不超过 10GB</p>
              </Dragger>
            </Spin>

            {/* Preview section */}
            {table?.initialized ? (
              <div style={{ marginTop: 16 }}>
                <div style={{ fontWeight: 500, marginBottom: 8 }}>数据预览</div>
                {previewLoading ? (
                  <div style={{ textAlign: "center", padding: 20, color: "#999" }}>加载中...</div>
                ) : previewData ? (
                  <>
                    <div style={{ marginBottom: 8, color: "#666", fontSize: 13 }}>
                      共 {previewData.total.toLocaleString()} 行，显示前 {previewData.rows.length} 行
                    </div>
                    <Table
                      columns={previewData.columns.map((c, idx) => {
                        const comment = previewColumns[idx]?.comment;
                        const title = comment ? `${comment} (${c})` : c;
                        return {
                          title,
                          dataIndex: c,
                          key: c,
                          ellipsis: true,
                          width: Math.max(120, title.length * 10 + 40),
                          render: (v: unknown) => v === null || v === undefined ? <span style={{ color: "#999" }}>NULL</span> : String(v),
                        };
                      })}
                      dataSource={previewData.rows.map((r, i) => ({ ...r, _key: i }))}
                      rowKey="_key"
                      scroll={{ x: previewData.columns.reduce((sum, c, idx) => {
                        const comment = previewColumns[idx]?.comment;
                        const title = comment ? `${comment} (${c})` : c;
                        return sum + Math.max(120, title.length * 10 + 40);
                      }, 0), y: 300 }}
                      pagination={false}
                      size="small"
                      bordered
                    />
                  </>
                ) : (
                  <Button
                    icon={<EyeOutlined />}
                    onClick={async () => {
                      setPreviewLoading(true);
                      try {
                        const data = await previewTable(importTableId, 100);
                        setPreviewData(data);
                        setPreviewColumns(table?.columns || []);
                      } catch {
                        message.error("预览失败");
                      } finally {
                        setPreviewLoading(false);
                      }
                    }}
                  >
                    加载预览
                  </Button>
                )}
              </div>
            ) : table && table.columns.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <div style={{ fontWeight: 500, marginBottom: 8 }}>列定义</div>
                <div style={{ color: "#666", fontSize: 13, marginBottom: 8 }}>
                  该数据表尚未导入数据。下述为当前定义的数据描述，上传真实数据后将自动确定列名和类型。
                </div>
                <Table
                  columns={[
                    { title: "#", key: "idx", width: 50, render: (_: unknown, __: unknown, i: number) => i + 1 },
                    { title: "数据描述", dataIndex: "comment", key: "comment", width: 250 },
                    { title: "实际列名", dataIndex: "column_name", key: "column_name", width: 180, render: (v: string | null) => v || <span style={{ color: "#999" }}>待导入后生成</span> },
                    { title: "数据类型", dataIndex: "type", key: "type", width: 120, render: (v: string | null) => v || <span style={{ color: "#999" }}>待推断</span> },
                  ]}
                  dataSource={table.columns.map((c, i) => ({ ...c, _key: i }))}
                  rowKey="_key"
                  pagination={false}
                  size="small"
                  bordered
                />
              </div>
            ) : null}
          </Card>
        );
      })()}

      {/* Jobs Drawer */}
      <Drawer
        title="导入任务列表"
        open={jobsDrawerOpen}
        onClose={() => setJobsDrawerOpen(false)}
        width={900}
        extra={
          <Space>
            <Select
              placeholder="状态筛选"
              allowClear
              style={{ width: 120 }}
              value={jobsFilterStatus}
              onChange={(v) => { setJobsFilterStatus(v); setJobsPage(1); fetchJobs(1, undefined, v); }}
              options={Object.entries(STATUS_MAP).map(([k, v]) => ({ value: k, label: v.label }))}
            />
          </Space>
        }
      >
        <Table
          columns={jobColumns}
          dataSource={jobs}
          rowKey="id"
          loading={jobsLoading}
          expandable={{
            expandedRowRender: (record: DataImportJob) => {
              const errs = record.errors as Array<{ row?: number; column?: string; message?: string }>;
              if (!errs || errs.length === 0) return <Typography.Text type="secondary">无错误详情</Typography.Text>;
              return (
                <pre style={{
                  background: "#fff1f0",
                  border: "1px solid #ffa39e",
                  borderRadius: 6,
                  padding: "12px 16px",
                  margin: 0,
                  fontSize: 12,
                  lineHeight: 1.6,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  maxHeight: 200,
                  overflow: "auto",
                }}>
                  {errs.map((e, i) => `[${i + 1}] ${e.row != null ? `行${e.row} ` : ""}${e.column ? `列${e.column} ` : ""}${e.message || JSON.stringify(e)}`).join("\n")}
                </pre>
              );
            },
            rowExpandable: (record: DataImportJob) => record.status === "failed" || record.status === "partial",
          }}
          pagination={{
            current: jobsPage,
            pageSize: 10,
            total: jobsTotal,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p) => { setJobsPage(p); fetchJobs(p, undefined, jobsFilterStatus); },
          }}
        />
      </Drawer>


      {/* Detail Modal */}
      <Modal
        title="数据表详情"
        open={detailOpen}
        onCancel={() => { setDetailOpen(false); setDetailData(null); }}
        footer={<Button onClick={() => { setDetailOpen(false); setDetailData(null); }}>关闭</Button>}
        width={650}
      >
        {detailData && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: "8px 16px", fontSize: 14 }}>
              <span style={{ color: "#999" }}>表名</span>
              <span><Tag>{detailData.table_name}</Tag> <span style={{ color: "#999", fontSize: 12 }}>（系统自动生成）</span></span>

              <span style={{ color: "#999" }}>显示名称</span>
              <span style={{ fontWeight: 500 }}>{detailData.display_name || "-"}</span>

              <span style={{ color: "#999" }}>表描述</span>
              <span>{detailData.description || "-"}</span>

              <span style={{ color: "#999" }}>状态</span>
              <span><Tag color={detailData.initialized ? "success" : "default"}>{detailData.initialized ? "已导入" : "待导入"}</Tag></span>

              <span style={{ color: "#999" }}>列数</span>
              <span>{detailData.column_count}</span>

              <span style={{ color: "#999" }}>行数</span>
              <span>{detailData.row_count.toLocaleString()}</span>

              <span style={{ color: "#999" }}>导入次数</span>
              <span>{detailData.import_count ?? 0}</span>

              <span style={{ color: "#999" }}>累计导入行数</span>
              <span>{(detailData.total_imported ?? 0).toLocaleString()}</span>

              <span style={{ color: "#999" }}>创建时间</span>
              <span>{detailData.created_at ? dayjs(detailData.created_at).format("YYYY-MM-DD HH:mm:ss") : "-"}</span>

              <span style={{ color: "#999" }}>更新时间</span>
              <span>{detailData.updated_at ? dayjs(detailData.updated_at).format("YYYY-MM-DD HH:mm:ss") : "-"}</span>
            </div>

            {detailData.columns.length > 0 && (
              <>
                <div style={{ fontWeight: 500, marginTop: 8 }}>列定义</div>
                <Table
                  columns={[
                    { title: "#", key: "idx", width: 50, render: (_: unknown, __: unknown, i: number) => i + 1 },
                    { title: "数据描述", dataIndex: "comment", key: "comment", width: 200 },
                    { title: "实际列名", dataIndex: "column_name", key: "column_name", width: 150, render: (v: string | null) => v || <span style={{ color: "#999" }}>待导入后生成</span> },
                    { title: "数据类型", dataIndex: "type", key: "type", width: 100, render: (v: string | null) => v || <span style={{ color: "#999" }}>待推断</span> },
                  ]}
                  dataSource={detailData.columns.map((c, i) => ({ ...c, _key: i }))}
                  rowKey="_key"
                  pagination={false}
                  size="small"
                  bordered
                />
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
