import { useState } from "react";
import {
  AuditOutlined,
  BellOutlined,
  CheckCircleFilled,
  ClockCircleFilled,
  FileDoneOutlined,
  LinkOutlined,
  MessageOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Button, Empty, Tabs, Tag, message } from "antd";

import type { NotificationRecord, Role } from "../mock";

interface NotificationPageProps {
  role: Role;
  notifications: NotificationRecord[];
  onOpenVerify: () => void;
}

function noticeIcon(kind: NotificationRecord["kind"]) {
  if (kind === "approval") return <AuditOutlined />;
  if (kind === "task") return <FileDoneOutlined />;
  return <BellOutlined />;
}

function statusTag(status?: string) {
  if (!status) return null;
  if (status === "审批中") {
    return (
      <Tag icon={<ClockCircleFilled />} color="processing">
        {status}
      </Tag>
    );
  }
  if (status === "已通过" || status === "已完成") {
    return (
      <Tag icon={<CheckCircleFilled />} color="success">
        {status}
      </Tag>
    );
  }
  if (status === "待确认") {
    return <Tag color="gold">{status}</Tag>;
  }
  return <Tag>{status}</Tag>;
}

export function NotificationPage({
  role,
  notifications,
  onOpenVerify,
}: NotificationPageProps) {
  const visible =
    role === "admin"
      ? notifications.filter((item) => item.kind === "notice")
      : notifications;
  const [tab, setTab] = useState("approval");
  const approvals = visible.filter((item) => item.kind === "approval");
  const tasks = visible.filter((item) => item.kind === "task");
  const notices = visible.filter((item) => item.kind === "notice");

  const renderList = (items: NotificationRecord[]) => {
    if (items.length === 0) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            role === "admin" ? "管理员不接收案件审批与任务通知" : "暂无通知"
          }
        />
      );
    }
    return (
      <div className="dl-notice-list">
        {items.map((item) => (
          <article key={item.id} className="dl-notice-item">
            <div className={`dl-notice-icon dl-notice-icon-${item.kind}`}>
              {noticeIcon(item.kind)}
            </div>
            <div className="dl-notice-main">
              <div className="dl-notice-title-row">
                <h3>{item.title}</h3>
                {statusTag(item.status)}
              </div>
              <p>{item.description}</p>
              <div className="dl-notice-meta">
                <span>{item.time}</span>
                {item.caseCode ? (
                  <span className="dl-notice-case">{item.caseCode}</span>
                ) : null}
              </div>
            </div>
            <div className="dl-notice-actions">
              {item.kind === "approval" ? (
                <Button
                  size="small"
                  icon={<LinkOutlined />}
                  onClick={() =>
                    item.status === "审批中"
                      ? message.info("原型：将打开 审批表单页面")
                      : message.info("原型：查看 审批记录")
                  }
                >
                  {item.status === "审批中" ? "打开审批表单" : "查看审批记录"}
                </Button>
              ) : item.kind === "task" ? (
                <Button
                  size="small"
                  icon={<MessageOutlined />}
                  onClick={onOpenVerify}
                  disabled={role === "admin"}
                >
                  查看会话
                </Button>
              ) : role === "admin" ? (
                <Button
                  size="small"
                  onClick={() => message.info("原型：管理员系统通知详情")}
                >
                  查看
                </Button>
              ) : (
                <Button
                  size="small"
                  type="primary"
                  ghost
                  onClick={onOpenVerify}
                >
                  去会话确认
                </Button>
              )}
            </div>
          </article>
        ))}
      </div>
    );
  };

  return (
    <main className="dl-notice-page">
      <header className="dl-page-head">
        <div>
          <div className="dl-page-kicker">通知中心</div>
          <h1>通知中心</h1>
          <p>
            集中查看审批、智能体任务和系统通知；审批表单在审批系统页面填写，工作台只负责发起与回跳。
          </p>
        </div>
        <div className="dl-page-head-actions">
          <span className="dl-notice-summary">
            <SafetyCertificateOutlined />
            审批与任务由平台回调更新
          </span>
        </div>
      </header>

      {role === "admin" ? (
        <div className="dl-agents-admin-note">
          <SafetyCertificateOutlined />
          <span>
            管理员账号不接收案件审批与任务通知，仅展示系统级通知；案件通知请由业务账号查看。
          </span>
        </div>
      ) : null}

      <div className="dl-notice-panel">
        <Tabs
          activeKey={role === "admin" ? "notice" : tab}
          onChange={setTab}
          items={
            role === "admin"
              ? [
                  {
                    key: "notice",
                    label: (
                      <span>
                        系统通知 <em>{notices.length}</em>
                      </span>
                    ),
                    children: renderList(notices),
                  },
                ]
              : [
                  {
                    key: "approval",
                    label: (
                      <span>
                        审批 <em>{approvals.length}</em>
                      </span>
                    ),
                    children: renderList(approvals),
                  },
                  {
                    key: "task",
                    label: (
                      <span>
                        任务 <em>{tasks.length}</em>
                      </span>
                    ),
                    children: renderList(tasks),
                  },
                  {
                    key: "notice",
                    label: (
                      <span>
                        通知 <em>{notices.length}</em>
                      </span>
                    ),
                    children: renderList(notices),
                  },
                ]
          }
        />
      </div>

      <div className="dl-notice-foot">
        <span>
          <AuditOutlined /> 审批发起后由 接入服务 创建 审批流程
        </span>
        <span>
          <CheckCircleFilled /> 审批通过后回调业务系统恢复查询
        </span>
        <span>审批记录与审计日志可在平台侧追溯</span>
      </div>
    </main>
  );
}

export default NotificationPage;
