import {
  BellOutlined,
  DownOutlined,
  LogoutOutlined,
  QuestionCircleOutlined,
  SearchOutlined,
  SwapOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Avatar, Badge, Dropdown, Input } from "antd";

import type { CaseRecord, Role } from "../mock";
import { currentUser } from "../mock";

interface TopBarProps {
  pageLabel: string;
  role: Role;
  currentCase: CaseRecord;
  cases: CaseRecord[];
  onCaseChange: (caseRecord: CaseRecord) => void;
  onRoleChange: (role: Role) => void;
  onLogout: () => void;
}

export function TopBar({
  pageLabel,
  role,
  currentCase,
  cases,
  onCaseChange,
  onRoleChange,
  onLogout,
}: TopBarProps) {
  const caseItems = cases.map((caseRecord) => ({
    key: caseRecord.id,
    label: (
      <div style={{ minWidth: 260, padding: "2px 0" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            justifyContent: "space-between",
          }}
        >
          <span style={{ fontFamily: "monospace", fontSize: 11, color: "#24527d" }}>
            {caseRecord.code}
          </span>
          <span style={{ fontSize: 10, color: "#8a8275" }}>
            {caseRecord.status}
          </span>
        </div>
        <div style={{ marginTop: 3, fontSize: 12 }}>{caseRecord.name}</div>
      </div>
    ),
  }));

  const userMenu = {
    items: [
      {
        key: "identity",
        label: (
          <div style={{ padding: "2px 0" }}>
            <div style={{ fontSize: 12, fontWeight: 600 }}>{currentUser.name}</div>
            <div style={{ marginTop: 2, fontSize: 10, color: "#8a8275" }}>
              {currentUser.org}
            </div>
          </div>
        ),
        disabled: true,
      },
      { type: "divider" as const },
      {
        key: "role",
        icon: <SwapOutlined />,
        label: role === "admin" ? "切换为普通用户视角" : "切换为管理员视角",
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
        <div className="dl-crumb">
          <span>工作台</span>
          <span style={{ color: "#c3baac" }}>/</span>
          <span className="dl-crumb-current">{pageLabel}</span>
        </div>

        <Dropdown
          menu={{
            items: caseItems,
            onClick: ({ key }) => {
              const next = cases.find((item) => item.id === key);
              if (next) onCaseChange(next);
            },
          }}
          trigger={["click"]}
          placement="bottomLeft"
        >
          <button className="dl-case-switch" type="button">
            <span className="dl-case-code">{currentCase.code}</span>
            <span className="dl-case-name">{currentCase.name}</span>
            <DownOutlined style={{ fontSize: 9, color: "#9a9185" }} />
          </button>
        </Dropdown>
      </div>

      <div className="dl-topbar-search">
        <Input
          allowClear
          prefix={<SearchOutlined style={{ color: "#9a9185" }} />}
          placeholder="搜索案件、文件、知识…"
          style={{ background: "#f8f5ee", borderColor: "#e2dbcf" }}
        />
      </div>

      <div className="dl-topbar-actions">
        <span className="dl-prototype-chip">设计原型</span>
        <Dropdown
          menu={{
            items: [
              {
                key: "n1",
                label: "案件 A202600001 有新档案",
              },
              {
                key: "n2",
                label: "数据导出审批已通过",
              },
              {
                key: "n3",
                label: "知识空间同步完成",
              },
            ],
          }}
          trigger={["click"]}
        >
          <button className="dl-icon-button" type="button" aria-label="通知">
            <Badge count={3} size="small" offset={[2, -2]}>
              <BellOutlined />
            </Badge>
          </button>
        </Dropdown>
        <button className="dl-icon-button" type="button" aria-label="帮助">
          <QuestionCircleOutlined />
        </button>
        <Dropdown menu={userMenu} trigger={["click"]} placement="bottomRight">
          <button
            type="button"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              border: 0,
              background: "transparent",
              cursor: "pointer",
              padding: 2,
            }}
          >
            <Avatar
              size={30}
              style={{
                background: "linear-gradient(135deg, #2c5a86, #10263d)",
                fontSize: 12,
              }}
              icon={<UserOutlined />}
            >
              {currentUser.avatar}
            </Avatar>
            <span style={{ fontSize: 12, color: "#4d5a66" }}>
              {currentUser.name}
            </span>
          </button>
        </Dropdown>
      </div>
    </header>
  );
}

export default TopBar;
