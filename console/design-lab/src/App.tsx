import { useEffect, useState } from "react";

import type { CaseRecord, Role } from "./mock";
import { cases } from "./mock";
import { CasePage } from "./components/CasePage";
import { ChatWorkspace } from "./components/ChatWorkspace";
import { LoginPage } from "./components/LoginPage";
import { PlaceholderPage } from "./components/PlaceholderPage";
import { Sidebar, type PageKey } from "./components/Sidebar";
import { TopBar } from "./components/TopBar";

const pageLabels: Record<PageKey, string> = {
  chat: "智能研判",
  cases: "案件",
  files: "文件",
  knowledge: "知识空间",
  data: "数据空间",
  approvals: "审批中心",
  inbox: "收件箱",
  cron: "定时任务",
  skills: "技能",
  tools: "工具",
  users: "用户与权限",
  audit: "审计日志",
};

const adminOnlyPages = new Set<PageKey>(["users", "audit"]);

export default function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [role, setRole] = useState<Role>("admin");
  const [page, setPage] = useState<PageKey>("chat");
  const [currentCase, setCurrentCase] = useState<CaseRecord>(cases[0]);

  useEffect(() => {
    if (role === "user" && adminOnlyPages.has(page)) {
      setPage("chat");
    }
  }, [page, role]);

  if (!authenticated) {
    return <LoginPage onLogin={() => setAuthenticated(true)} />;
  }

  const openChat = (caseRecord: CaseRecord) => {
    setCurrentCase(caseRecord);
    setPage("chat");
  };

  return (
    <div className="dl-app">
      <Sidebar
        role={role}
        activePage={page}
        onNavigate={setPage}
        userName="张三"
        roleLabel={role === "admin" ? "系统管理员" : "案件研判岗"}
        org="情报研判中心"
        avatar="张"
      />
      <div className="dl-main">
        <TopBar
          pageLabel={pageLabels[page]}
          role={role}
          currentCase={currentCase}
          cases={cases}
          onCaseChange={setCurrentCase}
          onRoleChange={setRole}
          onLogout={() => {
            setAuthenticated(false);
            setPage("chat");
          }}
        />
        <main className="dl-content">
          {page === "chat" ? (
            <ChatWorkspace currentCase={currentCase} />
          ) : page === "cases" ? (
            <CasePage onOpenChat={openChat} />
          ) : (
            <PlaceholderPage page={page} />
          )}
        </main>
      </div>
    </div>
  );
}
