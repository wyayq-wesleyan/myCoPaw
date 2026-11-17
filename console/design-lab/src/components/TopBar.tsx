import {
  ArrowLeftOutlined,
  BellOutlined,
  DownOutlined,
  LogoutOutlined,
  RobotOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import { Avatar, Badge, Dropdown, Tooltip } from "antd";

import type { AgentRecord, CaseRecord, Role, Screen } from "../mock";
import { currentUser } from "../mock";

interface TopBarProps {
  screen: "office" | "verify" | "agents" | "notifications" | "agentDetail";
  role: Role;
  agents: AgentRecord[];
  agentId: string;
  currentCase?: CaseRecord;
  unreadCount: number;
  backLabel: string;
  onBack: () => void;
  onAgentChange: (agentId: string) => void;
  onNavigate: (screen: Screen) => void;
  onRoleChange: (role: Role) => void;
  onLogout: () => void;
}

const screenTitles: Record<TopBarProps["screen"], string> = {
  verify: "信息核查",
  office: "办公助手",
  agents: "智能体管理",
  agentDetail: "智能体详情",
  notifications: "通知中心",
};

const screenContexts: Record<TopBarProps["screen"], string> = {
  verify: "按案件核查 · 权限不足时发起审批",
  office: "不依赖案件 · 文档与写作",
  agents: "全部智能体 · 可筛选查看",
  agentDetail: "知识空间 · 技能 · 工具 · 查询客户端",
  notifications: "审批、任务与系统通知",
};

export function TopBar({
  screen,
  role,
  agents,
  agentId,
  currentCase,
  unreadCount,
  backLabel,
  onBack,
  onAgentChange,
  onNavigate,
  onRoleChange,
  onLogout,
}: TopBarProps) {
  // 统一身份登录：账号始终是当前登录人，角色决定能做什么。
  const user = currentUser;
  const selectedAgent = agents.find((agent) => agent.id === agentId);
  const selectableAgents = agents.filter(
    (agent) =>
      agent.enabled &&
      agent.module === (screen === "office" ? "office" : "verify"),
  );

  const agentItems = selectableAgents.map((agent) => ({
    key: agent.id,
    label: (
      <div className="dl-agent-menu-item">
        <div className="dl-agent-menu-top">
          <span className="dl-agent-menu-name">{agent.name}</span>
          {agent.id === agentId ? (
            <span className="dl-agent-menu-current">当前</span>
          ) : null}
        </div>
        <div className="dl-agent-menu-desc">{agent.description}</div>
      </div>
    ),
  }));

  const userMenu = {
    items: [
      {
        key: "identity",
        label: (
          <div className="dl-user-menu-identity">
            <div className="dl-user-menu-name">{user.name}</div>
            <div className="dl-user-menu-org">{user.org}</div>
            <div className="dl-user-menu-role">
              {role === "admin" ? "管理角色（可管理智能体）" : user.roleLabel}
            </div>
          </div>
        ),
        disabled: true,
      },
      { type: "divider" as const },
      {
        key: "role",
        icon: <SwapOutlined />,
        label:
          role === "admin"
            ? "切换为普通业务账号视角（演示）"
            : "切换为管理角色视角（演示）",
      },
      { type: "divider" as const },
      { key: "logout", icon: <LogoutOutlined />, label: "退出登录" },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === "role") {
        onRoleChange(role === "admin" ? "user" : "admin");
      }
      if (key === "logout") {
        onLogout();
      }
    },
  };

  return (
    <header className="dl-topbar">
      <div className="dl-topbar-left">
        <button className="dl-topbar-back" type="button" onClick={onBack}>
          <ArrowLeftOutlined />
          <span>{backLabel}</span>
        </button>

        <div className="dl-topbar-title">
          <span className="dl-topbar-module">{screenTitles[screen]}</span>
          <span className="dl-topbar-context">{screenContexts[screen]}</span>
        </div>

        {screen === "verify" && currentCase ? (
          <div className="dl-case-context">
            <span className="dl-case-code">{currentCase.code}</span>
            <span className="dl-case-context-name">{currentCase.name}</span>
            <span className="dl-case-context-meta">
              在控 {currentCase.targets}
            </span>
          </div>
        ) : null}
      </div>

      <div className="dl-topbar-right">
        {screen === "verify" || screen === "office" ? (
          <Dropdown
            menu={{
              items: agentItems,
              onClick: ({ key }) => onAgentChange(key),
            }}
            trigger={["click"]}
            placement="bottomRight"
            overlayClassName="dl-agent-dropdown"
          >
            <button className="dl-agent-switch" type="button">
              <span className="dl-agent-switch-icon">
                <RobotOutlined />
              </span>
              <span className="dl-agent-switch-copy">
                <span className="dl-agent-switch-label">智能体</span>
                <span className="dl-agent-switch-name">
                  {selectedAgent?.name ?? "选择智能体"}
                </span>
              </span>
              <DownOutlined className="dl-agent-switch-arrow" />
            </button>
          </Dropdown>
        ) : null}

        <span className="dl-prototype-chip">设计原型</span>

        <Tooltip title="通知中心">
          <button
            className="dl-icon-button"
            type="button"
            onClick={() => onNavigate("notifications")}
            aria-label="通知中心"
          >
            <Badge count={unreadCount} size="small" offset={[2, -2]}>
              <BellOutlined />
            </Badge>
          </button>
        </Tooltip>

        <Dropdown menu={userMenu} trigger={["click"]} placement="bottomRight">
          <button className="dl-user-button" type="button">
            <Avatar
              size={30}
              className={
                role === "admin" ? "dl-avatar-admin" : "dl-avatar-user"
              }
            >
              {user.avatar}
            </Avatar>
            <span className="dl-user-button-copy">
              <span className="dl-user-button-name">{user.name}</span>
              <span className="dl-user-button-role">
                {role === "admin" ? "业务兼管理角色" : "业务角色"}
              </span>
            </span>
            <DownOutlined className="dl-user-button-arrow" />
          </button>
        </Dropdown>
      </div>
    </header>
  );
}

export default TopBar;
