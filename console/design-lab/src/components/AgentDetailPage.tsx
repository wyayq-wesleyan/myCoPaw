import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRightOutlined,
  BookOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
  InfoCircleOutlined,
  LinkOutlined,
  PlusOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Button,
  Input,
  Modal,
  Progress,
  Switch,
  Tag,
  Upload,
  message,
} from "antd";

import type { AgentRecord, ChatMessage, Role } from "../mock";
import { resourcesForAgent } from "../mock";
import { ChatConversation, MessageItem } from "./ChatThread";
import { Composer } from "./Composer";

interface AgentDetailPageProps {
  agent: AgentRecord;
  role: Role;
  onToggleAgent: (agentId: string, enabled: boolean) => void;
  onUpdateAgent: (agentId: string, patch: Partial<AgentRecord>) => void;
  onDuplicateAgent: (agentId: string) => AgentRecord;
  onUseAgent: (agentId: string) => void;
  onOpenDetail: (agentId: string) => void;
}

type ResourceMenu = "knowledge" | "skills" | "tools" | "mcp";

type KnowledgeStatus = "待解析" | "解析中" | "向量化中" | "已就绪" | "解析失败";

interface KnowledgeDoc {
  id: string;
  name: string;
  size: string;
  updatedAt: string;
  status: KnowledgeStatus;
  chunks: number;
}

const moduleLabels = {
  verify: "信息核查",
  office: "办公助手",
};

function initialDebugFor(agentName: string): ChatMessage[] {
  return [
    {
      id: "debug-1",
      role: "assistant",
      author: agentName,
      text: "这里是智能体调试区。你可以输入一段真实业务问题，我会按当前配置执行，但不会写入案件数据。",
      time: "10:52",
    },
    {
      id: "debug-2",
      role: "user",
      author: "我",
      text: "用一条示例手机号走一遍核查流程，看看结果结构是否合适。",
      time: "10:53",
    },
    {
      id: "debug-3",
      role: "assistant",
      author: agentName,
      text: "已按“目标解析 → 权限校验 → 查询 → 结论整理”执行。示例结果会返回人员基础信息、关联号码和核查结论；如果目标不在控，会先返回审批决策卡。",
      time: "10:54",
    },
  ];
}

