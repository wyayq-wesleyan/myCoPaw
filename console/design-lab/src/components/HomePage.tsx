import type { ReactNode } from "react";
import {
  ArrowRightOutlined,
  BellOutlined,
  ClockCircleOutlined,
  FileSearchOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  RadarChartOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { Badge } from "antd";

import type {
  CaseRecord,
  ModuleKey,
  NotificationRecord,
  Role,
  SessionRecord,
} from "../mock";
import { currentUser } from "../mock";

interface HomePageProps {
  role: Role;
  cases: CaseRecord[];
  notifications: NotificationRecord[];
  sessions: SessionRecord[];
  onOpenModule: (module: ModuleKey) => void;
  onOpenAgentManagement: () => void;
  onOpenNotifications: () => void;
  onOpenSession: (session: SessionRecord) => void;
}

interface ModuleCard {
  key: ModuleKey;
  index: string;
  title: string;
  subtitle: string;
  description?: string;
  icon: ReactNode;
  tags: string[];
  planned?: boolean;
}

const moduleCards: ModuleCard[] = [
  {
    key: "office",
    index: "01",
    title: "办公助手",
    subtitle: "不依赖案件",
    description:
      "公文起草、材料总结、表格整理与日常问答，管理员和普通用户均可使用。",
    icon: <MessageOutlined />,
    tags: ["公文", "总结", "非案件"],
  },
  {
    key: "verify",
    index: "02",
    title: "信息核查",
    subtitle: "基于案件",
    icon: <FileSearchOutlined />,
    tags: ["按案件", "在控目标", "审批"],
  },
  {
    key: "archive",
    index: "03",
    title: "档案经营",
    subtitle: "长期经营",
    description:
      "围绕重点人员和特定群体，按既定流程进行定时、持续的查询与分析。",
    icon: <FolderOpenOutlined />,
    tags: ["定时任务", "重点人员"],
    planned: true,
  },
  {
    key: "monitor",
    index: "04",
    title: "信息监测",
    subtitle: "持续监测",
    description: "对特定目标进行持续监测、异常发现和预警，形成监测记录。",
    icon: <RadarChartOutlined />,
    tags: ["监测", "预警"],
    planned: true,
  },
];

export function HomePage({
  role,
  cases,
  notifications,
  sessions,
  onOpenModule,
  onOpenAgentManagement,
  onOpenNotifications,
  onOpenSession,
}: HomePageProps) {
  const user = currentUser;
  const unread = notifications.filter(
    (item) => item.status === "审批中" || item.status === "待确认",
  ).length;

  const handleModule = (module: ModuleCard) => {
    if (module.planned) return;
    onOpenModule(module.key);
  };

  return (
    <div className="dl-home">
      <header className="dl-home-header">
        <div className="dl-home-brand">
          <div className="dl-brand-mark">汇</div>
          <div>
            <div className="dl-home-brand-title">汇智小助手</div>
            <div className="dl-home-brand-sub">智能业务工作台</div>
          </div>
        </div>

        <nav className="dl-home-nav">
          <button type="button" onClick={onOpenAgentManagement}>
            <RobotOutlined />
            智能体管理
          </button>
          <button type="button" onClick={onOpenNotifications}>
            <Badge count={unread} size="small" offset={[4, -2]}>
              <BellOutlined />
            </Badge>
            通知中心
          </button>
          <div className="dl-home-user">
            <span className="dl-home-user-avatar">{user.avatar}</span>
            <span className="dl-home-user-copy">
              <span>{user.name}</span>
              <small>
                {role === "admin" ? "案件研判岗 · 管理角色" : "案件研判岗"}
              </small>
            </span>
          </div>
        </nav>
      </header>

      <main className="dl-home-main">
        <section className="dl-home-hero">
          <div className="dl-home-greeting">
            <div className="dl-home-eyebrow">2026-11-16 · 周一</div>
            <h1>
              上午好，{user.name}
              <span className="dl-home-dot">.</span>
            </h1>
          </div>

          <div className="dl-home-recent">
            <div className="dl-home-recent-head">
              <span>
                <ClockCircleOutlined /> 最近会话
              </span>
              <span className="dl-home-recent-count">
                {sessions.length > 0 ? `${sessions.length} 个` : ""}
              </span>
            </div>
            {sessions.length === 0 ? (
              <div className="dl-home-recent-empty">暂无最近会话</div>
            ) : (
              <div className="dl-home-recent-list">
                {sessions.slice(0, 3).map((session) => {
                  const sessionCase = cases.find(
                    (item) => item.id === session.caseId,
                  );
                  return (
                    <button
                      key={session.id}
                      type="button"
                      className="dl-home-recent-item"
                      onClick={() => onOpenSession(session)}
                    >
                      <span className="dl-home-recent-title">
                        {session.title}
                      </span>
                      <span className="dl-home-recent-meta">
                        {sessionCase?.code} · {session.updatedAt}
                      </span>
                      <ArrowRightOutlined className="dl-home-recent-arrow" />
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        <section className="dl-module-section">
          <div className="dl-section-head">
            <div>
              <div className="dl-section-kicker">工作区</div>
              <h2>进入工作区</h2>
            </div>
          </div>

          <div className="dl-module-grid">
            {moduleCards.map((module) => {
              const disabled = module.planned;
              return (
                <button
                  key={module.key}
                  type="button"
                  disabled={disabled}
                  className={`dl-module-card ${
                    disabled ? "dl-module-card-disabled" : ""
                  }`}
                  onClick={() => handleModule(module)}
                >
                  <div className="dl-module-card-top">
                    <span className="dl-module-index">{module.index}</span>
                    <span className="dl-module-icon">{module.icon}</span>
                  </div>
                  <div className="dl-module-title-row">
                    <h3>{module.title}</h3>
                    <span className="dl-module-subtitle">
                      {module.subtitle}
                    </span>
                  </div>
                  {module.description ? (
                    <p className="dl-module-desc">{module.description}</p>
                  ) : null}
                  <div className="dl-module-tags">
                    {module.tags.map((tag) => (
                      <span key={tag}>{tag}</span>
                    ))}
                  </div>
                  <div className="dl-module-foot">
                    {module.planned ? (
                      <span className="dl-module-state">
                        <ClockCircleOutlined /> 规划中
                      </span>
                    ) : (
                      <span className="dl-module-enter">
                        进入工作区 <ArrowRightOutlined />
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        <footer className="dl-home-footer">
          <span>汇智小助手 · 智能业务工作台</span>
          <span className="dl-home-footer-right">内部使用</span>
        </footer>
      </main>
    </div>
  );
}

export default HomePage;
