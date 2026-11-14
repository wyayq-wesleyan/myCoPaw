import type { ReactNode } from "react";
import {
  AuditOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  MessageOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
  ToolOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

import type { Role } from "../mock";

export type PageKey =
  | "chat"
  | "cases"
  | "files"
  | "knowledge"
  | "data"
  | "approvals"
  | "inbox"
  | "cron"
  | "skills"
  | "tools"
  | "users"
  | "audit";

interface NavItem {
  key: PageKey;
  label: string;
  icon: ReactNode;
  adminOnly?: boolean;
  badge?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const navGroups: NavGroup[] = [
  {
    label: "工作台",
    items: [
      { key: "chat", label: "智能研判", icon: <MessageOutlined /> },
      { key: "cases", label: "案件", icon: <FolderOpenOutlined /> },
      { key: "files", label: "文件", icon: <FileTextOutlined /> },
      { key: "knowledge", label: "知识空间", icon: <SafetyCertificateOutlined /> },
      { key: "data", label: "数据空间", icon: <DatabaseOutlined /> },
    ],
  },
  {
    label: "协同",
    items: [
      { key: "approvals", label: "审批中心", icon: <AuditOutlined />, badge: "2" },
      { key: "inbox", label: "收件箱", icon: <InboxOutlined />, badge: "3" },
      { key: "cron", label: "定时任务", icon: <ClockCircleOutlined /> },
    ],
  },
  {
    label: "资源与管理",
    items: [
      { key: "skills", label: "技能", icon: <ThunderboltOutlined /> },
      { key: "tools", label: "工具", icon: <ToolOutlined /> },
      { key: "users", label: "用户与权限", icon: <TeamOutlined />, adminOnly: true },
      { key: "audit", label: "审计日志", icon: <SettingOutlined />, adminOnly: true },
    ],
  },
];

interface SidebarProps {
  role: Role;
  activePage: PageKey;
  onNavigate: (page: PageKey) => void;
  userName: string;
  roleLabel: string;
  org: string;
  avatar: string;
}

export function Sidebar({
  role,
  activePage,
  onNavigate,
  userName,
  roleLabel,
  org,
  avatar,
}: SidebarProps) {
  return (
    <aside className="dl-sidebar">
      <div className="dl-brand">
        <div className="dl-brand-mark">汇</div>
        <div className="dl-brand-copy">
          <div className="dl-brand-title">汇智小助手</div>
          <div className="dl-brand-sub">智能研判工作台</div>
          <div className="dl-env-badge">内网生产环境</div>
        </div>
      </div>

      <nav className="dl-nav">
        {navGroups.map((group) => {
          const items = group.items.filter(
            (item) => !item.adminOnly || role === "admin",
          );
          if (items.length === 0) return null;
          return (
            <div className="dl-nav-group" key={group.label}>
              <div className="dl-nav-group-label">{group.label}</div>
              {items.map((item) => (
                <button
                  className={`dl-nav-item ${
                    activePage === item.key ? "dl-nav-item-active" : ""
                  }`}
                  key={item.key}
                  onClick={() => onNavigate(item.key)}
                  type="button"
                >
                  <span className="dl-nav-icon">{item.icon}</span>
                  <span className="dl-nav-label">{item.label}</span>
                  {item.badge ? (
                    <span className="dl-nav-badge">{item.badge}</span>
                  ) : null}
                </button>
              ))}
            </div>
          );
        })}
      </nav>

      <div className="dl-sidebar-footer">
        <div className="dl-connection">
          <span className="dl-connection-dot" />
          <span>平台连接正常</span>
          <span style={{ marginLeft: "auto", opacity: 0.6 }}>
            {role === "admin" ? "管理员" : "普通用户"}
          </span>
        </div>
        <div className="dl-user-card">
          <div className="dl-avatar">{avatar}</div>
          <div className="dl-user-copy">
            <div className="dl-user-name">{userName}</div>
            <div className="dl-user-role">
              {roleLabel} · {org}
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}

export default Sidebar;
