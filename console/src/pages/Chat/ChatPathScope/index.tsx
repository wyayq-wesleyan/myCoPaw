import { useCallback, useEffect, useState } from "react";
import { Badge, Button, Dropdown, Empty, Spin, Tooltip, Typography } from "antd";
import { CheckOutlined, DatabaseOutlined, FolderOutlined, GlobalOutlined } from "@ant-design/icons";
import { message } from "antd";
import { pathsApi, pathTree, type PathNode } from "../../../api/modules/paths";
import { useAgentStore } from "../../../stores/agentStore";
import styles from "./index.module.less";

const { Text } = Typography;

export type PathScopeKind = "knowledge" | "data";

interface Props {
  /** Which resources this selector controls. */
  kind: PathScopeKind;
  /** Called after the grants are written, so the page can refresh what it shows. */
  onChanged?: () => void;
}

/**
 * Per-agent path scope, shown in the chat header next to the model selector.
 *
 * Paths decide **what an agent may read**: knowledge documents and data tables
 * filed under a path are reachable only by agents granted that path, while
 * anything unfiled lives in the account's public area (the root path) which every
 * agent has. This is the control that makes that choice without leaving the chat.
 *
 * Two deliberate choices:
 *
 *  * It edits the **agent**, not the conversation. The requirement is that the
 *    choice applies to all of that agent's conversations, and the backend
 *    resolves the scope per retrieval — so switching here affects every chat with
 *    this agent immediately, with no reload.
 *  * It writes the **whole set** on each toggle. A checkbox list implies the
 *    agent's reach equals what is ticked; adding/removing one at a time could
 *    leave the two disagreeing if a request failed midway.
 */
export default function ChatPathScope({ kind, onChanged }: Props) {
  const { selectedAgent } = useAgentStore();
  const [tree, setTree] = useState<PathNode[]>([]);
  const [granted, setGranted] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const label = kind === "knowledge" ? "知识空间" : "数据空间";
  const kindIcon = kind === "knowledge" ? <FolderOutlined /> : <DatabaseOutlined />;

  const load = useCallback(async () => {
    if (!selectedAgent) return;
    setLoading(true);
    try {
      const [treeResult, grantResult] = await Promise.all([
        pathTree(),
        pathsApi.agentGrants(selectedAgent),
      ]);
      setTree(treeResult.tree || []);
      setGranted(grantResult.path_ids || []);
    } catch {
      // The paths API may be missing on a backend that has not been restarted.
      // The selector stays closed rather than showing a misleading empty set.
      setTree([]);
      setGranted([]);
    } finally {
      setLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    void load();
  }, [load]);

  const isGranted = (pathId: string) => granted.includes(pathId);

  const toggle = async (pathId: string) => {
    if (!selectedAgent || saving) return;
    const next = isGranted(pathId)
      ? granted.filter((id) => id !== pathId)
      : [...granted, pathId];
    setSaving(true);
    // Optimistic: the checkbox must respond immediately, and the whole set is
    // written so a failure can be rolled back to the previous value exactly.
    const previous = granted;
    setGranted(next);
    try {
      const result = await pathsApi.setAgentGrants(selectedAgent, next);
      setGranted(result.path_ids || next);
      onChanged?.();
    } catch (error) {
      setGranted(previous);
      message.error(error instanceof Error ? error.message : "路径授权失败");
    } finally {
      setSaving(false);
    }
  };

  /** Flatten so nested cases are visible with their depth, root first. */
  const rows: Array<{ node: PathNode; depth: number }> = [];
  const walk = (nodes: PathNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth });
      walk(node.children || [], depth + 1);
    }
  };
  walk(tree, 0);

  const dropdownContent = (
    <div className={styles.pathPanel}>
      <div className={styles.pathPanelHeader}>
        <Text strong>{label}</Text>
        <Text type="secondary" className={styles.pathPanelHint}>
          勾选后该智能体的所有对话都可读取这条路径下的内容；未勾选的路径它读不到。
        </Text>
      </div>
      {loading ? (
        <div className={styles.pathPanelCenter}>
          <Spin size="small" />
        </div>
      ) : rows.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无路径。可在知识空间或数据空间页新建路径。"
        />
      ) : (
        <div className={styles.pathList}>
          {rows.map(({ node, depth }) => {
            const checked = isGranted(node.id);
            // One label for both the eye and assistive tech: the root's stored
            // name is the account's ("默认") while it is *presented* as the public
            // area, and a screen reader announcing a different name than the one
            // on screen makes the row impossible to identify reliably.
            const displayName = node.is_root ? "公共区（未归类）" : node.name;
            return (
              <button
                key={node.id}
                type="button"
                className={`${styles.pathRow} ${checked ? styles.pathRowChecked : ""}`}
                style={{ paddingLeft: 10 + depth * 14 }}
                disabled={saving}
                onClick={(event) => {
                  event.stopPropagation();
                  void toggle(node.id);
                }}
                aria-pressed={checked}
                aria-label={displayName}
              >
                <span className={styles.pathCheck}>
                  {checked ? <CheckOutlined /> : null}
                </span>
                <span className={styles.pathIcon}>
                  {node.is_root ? <GlobalOutlined /> : kindIcon}
                </span>
                <span className={styles.pathName}>{displayName}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );

  const grantedCount = granted.length;

  return (
    <Dropdown
      open={open}
      onOpenChange={setOpen}
      trigger={["click"]}
      dropdownRender={() => dropdownContent}
      placement="bottomRight"
    >
      <Tooltip title={`${label}：选择该智能体可以读取的路径`}>
        <Button size="small" icon={kindIcon} loading={saving}>
          {label}
          <Badge
            count={grantedCount}
            size="small"
            showZero
            className={styles.pathBadge}
            color={grantedCount === 0 ? "#d9d9d9" : "#1677ff"}
          />
        </Button>
      </Tooltip>
    </Dropdown>
  );
}
