import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
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
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FolderOutlined,
  GlobalOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SwapOutlined,
  UnorderedListOutlined,
  TableOutlined,
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
  updateTable,
} from "../../api/modules/dataAnalysis";
import type {
  DataImportJob,
  DataTableItem,
} from "../../api/modules/dataAnalysis";
import PathTreePanel, { ALL_PATHS } from "../../features/path-tree/PathTreePanel";
import { flattenPaths, pathsApi, pathTree, type PathNode } from "../../api/modules/paths";
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
  // Path scope: which path's tables are shown. ALL_PATHS means no filter.
  const [selectedPath, setSelectedPath] = useState<string>(ALL_PATHS);
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  // Move-selected-tables dialog.
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferTarget, setTransferTarget] = useState<string | null>(null);
  const [transferPaths, setTransferPaths] = useState<Map<string, PathNode>>(new Map());
  const [transferring, setTransferring] = useState(false);
  // Bumped after a transfer so the tree's counts re-read.
  const [treeRefreshKey, setTreeRefreshKey] = useState(0);

  // Create wizard
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [columnNotes, setColumnNotes] = useState<string[]>([""]);
  const [creating, setCreating] = useState(false);

  // Edit table metadata (display name + description only; the physical
  // table_name is not editable, and imported data is untouched).
  const [editOpen, setEditOpen] = useState(false);
  const [editTableId, setEditTableId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editForm] = Form.useForm();

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
  // Account-wide in-flight count, independent of the path being viewed (see
  // `loadJobCounts`). Drives both the header indicator and the polling loop.
  const [pendingImportCount, setPendingImportCount] = useState(0);
  // Path id -> node, fed by the tree so a new table's destination can be named and
  // sent without a second request for the same structure.
  const [pathNodes, setPathNodes] = useState<Map<string, PathNode>>(new Map());

  const rootPathId = useMemo(() => {
    for (const node of pathNodes.values()) {
      if (node.is_root) return node.id;
    }
    return "";
  }, [pathNodes]);

  /**
   * Where a new table will be created.
   *
   * ``ALL_PATHS`` means "no filter", which for creation can only mean the public
   * area (the root path). Naming it lets the wizard state the destination instead
   * of the user finding out later that it went somewhere unexpected.
   */
  const createTargetPathId =
    selectedPath && selectedPath !== ALL_PATHS ? selectedPath : rootPathId;
  const createTargetName =
    (createTargetPathId && pathNodes.get(createTargetPathId)?.name) || "公共区（未归类）";

  // Preview (inline in upload area)
  const [previewData, setPreviewData] = useState<{
    columns: string[];
    rows: Record<string, unknown>[];
    total: number;
    initialized?: boolean;
    // Metadata aligned to `columns` by the backend (name-matched). Preferred over
    // pairing with `previewColumns` by index, which ignored the id primary key
    // and shifted every header label by one.
    column_meta?: Array<{ name: string; comment: string; type: string; is_primary_key: boolean }>;
  } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewColumns, setPreviewColumns] = useState<Array<{ comment: string; column_name: string | null; type: string | null }>>([]);

  // Detail
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailData, setDetailData] = useState<DataTableItem | null>(null);

  /**
   * Reload the path tree.
   *
   * The tree shows per-path table counts and the import badge, so anything that
   * changes where a table lives — or whether its import is running — must call
   * this. Without it the tree kept showing pre-import counts and no badge while a
   * file was still being imported.
   */
  const refreshTree = useCallback(() => setTreeRefreshKey((n) => n + 1), []);

  const fetchTables = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page, page_size: pageSize };
      if (keyword) params.keyword = keyword;
      // Unknown/foreign paths come back as an empty page from the server, which
      // is the intended fail-closed behaviour — never fall back to "全部".
      if (selectedPath && selectedPath !== ALL_PATHS) params.path_id = selectedPath;
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
  }, [page, pageSize, keyword, dateRange, selectedPath]);

  useEffect(() => {
    fetchTables();
  }, [fetchTables]);

  // Switching path restarts paging: the previous page number could be past the
  // end of a smaller filtered set. Skipped on first render (fetchTables above
  // already loads page 1).
  const pathEffectReady = useRef(false);
  useEffect(() => {
    if (!pathEffectReady.current) {
      pathEffectReady.current = true;
      return;
    }
    // The ticked rows belong to the list that was on screen. Carrying the
    // selection across a path switch leaves checkboxes ticked for tables that are
    // no longer displayed, so a batch action would hit rows the user cannot see.
    setSelectedRowKeys([]);
    setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPath]);

  // Fetch the account-wide counts: total jobs (for the drawer) and how many are
  // still in flight (for the header). The pending count must come from the server
  // rather than from `tables`: a table filed under another path is not in the
  // current view, but its import is still running, and deriving the number from
  // the visible rows made the indicator vanish when switching paths.
  const loadJobCounts = useCallback(async () => {
    try {
      const [all, pending] = await Promise.all([
        getImportJobs({ page: 1, page_size: 1 }),
        getImportJobs({ page: 1, page_size: 1, pending_only: true }),
      ]);
      setJobsTotal(all.total);
      setPendingImportCount(pending.total);
    } catch {
      /* the indicator is best-effort; the table list still works without it */
    }
  }, []);

  useEffect(() => {
    void loadJobCounts();
  }, [loadJobCounts]);

  // Poll while the *account* has imports in flight (see `loadJobCounts`).
  useEffect(() => {
    if (pendingImportCount <= 0) return;
    const timer = window.setInterval(() => {
      void fetchTables();
      void loadJobCounts();
      refreshTree();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [pendingImportCount, fetchTables, loadJobCounts, refreshTree]);

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

  const fetchJobs = useCallback(async (
    p: number,
    tableId?: string,
    status?: string,
    pendingOnly?: boolean,
  ) => {
    setJobsLoading(true);
    try {
      const params: Record<string, unknown> = { page: p, page_size: 10 };
      if (tableId) params.table_id = tableId;
      if (pendingOnly) params.pending_only = true;
      else if (status) params.status = status;
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
      refreshTree();
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
      refreshTree();
    } catch {
      message.error("批量删除失败");
    }
  };

  /**
   * Move the selected tables to a path (or back to the public area).
   *
   * Filing a table also moves its physical storage to that path's schema on the
   * server; that move is what actually isolates it. Failure is reported per file
   * rather than swallowed, because a partially-filed batch is the confusing case.
   */
  const transferSelected = async (targetPathId: string | null) => {
    const ids = selectedRowKeys as string[];
    if (ids.length === 0) return;
    setTransferring(true);
    let moved = 0;
    let failed = 0;
    for (const tableId of ids) {
      try {
        if (targetPathId) {
          await pathsApi.bindResource(targetPathId, "data_table", tableId);
        } else {
          await pathsApi.unbindResourceAnyPath("data_table", tableId);
        }
        moved += 1;
      } catch {
        failed += 1;
      }
    }
    setTransferring(false);
    setTransferOpen(false);
    setSelectedRowKeys([]);
    fetchTables();
    refreshTree();
    if (failed === 0) {
      message.success(`已将 ${moved} 张表转移`);
    } else {
      message.warning(`${moved} 张成功，${failed} 张失败`);
    }
  };

  /** Open the transfer dialog, loading the path tree to choose from. */
  const openTransfer = async () => {
    setTransferTarget(null);
    setTransferOpen(true);
    try {
      const result = await pathTree();
      setTransferPaths(flattenPaths(result.tree || []));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "路径列表读取失败");
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
    fetchJobs(1, undefined, undefined, true);
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
        // Land in the path the user is looking at; without this the table was
        // created public and had to be transferred by hand afterwards.
        path_id: createTargetPathId,
      });
      message.success(`数据表定义已创建并归入「${createTargetName}」，首次导入时将自动确定列名和类型`);
      setCreateOpen(false);
      createForm.resetFields();
      setColumnNotes([""]);
      fetchTables();
      refreshTree();
    } catch (err) {
      if (err && typeof err === "object" && "errorFields" in err) return;
      message.error("创建失败");
    } finally {
      setCreating(false);
    }
  };

  // Header for one preview column. Prefers the backend's name-matched metadata;
  // if it is absent, look the description up by column NAME and only fall back
  // to positional pairing last (that pairing is what mislabelled every column).
  const previewTitle = (name: string, idx: number): string => {
    const fromMeta = previewData?.column_meta?.find((m) => m.name === name);
    if (fromMeta) {
      if (fromMeta.is_primary_key && !fromMeta.comment) return `主键 (${name})`;
      return fromMeta.comment ? `${fromMeta.comment} (${name})` : name;
    }
    const byName = previewColumns.find((c) => c.column_name === name);
    const comment = byName?.comment ?? previewColumns[idx]?.comment;
    return comment ? `${comment} (${name})` : name;
  };

  const handleImport = async (file: File) => {
    if (!importTableId) return false;
    setImporting(true);
    try {
      await importFiles(importTableId, [file]);
      message.success("文件已上传，正在后台导入");
      fetchTables();
      refreshTree();
    } catch {
      message.error("上传失败");
    } finally {
      setImporting(false);
    }
    return false;
  };

  // Edit table metadata
  const handleEditOpen = (record: DataTableItem) => {
    setEditTableId(record.id);
    editForm.setFieldsValue({
      display_name: record.display_name || "",
      description: record.description || "",
    });
    setEditOpen(true);
  };

  const handleEditSave = async () => {
    if (!editTableId) return;
    try {
      const values = await editForm.validateFields();
      setEditing(true);
      await updateTable(editTableId, {
        display_name: values.display_name || "",
        description: values.description || "",
      });
      message.success("已更新表信息");
      setEditOpen(false);
      fetchTables();
    } catch (err) {
      if (err && typeof err === "object" && "errorFields" in err) return;
      message.error("更新失败");
    } finally {
      setEditing(false);
    }
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
      // No badge here: an import count on the column is not attributable to a path,
      // so it showed while viewing a case with nothing importing. Per-path progress
      // is shown on the path tree instead.
      title: "状态",
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
      width: 230,
      render: (_, record) => (
        <Space size="small">
          <Button size="small" icon={<EyeOutlined />} onClick={() => handleShowDetail(record.id)}>
            详情
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => handleEditOpen(record)}>
            编辑
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
    <section className={styles.page}>
      <header className={styles.hero}>
        <div className={styles.titleBlock}>
          <div className={styles.mark}><TableOutlined /></div>
          <div>
            <Typography.Title level={3}>数据空间</Typography.Title>
            <Typography.Text>把表格数据变为可查询、可关联、可供智能体分析的数据资产。</Typography.Text>
          </div>
        </div>
        <Space>
          {/* Entry point to the task list. No account-wide number here: the count
              is only meaningful per path, so it lives on the tree. */}
          <Tooltip title="查看导入任务列表">
            <Button
              type="text"
              size="small"
              icon={<UnorderedListOutlined />}
              onClick={handleShowJobs}
              aria-label="导入任务列表"
            >
              任务
            </Button>
          </Tooltip>
          <Tooltip title="刷新列表">
            <Button type="text" icon={<ReloadOutlined />} onClick={() => void fetchTables()} />
          </Tooltip>
        </Space>
      </header>

      <div className={styles.body}>
        {/* Path tree: shows which case each table is filed under and which agents
            can read it. */}
        <aside className={styles.sidebar}>
          <div className={styles.sidebarTitle}>路径</div>
          <PathTreePanel
            value={selectedPath}
            onChange={setSelectedPath}
            countKind="table"
            refreshKey={treeRefreshKey}
            onTreeLoaded={(nodes) => setPathNodes(flattenPaths(nodes))}
          />
        </aside>
        <div className={styles.content}>
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
              {selectedRowKeys.length > 0 && (
                <Button
                  icon={<SwapOutlined />}
                  onClick={() => void openTransfer()}
                >
                  转移到路径 ({selectedRowKeys.length})
                </Button>
              )}
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
          // The columns need ~1080px, more than this column has beside the 240px
          // path tree. Without a scroll container the table's intrinsic width
          // pushed the whole flex row wider, which shrank the sidebar to ~56px and
          // made this page's layout visibly differ from the knowledge page's.
          scroll={{ x: 1080 }}
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
        </div>
      </div>

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
          说明：您只需描述每列的含义，系统会在您上传真实数据文件时自动取用文件表头作为列名，并自动生成主键（id）。所有列默认按文本（TEXT）存储，手机号、IMEI、账号等编号类数据不会被误转成数字而丢失。
        </div>
      </Modal>

      {/* Edit table metadata */}
      <Modal
        title="编辑数据表"
        open={editOpen}
        onOk={handleEditSave}
        onCancel={() => setEditOpen(false)}
        confirmLoading={editing}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={editForm} layout="vertical">
          <Form.Item
            name="display_name"
            label="表名"
            rules={[{ required: true, message: "请输入表名" }]}
          >
            <Input placeholder="如：销售订单" />
          </Form.Item>
          <Form.Item
            name="description"
            label="表描述"
            rules={[{ required: true, message: "请输入表描述" }]}
          >
            <Input.TextArea
              rows={3}
              placeholder="该表所代表的数据含义，如：公司季度销售明细"
            />
          </Form.Item>
        </Form>
        <div style={{ color: "#999", fontSize: 12 }}>
          说明：仅修改表名与描述，不影响已导入的数据；实际表名由系统生成，不可更改。
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
                        const title = previewTitle(c, idx);
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
                        return sum + Math.max(120, previewTitle(c, idx).length * 10 + 40);
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

      {/* Move selected tables between paths. */}
      <Modal
        title={`将 ${selectedRowKeys.length} 张表转移到`}
        open={transferOpen}
        onCancel={() => setTransferOpen(false)}
        onOk={() => void transferSelected(transferTarget)}
        okText="转移"
        cancelText="取消"
        confirmLoading={transferring}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary">
          转移会把表的数据一并移到目标路径，之后只有挂载了该路径的智能体才能访问。
          选择「公共区（未归类）」则移回公共区，账号下所有智能体可见。
        </Typography.Paragraph>
        <Radio.Group
          value={transferTarget ?? ""}
          onChange={(event) =>
            setTransferTarget(event.target.value === "" ? null : String(event.target.value))
          }
          style={{ display: "flex", flexDirection: "column", gap: 8 }}
        >
          <Radio value="">
            <GlobalOutlined /> 公共区（未归类）
          </Radio>
          {[...transferPaths.values()]
            .filter((node) => !node.is_root)
            .map((node) => (
              <Radio key={node.id} value={node.id}>
                <FolderOutlined /> {node.name}
              </Radio>
            ))}
        </Radio.Group>
        {[...transferPaths.values()].filter((node) => !node.is_root).length === 0 && (
          <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
            还没有子路径。请先在左侧路径树中新建一条路径，再转移数据表。
          </Typography.Paragraph>
        )}
      </Modal>
    </section>
  );
}
