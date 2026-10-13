import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Spin,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  FolderAddOutlined,
  FolderOutlined,
  GlobalOutlined,
  SyncOutlined,
  MoreOutlined,
} from "@ant-design/icons";
import {
  pathsApi,
  pathTree,
  type PathNode,
} from "../../api/modules/paths";
import styles from "./PathTreePanel.module.less";

const { Text } = Typography;

/** A synthetic node id meaning "everything my agent may reach, unfiltered". */
export const ALL_PATHS = "__all__";

interface Props {
  /** Currently selected path id, or {@link ALL_PATHS}. */
  value: string;
  onChange: (pathId: string) => void;
  /**
   * Which count to show on each node. The tree itself is identical in both
   * pages; only the numbers differ (documents vs tables), so the caller picks.
   */
  countKind?: "document" | "table";
  /** Bump to force a refetch (e.g. after an upload). */
  refreshKey?: number;
  /**
   * Render the tree as read-only. Set when the page cannot actually filter by
   * path (e.g. the knowledge service is not configured), so clicking a node does
   * not fire a request that is guaranteed to fail and surface an error toast.
   * The structure stays visible — it is still the useful part.
   *
   * A read-only tree also drops the selection highlight: highlighting a row says
   * "you are filtered to this", which is not a state the page can be in when it
   * cannot filter at all.
   */
  disabled?: boolean;
  /** Hide the edit actions (e.g. a picker that should not mutate anything). */
  readOnly?: boolean;
  /**
   * Reports the loaded tree to the caller.
   *
   * The page above needs the node *names* (to say where an upload will land)
   * without issuing a second request for the same structure, so the tree that was
   * already fetched is handed up rather than refetched.
   */
  onTreeLoaded?: (tree: PathNode[]) => void;
}

/**
 * The path tree shown beside the knowledge / data-analysis content.
 *
 * Two facts are surfaced deliberately because both are easy to misread:
 *
 *  * the root is labelled **公共区（未归类）**, since "unbound" means "public"
 *    rather than "isolated";
 *  * each node shows how many agents are attached, so an empty-looking case with
 *    no agents is obviously unreachable rather than mysteriously empty.
 *
 * Editing lives on the row itself (hover → buttons) rather than on a separate
 * screen, because the point of the tree is to see where a path sits while you
 * change it.
 */
