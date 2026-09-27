import { useCallback, useEffect, useMemo, useState } from "react";
import { Empty, Spin, Tag, Tooltip, Typography } from "antd";
import { FolderOutlined, GlobalOutlined, RobotOutlined } from "@ant-design/icons";
import { pathTree, type PathNode } from "../../api/modules/paths";
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
  /** Whether to show which agents are granted each node. */
  showAgents?: boolean;
}

/**
 * The path tree shown beside the knowledge / data-analysis content.
 *
 * Read-only for now: the goal is to make the isolation *visible* — which case a
 * resource is filed under, and which agents can read it — before adding
 * mutations. Two facts are surfaced deliberately because both are easy to
 * misread:
 *
 *  * the root is labelled **公共区（未归类）**, since "unbound" means "public"
 *    rather than "isolated";
 *  * each node shows how many agents are attached, so an empty-looking case with
 *    no agents is obviously unreachable rather than mysteriously empty.
 */
export default function PathTreePanel({
  value,
  onChange,
  countKind = "document",
  refreshKey = 0,
  showAgents = true,
}: Props) {
  const [tree, setTree] = useState<PathNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await pathTree();
      setTree(result.tree || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "路径加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

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

  if (loading && tree.length === 0) {
    return (
      <div className={styles.center}>
        <Spin size="small" />
      </div>
    );
  }

  if (error && tree.length === 0) {
    return (
      <div className={styles.center}>
        <Text type="danger" className={styles.error}>
          {error}
        </Text>
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

  const countOf = (node: PathNode) =>
    countKind === "document" ? node.document_count || 0 : node.table_count || 0;

  /**
   * A node is rendered flat (indented by depth) rather than with a collapsing
   * Tree: the hierarchy is shallow in practice, and always-expanded keeps the
   * whole account structure visible at a glance — which is the point of showing
   * paths next to the content.
   */
  const renderNode = (node: PathNode, depth: number) => {
    const selected = value === node.id;
    const agents = node.agents || [];
    const count = countOf(node);
    const label = node.is_root ? "公共区（未归类）" : node.name;
    return (
      <div key={node.id}>
        <div
          className={`${styles.row} ${selected ? styles.rowActive : ""}`}
          style={{ paddingLeft: 8 + depth * 14 }}
          onClick={() => onChange(node.id)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") onChange(node.id);
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
          {showAgents && (
            <Tooltip
              title={
                agents.length === 0
                  ? node.is_root
                    ? "根路径在升级时已授予所有智能体。"
                    : "尚无智能体挂载此路径，因此没有智能体能读到它。"
                  : `已挂载 ${agents.length} 个智能体：${agents.join("、")}`
              }
            >
              <Tag
                icon={<RobotOutlined />}
                color={agents.length === 0 ? "default" : "blue"}
                className={styles.agentTag}
              >
                {agents.length}
              </Tag>
            </Tooltip>
          )}
        </div>
        {(node.children || []).map((child) => renderNode(child, depth + 1))}
      </div>
    );
  };

  return (
    <div className={styles.panel}>
      <div
        className={`${styles.row} ${value === ALL_PATHS ? styles.rowActive : ""}`}
        onClick={() => onChange(ALL_PATHS)}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") onChange(ALL_PATHS);
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
    </div>
  );
}