function initialKnowledgeDocs(agentName: string): KnowledgeDoc[] {
  return [
    {
      id: "kd-1",
      name: "信息核查工作规范.pdf",
      size: "2.4 MB",
      updatedAt: "11-12 09:20",
      status: "已就绪",
      chunks: 86,
    },
    {
      id: "kd-2",
      name: "专业术语解释.docx",
      size: "386 KB",
      updatedAt: "11-13 15:02",
      status: "已就绪",
      chunks: 42,
    },
    {
      id: "kd-3",
      name: `${agentName.replace(/·/g, "-")}-业务流程手册.pdf`,
      size: "1.8 MB",
      updatedAt: "今天 10:12",
      status: "解析中",
      chunks: 0,
    },
  ];
}

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function AgentDetailPage({
  agent,
  role,
  onToggleAgent,
  onUpdateAgent,
  onDuplicateAgent,
  onUseAgent,
  onOpenDetail,
}: AgentDetailPageProps) {
  const baseResources = resourcesForAgent(agent);
  const isAdmin = role === "admin";
  // 系统预设：管理员可维护，普通用户只读
  // 自定义：归属者本人可调优
  const canEdit =
    agent.scope === "system"
      ? isAdmin
      : isAdmin || agent.owner === "张三" || !agent.owner;
  const isPresetReadOnly = agent.scope === "system" && !isAdmin;
  const [menu, setMenu] = useState<ResourceMenu>("knowledge");
  const [knowledge, setKnowledge] = useState<KnowledgeDoc[]>(() =>
    initialKnowledgeDocs(agent.name),
  );
  const [skills, setSkills] = useState(baseResources.skills);
  const [tools, setTools] = useState(baseResources.tools);
  const [mcpClients, setMcpClients] = useState<
    Array<{
      name: string;
      meta: string;
      transport: "streamable_http" | "sse" | "stdio";
      url: string;
      enabled: boolean;
    }>
  >([
    {
      name: "查询中心客户端",
      meta: "远程地址 · 只读查询",
      transport: "streamable_http",
      url: "https://query.internal/mcp",
      enabled: true,
    },
    {
      name: "案件系统客户端",
      meta: "远程地址 · 目标校验",
      transport: "sse",
      url: "https://case.internal/mcp/sse",
      enabled: true,
    },
  ]);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState({
    name: agent.name,
    description: agent.description,
    promptSummary: agent.promptSummary,
    model: agent.model,
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ name: "", meta: "" });
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpForm, setMcpForm] = useState({
    name: "",
    transport: "streamable_http" as "streamable_http" | "sse" | "stdio",
    url: "",
    headers: "",
    command: "",
    args: "",
    env: "",
    cwd: "",
    enabled: true,
  });
  const [debugMessages, setDebugMessages] = useState<ChatMessage[]>(() =>
    initialDebugFor(agent.name),
  );
  const [thinking, setThinking] = useState(false);

  useEffect(() => {
    setEditForm({
      name: agent.name,
      description: agent.description,
      promptSummary: agent.promptSummary,
      model: agent.model,
    });
  }, [agent]);

  const menuMeta: Array<{
    key: ResourceMenu;
    label: string;
    icon: ReactNode;
    count: number;
  }> = [
    {
      key: "knowledge",
      label: "知识空间",
      icon: <BookOutlined />,
      count: knowledge.length,
    },
    {
      key: "skills",
      label: "技能",
      icon: <ThunderboltOutlined />,
      count: skills.length,
    },
    {
      key: "tools",
      label: "工具",
      icon: <ToolOutlined />,
      count: tools.length,
    },
    {
      key: "mcp",
      label: "查询客户端",
      icon: <LinkOutlined />,
      count: mcpClients.length,
    },
  ];

  const currentItems: Array<{
    name: string;
    meta: string;
    transport?: string;
    url?: string;
    enabled?: boolean;
  }> = menu === "skills" ? skills : menu === "tools" ? tools : mcpClients;

  const updateCurrentItems = (
    updater: (
      items: Array<{
        name: string;
        meta: string;
        transport?: string;
        url?: string;
        enabled?: boolean;
      }>,
    ) => Array<{
      name: string;
      meta: string;
      transport?: string;
      url?: string;
      enabled?: boolean;
    }>,
  ) => {
    if (menu === "skills") setSkills(updater as never);
    else if (menu === "tools") setTools(updater as never);
    else if (menu === "mcp") setMcpClients(updater as never);
  };

  const addItem = (
    name: string,
    meta: string,
    extra?: Record<string, unknown>,
  ) => {
    updateCurrentItems((items) => [{ name, meta, ...extra }, ...items]);
  };

  const removeItem = (name: string) => {
    updateCurrentItems((items) => items.filter((item) => item.name !== name));
    message.success(`已删除「${name}」`);
  };

  const saveAgent = () => {
    onUpdateAgent(agent.id, {
      ...editForm,
      updatedAt: "刚刚",
    });
    setEditOpen(false);
  };

  const addResource = () => {
    if (!createForm.name.trim()) return;
    addItem(
      createForm.name.trim(),
      createForm.meta.trim() ||
        (menu === "knowledge"
          ? "刚刚上传 · 待解析"
          : menu === "skills"
          ? "新建 · 已启用"
          : menu === "tools"
          ? "新建 · 已启用"
          : "已连接"),
    );
    setCreateOpen(false);
    setCreateForm({ name: "", meta: "" });
    message.success("已添加");
  };

  const sendDebug = (text: string) => {
    if (!text.trim()) return;
    setDebugMessages((previous) => [
      ...previous,
      {
        id: `debug-${Date.now()}`,
        role: "user",
        author: "我",
        text,
        time: nowTime(),
      },
    ]);
    setThinking(true);
    window.setTimeout(() => {
      setDebugMessages((previous) => [
        ...previous,
        {
          id: `debug-${Date.now()}-reply`,
          role: "assistant",
          author: agent.name,
          text: "调试执行完成。我会按当前智能体配置返回结果；正式在案件中使用时，还会带上该智能体关联的知识、技能、工具和查询客户端。",
          time: nowTime(),
        },
      ]);
      setThinking(false);
    }, 700);
  };

  const startParse = (docId: string) => {
    setKnowledge((previous) =>
      previous.map((doc) =>
        doc.id === docId ? { ...doc, status: "解析中" } : doc,
      ),
    );
    window.setTimeout(() => {
      setKnowledge((previous) =>
        previous.map((doc) =>
          doc.id === docId ? { ...doc, status: "向量化中" } : doc,
        ),
      );
      window.setTimeout(() => {
        setKnowledge((previous) =>
          previous.map((doc) =>
            doc.id === docId
              ? { ...doc, status: "已就绪", chunks: 64, updatedAt: "刚刚" }
              : doc,
          ),
        );
        message.success("文档解析完成，已写入智能体专属向量库");
      }, 900);
    }, 900);
  };

  const reindex = (docId: string) => {
    setKnowledge((previous) =>
      previous.map((doc) =>
        doc.id === docId ? { ...doc, status: "向量化中" } : doc,
      ),
    );
    window.setTimeout(() => {
      setKnowledge((previous) =>
        previous.map((doc) =>
          doc.id === docId
            ? { ...doc, status: "已就绪", updatedAt: "刚刚" }
            : doc,
        ),
      );
      message.success("已重新向量化");
    }, 1100);
  };

  const removeKnowledgeDoc = (docId: string) => {
    setKnowledge((previous) => previous.filter((doc) => doc.id !== docId));
    message.success("文档已删除，向量数据同步清理");
  };

  const duplicate = () => {
    const next = onDuplicateAgent(agent.id);
    message.success(`已复制为「${next.name}」`);
    onOpenDetail(next.id);
  };

  return (
    <div className="dl-agent-detail-page">
      <aside className="dl-agent-res-sidebar">
        <div className="dl-agent-res-title">
          <RobotOutlined />
          <div>
            <strong>智能体配置</strong>
            <span>{moduleLabels[agent.module]}</span>
          </div>
        </div>

        <div className="dl-agent-res-menu">
          {menuMeta.map((item) => (
            <button
              key={item.key}
              type="button"
              className={menu === item.key ? "dl-agent-res-menu-active" : ""}
              onClick={() => setMenu(item.key)}
            >
              {item.icon}
              <span>{item.label}</span>
              <em>{item.count}</em>
            </button>
          ))}
        </div>

        <div className="dl-agent-res-content">
          <div className="dl-agent-res-content-head">
            <div>
              <strong>
                {menuMeta.find((item) => item.key === menu)?.label}
              </strong>
              <span>
                共{" "}
                {menu === "knowledge" ? knowledge.length : currentItems.length}{" "}
                项
              </span>
            </div>
            {canEdit ? (
              menu === "knowledge" ? (
                <Upload
                  multiple
                  showUploadList={false}
                  beforeUpload={(file) => {
                    setKnowledge((previous) => [
                      {
                        id: `kd-${Date.now()}-${file.name}`,
                        name: file.name,
                        size: `${Math.max(1, Math.round(file.size / 1024))} KB`,
                        updatedAt: "刚刚",
                        status: "待解析",
                        chunks: 0,
                      },
                      ...previous,
                    ]);
                    message.success(`已上传「${file.name}」，待解析`);
                    return false;
                  }}
                >
                  <Button size="small" icon={<UploadOutlined />}>
                    更新
                  </Button>
                </Upload>
              ) : (
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => {
                    if (menu === "mcp") {
                      setMcpForm({
                        name: "",
                        transport: "streamable_http",
                        url: "",
                        headers: "",
                        command: "",
                        args: "",
                        env: "",
                        cwd: "",
                        enabled: true,
                      });
                      setMcpOpen(true);
                      return;
                    }
                    setCreateForm({ name: "", meta: "" });
                    setCreateOpen(true);
                  }}
                >
                  新建
                </Button>
              )
            ) : null}
          </div>

          {menu === "knowledge" ? (
            <div className="dl-agent-knowledge">
              <div className="dl-agent-knowledge-space">
                <div>
                  <span>知识空间</span>
                  <strong>{agent.name} · 专属知识空间</strong>
                </div>
                <div>
                  <span>向量库</span>
                  <strong>
                    {agent.id.replace(/^agent-/, "kb-")} · 跟随智能体
                  </strong>
                </div>
                <div>
                  <span>就绪文档</span>
                  <strong>
                    {knowledge.filter((doc) => doc.status === "已就绪").length}{" "}
                    / {knowledge.length}
                  </strong>
                </div>
              </div>

              <div className="dl-agent-res-items">
                {knowledge.map((doc) => (
                  <div className="dl-agent-knowledge-item" key={doc.id}>
                    <div className="dl-agent-knowledge-head">
                      <strong>{doc.name}</strong>
                      <Tag
                        className={`dl-knowledge-status dl-knowledge-status-${doc.status}`}
                      >
                        {doc.status}
                      </Tag>
                    </div>
                    <div className="dl-agent-knowledge-meta">
                      <span>{doc.size}</span>
                      <span>{doc.updatedAt}</span>
                      <span>
                        {doc.status === "已就绪"
                          ? `${doc.chunks} 个片段`
                          : "等待处理"}
                      </span>
                    </div>
                    {doc.status === "解析中" || doc.status === "向量化中" ? (
                      <Progress
                        percent={doc.status === "解析中" ? 45 : 78}
                        size="small"
                        showInfo={false}
                        strokeColor="#b08d57"
                      />
                    ) : null}
                    {canEdit ? (
                      <div className="dl-agent-knowledge-actions">
                        <Button
                          size="small"
                          onClick={() => startParse(doc.id)}
                          disabled={
                            doc.status === "解析中" || doc.status === "向量化中"
                          }
                        >
                          解析
                        </Button>
                        <Button
                          size="small"
                          icon={<ReloadOutlined />}
                          onClick={() => reindex(doc.id)}
                          disabled={
                            doc.status === "解析中" || doc.status === "向量化中"
                          }
                        >
                          重新向量化
                        </Button>
                        <Button
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={() => removeKnowledgeDoc(doc.id)}
                        >
                          删除
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>

              <div className="dl-agent-knowledge-tip">
                知识空间跟随智能体，不复制到用户目录。用户在其他案件中调用该智能体时，以只读方式挂载这份知识，
                管理员更新文档后所有使用者立即生效；案件产生的临时文件和结果仍写入该案件自己的
                tmp / res。
              </div>
            </div>
          ) : (
            <div className="dl-agent-res-items">
              {currentItems.length === 0 ? (
                <div className="dl-agent-res-empty">暂无配置</div>
              ) : (
                currentItems.map((item) => (
                  <div
                    className={`dl-agent-res-item-card ${
                      menu === "mcp" ? "dl-agent-mcp-card" : ""
                    }`}
                    key={item.name}
                  >
                    <div>
                      <strong>{item.name}</strong>
                      <span>{item.meta}</span>
                      {menu === "mcp" ? (
                        <span className="dl-agent-mcp-address">
                          {item.transport === "stdio"
                            ? "本地进程"
                            : item.url || "未配置地址"}
                        </span>
                      ) : null}
                    </div>
                    {canEdit && menu === "mcp" ? (
                      <Button
                        size="small"
                        onClick={() => {
                          setMcpForm({
                            name: item.name,
                            transport:
                              item.transport === "stdio"
                                ? "stdio"
                                : item.transport === "sse"
                                ? "sse"
                                : "streamable_http",
                            url: item.url ?? "",
                            headers: "",
                            command: "",
                            args: "",
                            env: "",
                            cwd: "",
                            enabled: item.enabled ?? true,
                          });
                          setMcpOpen(true);
                        }}
                      >
                        配置
                      </Button>
                    ) : null}
                    {canEdit ? (
                      <button
                        type="button"
                        aria-label={`删除 ${item.name}`}
                        onClick={() => removeItem(item.name)}
                      >
                        <DeleteOutlined />
                      </button>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          )}
          <div className="dl-agent-res-note">
            {agent.scope === "system"
              ? "系统预设的配置对所有人生效；用户在案件中使用时，这些依赖以只读方式带入案件空间。"
              : "自定义智能体仅自己可见；这里的知识、技能、工具和查询客户端会随该智能体带入你使用的案件空间。"}
          </div>
        </div>
      </aside>

      <section className="dl-agent-debug">
        <div className="dl-agent-debug-head">
          <div>
            <div className="dl-agent-debug-title">问答调试</div>
            <div className="dl-agent-debug-sub">
              当前智能体：{agent.name} · 调试内容不会写入案件
            </div>
          </div>
          <Tag color="processing">调试模式</Tag>
        </div>

        <ChatConversation
          messages={debugMessages}
          agentName={agent.name}
          thinking={thinking}
          thinkingText="正在按当前配置执行调试…"
        >
          {debugMessages.map((item) => (
            <MessageItem
              key={item.id}
              message={item}
              agentName={agent.name}
              userName="我"
              targets={[]}
              approvals={[]}
              onApprove={() => undefined}
              onTerminate={() => undefined}
              onExport={() => message.info("原型：导出调试结果")}
            />
          ))}
        </ChatConversation>

        <Composer
          sessions={[
            {
              id: "debug-session",
              title: "调试会话",
              agentId: agent.id,
              caseId: "DEBUG",
              updatedAt: "刚刚",
              messageCount: debugMessages.length,
            },
          ]}
          activeSessionId="debug-session"
          model={agent.model}
          suggestions={["示例核查流程", "输出结构测试", "无权限处理测试"]}
          placeholder="输入调试问题，查看该智能体的实际回答"
          hintRight="调试内容不会写入案件"
          onSend={sendDebug}
          onSessionChange={() => undefined}
          onNewSession={() => {
            setDebugMessages([]);
            message.success("已清空调试会话");
          }}
          onAttach={() => message.info("原型：添加调试附件")}
        />
      </section>

      <aside className="dl-agent-info">
        <div className="dl-agent-info-head">
          <div className="dl-agent-info-avatar">
            <RobotOutlined />
          </div>
          <div>
            <h1>{agent.name}</h1>
            <div className="dl-agent-info-tags">
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
          </div>
        </div>

        <p className="dl-agent-info-desc">{agent.description}</p>

        <div className="dl-agent-info-actions">
          {canEdit ? (
            <>
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={() => setEditOpen(true)}
              >
                编辑信息
              </Button>
              <Button size="small" icon={<CopyOutlined />} onClick={duplicate}>
                {agent.scope === "custom" ? "另存副本" : "复制"}
              </Button>
              <label className="dl-agent-info-enable">
                <Switch
                  size="small"
                  checked={agent.enabled}
                  onChange={(checked) => onToggleAgent(agent.id, checked)}
                />
                {agent.enabled ? "可用" : "停用"}
              </label>
            </>
          ) : (
            <>
              <Button size="small" icon={<CopyOutlined />} onClick={duplicate}>
                复制为自定义
              </Button>
              <Button
                type="primary"
                size="small"
                icon={<ArrowRightOutlined />}
                disabled={!agent.enabled}
                onClick={() => onUseAgent(agent.id)}
              >
                使用此智能体
              </Button>
            </>
          )}
        </div>

        {isPresetReadOnly ? (
          <div className="dl-agent-preset-note">
            系统预设由管理员统一维护，当前账号只读。如需调整知识库、技能或工具，
            请先「复制为自定义」，在副本上修改。
          </div>
        ) : null}

        <div className="dl-agent-info-section">
          <div className="dl-agent-info-section-title">
            <InfoCircleOutlined /> 基本信息
          </div>
          <div className="dl-agent-info-row">
            <span>所属模块</span>
            <strong>{moduleLabels[agent.module]}</strong>
          </div>
          <div className="dl-agent-info-row">
            <span>模型</span>
            <strong>{agent.model}</strong>
          </div>
          <div className="dl-agent-info-row">
            <span>知识 / 技能 / 工具</span>
            <strong>
              {knowledge.length} / {skills.length} / {tools.length}
            </strong>
          </div>
          <div className="dl-agent-info-row">
            <span>查询客户端</span>
            <strong>{mcpClients.length} 个</strong>
          </div>
          <div className="dl-agent-info-row">
            <span>更新时间</span>
            <strong>{agent.updatedAt}</strong>
          </div>
        </div>

        <div className="dl-agent-info-section">
          <div className="dl-agent-info-section-title">提示词摘要</div>
          <p className="dl-agent-info-prompt">{agent.promptSummary}</p>
        </div>

        <div className="dl-agent-info-section">
          <div className="dl-agent-info-section-title">标签</div>
          <div className="dl-agent-info-taglist">
            {agent.tags.map((tag) => (
              <span key={tag}>{tag}</span>
            ))}
          </div>
        </div>

        <div className="dl-agent-info-note">
          {agent.scope === "system"
            ? "系统预设由管理员统一维护，用户只读；可复制为自定义智能体后自行调优。"
            : `自定义智能体归属 ${
                agent.owner ?? "我"
              }，仅自己可见；任何修改只影响你自己的副本。`}
        </div>
      </aside>

      <Modal
        title={mcpForm.name ? "配置查询客户端" : "新建查询客户端"}
        open={mcpOpen}
        onCancel={() => setMcpOpen(false)}
        onOk={() => {
          if (!mcpForm.name.trim()) return;
          const transportLabel =
            mcpForm.transport === "stdio"
              ? "本地进程"
              : mcpForm.transport === "sse"
              ? "SSE 远程"
              : "远程地址";
          const item = {
            name: mcpForm.name.trim(),
            meta: `${transportLabel} · ${
              mcpForm.enabled ? "已启用" : "已停用"
            }`,
            transport: mcpForm.transport,
            url: mcpForm.url,
            enabled: mcpForm.enabled,
          };
          setMcpClients((previous) => {
            const exists = previous.some((client) => client.name === item.name);
            return exists
              ? previous.map((client) =>
                  client.name === item.name ? { ...client, ...item } : client,
                )
              : [item, ...previous];
          });
          setMcpOpen(false);
          message.success("查询客户端配置已保存");
        }}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ disabled: !mcpForm.name.trim() }}
        width={620}
      >
        <div className="dl-mcp-form">
          <label>
            <span>客户端名称</span>
            <Input
              value={mcpForm.name}
              placeholder="例如：查询中心客户端"
              onChange={(event) =>
                setMcpForm((previous) => ({
                  ...previous,
                  name: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>连接方式</span>
            <div className="dl-mcp-transport">
              {(
                [
                  ["streamable_http", "远程地址"],
                  ["sse", "SSE 远程"],
                  ["stdio", "本地进程"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={
                    mcpForm.transport === value ? "dl-mcp-transport-active" : ""
                  }
                  onClick={() =>
                    setMcpForm((previous) => ({
                      ...previous,
                      transport: value,
                    }))
                  }
                >
                  {label}
                </button>
              ))}
            </div>
          </label>

          {mcpForm.transport === "stdio" ? (
            <>
              <label>
                <span>启动命令</span>
                <Input
                  value={mcpForm.command}
                  placeholder="例如：npx"
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      command: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                <span>命令参数</span>
                <Input
                  value={mcpForm.args}
                  placeholder="每行一个参数，例如：-y"
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      args: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                <span>工作目录</span>
                <Input
                  value={mcpForm.cwd}
                  placeholder="可选，本地进程工作目录"
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      cwd: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                <span>环境变量</span>
                <Input.TextArea
                  value={mcpForm.env}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  placeholder="每行一个 KEY=VALUE"
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      env: event.target.value,
                    }))
                  }
                />
              </label>
            </>
          ) : (
            <>
              <label>
                <span>服务地址</span>
                <Input
                  value={mcpForm.url}
                  placeholder="https://example.internal/mcp"
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      url: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                <span>请求头</span>
                <Input.TextArea
                  value={mcpForm.headers}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  placeholder="每行一个，例如：Authorization: Bearer ..."
                  onChange={(event) =>
                    setMcpForm((previous) => ({
                      ...previous,
                      headers: event.target.value,
                    }))
                  }
                />
              </label>
            </>
          )}

          <label className="dl-mcp-enable">
            <span>启用该客户端</span>
            <Switch
              size="small"
              checked={mcpForm.enabled}
              onChange={(checked) =>
                setMcpForm((previous) => ({ ...previous, enabled: checked }))
              }
            />
          </label>

          <div className="dl-mcp-note">
            查询客户端连接的是平台的查询中心。工具权限由平台侧配置，智能体侧只决定是否启用该客户端。
          </div>
        </div>
      </Modal>

      <Modal
        title="编辑智能体信息"
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={saveAgent}
        okText="保存"
        cancelText="取消"
        width={560}
      >
        <div className="dl-agent-edit-form">
          <label>
            <span>名称</span>
            <Input
              value={editForm.name}
              onChange={(event) =>
                setEditForm((previous) => ({
                  ...previous,
                  name: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>说明</span>
            <Input.TextArea
              value={editForm.description}
              autoSize={{ minRows: 2, maxRows: 4 }}
              onChange={(event) =>
                setEditForm((previous) => ({
                  ...previous,
                  description: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>提示词摘要</span>
            <Input.TextArea
              value={editForm.promptSummary}
              autoSize={{ minRows: 3, maxRows: 6 }}
              onChange={(event) =>
                setEditForm((previous) => ({
                  ...previous,
                  promptSummary: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>模型</span>
            <Input
              value={editForm.model}
              onChange={(event) =>
                setEditForm((previous) => ({
                  ...previous,
                  model: event.target.value,
                }))
              }
            />
          </label>
        </div>
      </Modal>

      <Modal
        title={
          menu === "knowledge"
            ? "更新知识文件"
            : menu === "skills"
            ? "新建技能"
            : menu === "tools"
            ? "新建工具"
            : "新建查询客户端"
        }
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={addResource}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ disabled: !createForm.name.trim() }}
        width={480}
      >
        <div className="dl-agent-edit-form">
          <label>
            <span>名称</span>
            <Input
              value={createForm.name}
              placeholder="请输入名称"
              onChange={(event) =>
                setCreateForm((previous) => ({
                  ...previous,
                  name: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>说明</span>
            <Input
              value={createForm.meta}
              placeholder="请输入说明或地址"
              onChange={(event) =>
                setCreateForm((previous) => ({
                  ...previous,
                  meta: event.target.value,
                }))
              }
            />
          </label>
        </div>
      </Modal>
    </div>
  );
}

export default AgentDetailPage;
