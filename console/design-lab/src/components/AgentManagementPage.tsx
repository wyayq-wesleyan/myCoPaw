import { useEffect, useMemo, useState } from "react";
import {
  AppstoreOutlined,
  BookOutlined,
  CopyOutlined,
  DeleteOutlined,
  RobotOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import {
  Button,
  Checkbox,
  Input,
  Pagination,
  Popconfirm,
  Tag,
  Tooltip,
  message,
} from "antd";

import type { AgentModule, AgentRecord, Role } from "../mock";

interface AgentManagementPageProps {
  role: Role;
  agents: AgentRecord[];
  onDuplicateAgent: (agentId: string) => AgentRecord;
  onDeleteAgents: (agentIds: string[]) => void;
  onUseAgent: (agentId: string) => void;
  onOpenDetail: (agentId: string) => void;
}

type ModuleFilter = "all" | AgentModule;
type ViewMode = "list" | "card";

const moduleLabels: Record<AgentModule, string> = {
  verify: "信息核查",
  office: "办公助手",
};

const PAGE_SIZE = 6;

export function AgentManagementPage({
  role,
  agents,
  onDuplicateAgent,
  onDeleteAgents,
  onUseAgent,
  onOpenDetail,
}: AgentManagementPageProps) {
  const [viewMode, setViewMode] = useState<ViewMode>("card");
  const [moduleFilter, setModuleFilter] = useState<ModuleFilter>("all");
  const [query, setQuery] = useState("");
  const [onlyEnabled, setOnlyEnabled] = useState(true);
  const [page, setPage] = useState(1);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const isAdmin = role === "admin";

  const disabledCount = agents.filter((agent) => !agent.enabled).length;

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return agents.filter((agent) => {
      if (moduleFilter !== "all" && agent.module !== moduleFilter) return false;
      if (onlyEnabled && !agent.enabled) return false;
      if (!keyword) return true;
      return [agent.name, agent.description, agent.model, ...agent.tags]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    });
  }, [agents, moduleFilter, onlyEnabled, query]);

  const paged = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageIds = paged.map((agent) => agent.id);
  const allPageSelected =
    pageIds.length > 0 && pageIds.every((id) => selectedIds.includes(id));
  const somePageSelected = pageIds.some((id) => selectedIds.includes(id));

  useEffect(() => {
    setPage(1);
    setSelectedIds([]);
  }, [query, moduleFilter, onlyEnabled]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const toggleSelected = (agentId: string) => {
    setSelectedIds((previous) =>
      previous.includes(agentId)
        ? previous.filter((id) => id !== agentId)
        : [...previous, agentId],
    );
  };

  const togglePageSelection = () => {
    if (allPageSelected) {
      setSelectedIds((previous) =>
        previous.filter((id) => !pageIds.includes(id)),
      );
    } else {
      setSelectedIds((previous) => [...new Set([...previous, ...pageIds])]);
    }
  };

  const deleteSelected = () => {
    onDeleteAgents(selectedIds);
    message.success(`已删除 ${selectedIds.length} 个智能体`);
    setSelectedIds([]);
  };

  const duplicateAgent = (agent: AgentRecord) => {
    const next = onDuplicateAgent(agent.id);
    message.success(
      next.scope === "custom"
        ? `已复制为自定义智能体「${next.name}」，可继续调优`
        : `已复制为「${next.name}」，可继续修改`,
    );
    onOpenDetail(next.id);
  };

  const canUse = (agent: AgentRecord) =>
    agent.enabled && (agent.module === "office" || role === "user");

  const agentActions = (agent: AgentRecord) => {
    if (isAdmin) {
      return (
        <div className="dl-agent-actions">
          <Button size="small" onClick={() => onOpenDetail(agent.id)}>
            详情
          </Button>
          <Button
            size="small"
            icon={<CopyOutlined />}
            onClick={() => duplicateAgent(agent)}
          >
            复制
          </Button>
          <Popconfirm
            title={`删除「${agent.name}」？`}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => {
              onDeleteAgents([agent.id]);
              message.success("智能体已删除");
            }}
          >
            <Button size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </div>
      );
    }

    // 普通用户：预设只读，可复制为自定义；自定义可调优、可删除
    if (agent.scope === "system") {
      return (
        <div className="dl-agent-actions">
          <Button size="small" onClick={() => onOpenDetail(agent.id)}>
            详情
          </Button>
          <Button
            size="small"
            icon={<CopyOutlined />}
            onClick={() => duplicateAgent(agent)}
          >
            复制为自定义
          </Button>
          <Button
            size="small"
            type="primary"
            disabled={!canUse(agent)}
            onClick={() => onUseAgent(agent.id)}
          >
            使用
          </Button>
        </div>
      );
    }

    return (
      <div className="dl-agent-actions">
        <Button size="small" onClick={() => onOpenDetail(agent.id)}>
          详情
        </Button>
        <Button
          size="small"
          type="primary"
          disabled={!canUse(agent)}
          onClick={() => onUseAgent(agent.id)}
        >
          使用
        </Button>
        <Popconfirm
          title={`删除「${agent.name}」？`}
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => {
            onDeleteAgents([agent.id]);
            message.success("已删除自定义智能体");
          }}
        >
          <Button size="small" danger icon={<DeleteOutlined />}>
            删除
          </Button>
        </Popconfirm>
      </div>
    );
  };

  return (
    <main className="dl-agents-page">
      <header className="dl-page-head">
        <div>
          <div className="dl-page-kicker">智能体管理</div>
          <h1>智能体</h1>
          <p>
            系统预设由管理员统一维护，普通用户只读；普通用户可复制预设为自定义智能体，自行增加知识库、技能后调试使用。
          </p>
        </div>
        <div className="dl-page-head-actions">
          <div className="dl-view-toggle">
            <Tooltip title="卡片视图">
              <button
                type="button"
                className={viewMode === "card" ? "dl-view-active" : ""}
                onClick={() => setViewMode("card")}
              >
                <AppstoreOutlined />
              </button>
            </Tooltip>
            <Tooltip title="列表视图">
              <button
                type="button"
                className={viewMode === "list" ? "dl-view-active" : ""}
                onClick={() => setViewMode("list")}
              >
                <UnorderedListOutlined />
              </button>
            </Tooltip>
          </div>
        </div>
      </header>

      {isAdmin ? (
        <div className="dl-agents-admin-note">
          <RobotOutlined />
          <span>
            管理员维护系统预设有知识文件、技能、工具和查询客户端；普通用户复制后可继续调整自己的副本，互不影响。
          </span>
        </div>
      ) : null}

      <div className="dl-agents-toolbar">
        <div className="dl-agents-summary">
          <span className="dl-agents-summary-title">全部智能体</span>
          <span className="dl-agents-summary-count">
            {filtered.length} / {agents.length}
          </span>
        </div>

        <div className="dl-agents-filters">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索名称、说明、标签"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="dl-agents-search"
          />
          <div className="dl-agents-module-filter">
            {(["all", "verify", "office"] as ModuleFilter[]).map((item) => (
              <button
                key={item}
                type="button"
                className={moduleFilter === item ? "dl-filter-active" : ""}
                onClick={() => setModuleFilter(item)}
              >
                {item === "all" ? "全部模块" : moduleLabels[item]}
              </button>
            ))}
          </div>
          <label className="dl-enabled-filter">
            <Checkbox
              checked={onlyEnabled}
              onChange={(event) => setOnlyEnabled(event.target.checked)}
            />
            仅显示可用
            {disabledCount > 0 ? (
              <span className="dl-disabled-count">已停用 {disabledCount}</span>
            ) : null}
          </label>
        </div>
      </div>

      {isAdmin ? (
        <div className="dl-agent-bulkbar">
          <Checkbox
            checked={allPageSelected}
            indeterminate={somePageSelected && !allPageSelected}
            onChange={togglePageSelection}
          >
            全选本页
          </Checkbox>
          <span>已选 {selectedIds.length} 个</span>
          <Popconfirm
            title={`删除 ${selectedIds.length} 个智能体？`}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={deleteSelected}
            disabled={selectedIds.length === 0}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={selectedIds.length === 0}
            >
              删除所选
            </Button>
          </Popconfirm>
        </div>
      ) : null}

      {filtered.length === 0 ? (
        <div className="dl-agents-empty">
          <RobotOutlined />
          <div>没有符合条件的智能体</div>
          <span>调整搜索词或筛选条件后再试。</span>
        </div>
      ) : viewMode === "card" ? (
        <div className="dl-agent-grid">
          {paged.map((agent) => (
            <article
              key={agent.id}
              className={`dl-agent-card ${
                agent.enabled ? "" : "dl-agent-card-disabled"
              }`}
            >
              <div className="dl-agent-card-head">
                {isAdmin ? (
                  <Checkbox
                    checked={selectedIds.includes(agent.id)}
                    onChange={() => toggleSelected(agent.id)}
                  />
                ) : null}
                <div className="dl-agent-card-icon">
                  <RobotOutlined />
                </div>
                <div className="dl-agent-card-title">
                  <div className="dl-agent-name-row">
                    <h3>{agent.name}</h3>
                    <Tag
                      className={
                        agent.scope === "system"
                          ? "dl-agent-tag-system"
                          : "dl-agent-tag-custom"
                      }
                    >
                      {agent.scope === "system" ? "系统预设" : "自定义"}
                    </Tag>
                    <Tag color={agent.enabled ? "success" : "default"}>
                      {agent.enabled ? "可用" : "已停用"}
                    </Tag>
                  </div>
                  <div className="dl-agent-sub">
                    {moduleLabels[agent.module]} · {agent.model}
                  </div>
                </div>
              </div>

              <p className="dl-agent-desc">{agent.description}</p>

              <div className="dl-agent-stats">
                <span>
                  <BookOutlined /> {agent.knowledgeCount} 知识
                </span>
                <span>
                  <ThunderboltOutlined /> {agent.skillCount} 技能
                </span>
                <span>
                  <ToolOutlined /> {agent.toolCount} 工具
                </span>
              </div>

              <div className="dl-agent-tags">
                {agent.tags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>

              <div className="dl-agent-foot">
                <span className="dl-agent-updated">
                  更新于 {agent.updatedAt}
                </span>
                {agentActions(agent)}
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="dl-agent-list">
          <div className="dl-agent-list-head">
            {isAdmin ? (
              <Checkbox
                checked={allPageSelected}
                indeterminate={somePageSelected && !allPageSelected}
                onChange={togglePageSelection}
              />
            ) : (
              <span />
            )}
            <span>名称</span>
            <span>模块</span>
            <span>模型</span>
            <span>资源</span>
            <span>状态</span>
            <span>更新时间</span>
            <span>操作</span>
          </div>
          {paged.map((agent) => (
            <div className="dl-agent-list-row" key={agent.id}>
              {isAdmin ? (
                <Checkbox
                  checked={selectedIds.includes(agent.id)}
                  onChange={() => toggleSelected(agent.id)}
                />
              ) : (
                <span />
              )}
              <div className="dl-agent-list-name">
                <span className="dl-agent-list-icon">
                  <RobotOutlined />
                </span>
                <span>
                  <strong>{agent.name}</strong>
                  <small>{agent.description}</small>
                </span>
              </div>
              <span>{moduleLabels[agent.module]}</span>
              <span>{agent.model}</span>
              <span>
                {agent.knowledgeCount} 知识 · {agent.skillCount} 技能 ·{" "}
                {agent.toolCount} 工具
              </span>
              <Tag color={agent.enabled ? "success" : "default"}>
                {agent.enabled ? "可用" : "已停用"}
              </Tag>
              <span>{agent.updatedAt}</span>
              {agentActions(agent)}
            </div>
          ))}
        </div>
      )}

      <div className="dl-agent-pagination">
        <Pagination
          size="small"
          current={page}
          pageSize={PAGE_SIZE}
          total={filtered.length}
          showSizeChanger={false}
          onChange={setPage}
          showTotal={(total) => `共 ${total} 个智能体`}
        />
      </div>
    </main>
  );
}

export default AgentManagementPage;