export default function PathTreePanel({
  value,
  onChange,
  countKind = "document",
  refreshKey = 0,
  disabled = false,
  readOnly = false,
  onTreeLoaded,
}: Props) {
  const [tree, setTree] = useState<PathNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [busy, setBusy] = useState(false);

  // Name dialog: create (with a parent) or rename (existing node).
  const [nameOpen, setNameOpen] = useState(false);
  const [nameTarget, setNameTarget] = useState<{ parentId: string; node: PathNode | null } | null>(null);
  const [nameValue, setNameValue] = useState("");



  // Delete confirmation. A modal rather than a Popconfirm because the action now
  // lives in a dropdown, and a popconfirm anchored to a menu item that closes on
  // click disappears before it can be read.
  const [deleteTarget, setDeleteTarget] = useState<PathNode | null>(null);

  const canEdit = !disabled && !readOnly;
  const refresh = useCallback(() => setLocalRefresh((n) => n + 1), []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await pathTree();
      setTree(result.tree || []);
      onTreeLoaded?.(result.tree || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "路径加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // ``onTreeLoaded`` is intentionally not a dependency: it is a reporting
    // callback, and depending on it would re-fetch on every caller render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, refreshKey, localRefresh]);

  /** Totals across the account, shown on the "全部" row. */
  const totals = useMemo(() => {
    let docs = 0;
    let tables = 0;
    const walk = (nodes: PathNode[]) => {
      for (const node of nodes) {
        docs += node.document_count || 0;
        tables += node.table_count || 0;
        walk(node.children || []);
      }
    };
    walk(tree);
    return { docs, tables };
  }, [tree]);

  const countOf = (node: PathNode) =>
    countKind === "document" ? node.document_count || 0 : node.table_count || 0;

  /**
   * How many of this node's resources are still processing.
   *
   * The two kinds are counted from separate server fields because documents are
   * processed by the knowledge worker and tables by the import worker; the page
   * shows the one matching what it lists.
   */
  const pendingOf = (node: PathNode) =>
    countKind === "document"
      ? node.pending_count || 0
      : node.pending_table_count || 0;

  // A disabled tree keeps showing structure but must not pretend to be a filter:
  // acting on a click it cannot honour is what produced a failing request and an
  // error toast.
  const select = (pathId: string) => {
    if (disabled) return;
    onChange(pathId);
  };

  /** Run a mutation, then refresh the tree so counts stay truthful. */
  const run = async (action: () => Promise<unknown>, successText: string) => {
    setBusy(true);
    try {
      await action();
      message.success(successText);
      refresh();
      return true;
    } catch (err) {
      // The store's messages are written for this audience (e.g. "路径下还有内容…"),
      // so they are surfaced verbatim rather than replaced.
      message.error(err instanceof Error ? err.message : "操作失败");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const openCreate = (parent: PathNode) => {
    setNameTarget({ parentId: parent.id, node: null });
    setNameValue("");
    setNameOpen(true);
  };

  const openRename = (node: PathNode) => {
    setNameTarget({ parentId: node.parent_id || "", node });
    setNameValue(node.name);
    setNameOpen(true);
  };

  const submitName = async () => {
    const target = nameTarget;
    if (!target) return;
    const trimmed = nameValue.trim();
    if (!trimmed) {
      message.warning("路径名称不能为空");
      return;
    }
    const ok = await run(
      () =>
        target.node
          ? pathsApi.rename(target.node.id, trimmed)
          : pathsApi.create(trimmed, target.parentId),
      target.node ? "已重命名" : "已创建路径",
    );
    if (ok) setNameOpen(false);
  };

  if (loading && tree.length === 0) {
    return (
      <div className={styles.center}>
        <Spin size="small" />
      </div>
    );
  }

  if (error && tree.length === 0) {
    // A missing paths API is the normal appearance of a deploy that has not
    // restarted the app yet, so offer a retry instead of making the user reload
    // the whole page once the backend catches up.
    return (
      <div className={styles.center}>
        <Text type="danger" className={styles.error}>
          {error}
        </Text>
        <Button type="link" size="small" onClick={() => void load()}>
          重试
        </Button>
      </div>
    );
  }

  if (tree.length === 0) {
    return (
      <div className={styles.center}>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无路径" />
      </div>
    );
  }

  /**
   * Row actions.
   *
   * Only the two most-used controls stay inline (create a sub-path, open the
   * overflow menu); everything else lives behind the menu. Five 24px buttons
   * needed 120px of a 226px row, which left no room for the path name — the tree
   * showed names only while the pointer was over the row that revealed the
   * buttons.
   */
  const renderActions = (node: PathNode) => {
    if (!canEdit) return null;
    const menuItems = [
      ...(node.is_root
        ? []
        : [
            {
              key: "rename",
              label: "重命名",
              icon: <EditOutlined />,
            },
          ]),
      ...(node.is_root
        ? []
        : [
            { type: "divider" as const },
            {
              key: "delete",
              label: "删除此路径",
              icon: <DeleteOutlined />,
              danger: true,
            },
          ]),
    ];
    return (
      <span
        className={styles.actions}
        onClick={(event) => event.stopPropagation()}
      >
        <Tooltip title="新建子路径">
          <Button
            type="text"
            size="small"
            icon={<FolderAddOutlined />}
            aria-label="新建子路径"
            onClick={() => openCreate(node)}
          />
        </Tooltip>
        <Dropdown
          trigger={["click"]}
          menu={{
            items: menuItems,
            onClick: ({ key, domEvent }) => {
              domEvent.stopPropagation();
              if (key === "rename") openRename(node);
              if (key === "delete") setDeleteTarget(node);
            },
          }}
        >
          <Button
            type="text"
            size="small"
            icon={<MoreOutlined />}
            aria-label="更多操作"
          />
        </Dropdown>
      </span>
    );
  };

  /**
   * A node is rendered flat (indented by depth) rather than with a collapsing
   * Tree: the hierarchy is shallow in practice, and always-expanded keeps the
   * whole account structure visible at a glance — which is the point of showing
   * paths next to the content.
   */
  const renderNode = (node: PathNode, depth: number) => {
    // No highlight while read-only: see the ``disabled`` prop docstring.
    const selected = !disabled && value === node.id;
    const count = countOf(node);
    // Which resources are still being processed *in this path*.
    const pending = pendingOf(node);
    const label = node.is_root ? "公共区（未归类）" : node.name;
    return (
      <div key={node.id}>
        <div
          className={`${styles.row} ${selected ? styles.rowActive : ""} ${
            disabled ? styles.rowDisabled : ""
          }`}
          style={{ paddingLeft: 8 + depth * 14 }}
          onClick={() => select(node.id)}
          role={disabled ? undefined : "button"}
          tabIndex={disabled ? undefined : 0}
          onKeyDown={(event) => {
            if (disabled) return;
            if (event.key === "Enter" || event.key === " ") select(node.id);
          }}
        >
          <span className={styles.icon}>
            {node.is_root ? <GlobalOutlined /> : <FolderOutlined />}
          </span>
          <Tooltip
            title={
              node.is_root
                ? "账号公共区：未归类到任何路径的资料都在这里，所有智能体可见。"
                : node.name
            }
          >
            <span className={styles.name}>{label}</span>
          </Tooltip>
          <span className={styles.count}>{count}</span>
          {/*
            Processing badge, in the slot the agent count used to occupy.
            Attribution is the point: the badge says *this path* has work running,
            so the user can tell where their upload went without inspecting each
            case. The account-wide total is deliberately not shown here — it would
            sit on every path and answer nothing. Agents are no longer displayed
            because membership is managed from the chat header, and showing it in
            two places invited the two from drifting apart.
          */}
          {pending > 0 && (
            <Tooltip
              title={
                countKind === "table"
                  ? `${pending} 个数据表正在后台导入`
                  : `${pending} 个文件正在后台处理`
              }
            >
              <Tag
                icon={<SyncOutlined spin />}
                color="processing"
                className={styles.pendingTag}
                aria-label={`处理中 ${pending}`}
              >
                {pending}
              </Tag>
            </Tooltip>
          )}
          {renderActions(node)}
        </div>
        {(node.children || []).map((child) => renderNode(child, depth + 1))}
      </div>
    );
  };

  return (
    <div className={styles.panel}>
      <div
        className={`${styles.row} ${
          !disabled && value === ALL_PATHS ? styles.rowActive : ""
        } ${disabled ? styles.rowDisabled : ""}`}
        onClick={() => select(ALL_PATHS)}
        role={disabled ? undefined : "button"}
        tabIndex={disabled ? undefined : 0}
        onKeyDown={(event) => {
          if (disabled) return;
          if (event.key === "Enter" || event.key === " ") select(ALL_PATHS);
        }}
      >
        <span className={styles.icon}>
          <FolderOutlined />
        </span>
        <span className={styles.name}>全部</span>
        <span className={styles.count}>
          {countKind === "document" ? totals.docs : totals.tables}
        </span>
      </div>
      <div className={styles.divider} />
      {tree.map((root) => renderNode(root, 0))}

      {/* Create / rename */}
      <Modal
        title={nameTarget?.node ? "重命名路径" : "新建子路径"}
        open={nameOpen}
        onCancel={() => setNameOpen(false)}
        onOk={() => void submitName()}
        okText={nameTarget?.node ? "保存" : "创建"}
        cancelText="取消"
        confirmLoading={busy}
        destroyOnHidden
      >
        <Input
          placeholder="例如：案件一、2026年、公共侦查手段"
          value={nameValue}
          onChange={(event) => setNameValue(event.target.value)}
          onPressEnter={() => void submitName()}
          maxLength={120}
          autoFocus
        />
        {nameTarget?.node == null && nameTarget?.parentId && (
          <Text type="secondary" className={styles.dialogHint}>
            将创建在该路径之下。
          </Text>
        )}
      </Modal>

      {/* Delete confirmation (see the ``deleteTarget`` docstring). */}
      <Modal
        title="删除此路径？"
        open={deleteTarget !== null}
        onCancel={() => setDeleteTarget(null)}
        onOk={() => {
          const target = deleteTarget;
          if (!target) return;
          void run(() => pathsApi.remove(target.id), "已删除路径").then((ok) => {
            if (ok) setDeleteTarget(null);
          });
        }}
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={busy}
        destroyOnHidden
      >
        <Text>
          将删除路径「{deleteTarget?.name}」。只允许删除空路径（无内容、无子路径、
          无智能体），若路径下还有内容会拒绝删除。
        </Text>
      </Modal>
    </div>
  );
}
