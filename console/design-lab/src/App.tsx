import { useEffect, useMemo, useState } from "react";
import { message } from "antd";

import type {
  AgentModule,
  AgentRecord,
  CaseRecord,
  ModuleKey,
  Role,
  Screen,
  SessionRecord,
} from "./mock";
import {
  cases,
  initialAgents,
  notifications,
  sessions as initialSessions,
} from "./mock";
import { AgentDetailPage } from "./components/AgentDetailPage";
import { AgentManagementPage } from "./components/AgentManagementPage";
import { HomePage } from "./components/HomePage";
import { LoginPage } from "./components/LoginPage";
import { NotificationPage } from "./components/NotificationPage";
import { OfficeWorkspace } from "./components/OfficeWorkspace";
import { TopBar } from "./components/TopBar";
import { VerifyWorkspace } from "./components/VerifyWorkspace";

const initialAgentByModule: Record<AgentModule, string> = {
  verify: "agent-verify-default",
  office: "agent-office-default",
};

export default function App() {
  const [screen, setScreen] = useState<Screen>("login");
  const [role, setRole] = useState<Role>("user");
  const [caseRecords, setCaseRecords] = useState<CaseRecord[]>(cases);
  const [currentCase, setCurrentCase] = useState<CaseRecord>(cases[0]);
  const [agents, setAgents] = useState<AgentRecord[]>(initialAgents);
  const [agentByModule, setAgentByModule] =
    useState<Record<AgentModule, string>>(initialAgentByModule);
  const [sessions, setSessions] = useState<SessionRecord[]>(initialSessions);
  const [activeSessionId, setActiveSessionId] = useState("sess-001");
  const [detailAgentId, setDetailAgentId] = useState<string | null>(null);

  const visibleNotifications = useMemo(
    () =>
      role === "admin"
        ? notifications.filter((item) => item.kind === "notice")
        : notifications,
    [role],
  );
  // 原型只有一个登录账号（张三，同时具备管理角色）：
  // 系统预设 + 该账号的自定义智能体都可见。
  const agentsForRole = agents;
  const unreadCount = visibleNotifications.filter(
    (item) => item.status === "审批中" || item.status === "待确认",
  ).length;

  const verifyAgents = useMemo(
    () =>
      agentsForRole.filter(
        (agent) => agent.module === "verify" && agent.enabled,
      ),
    [agentsForRole],
  );
  const officeAgents = useMemo(
    () =>
      agentsForRole.filter(
        (agent) => agent.module === "office" && agent.enabled,
      ),
    [agentsForRole],
  );

  useEffect(() => {
    if (
      screen === "verify" &&
      !verifyAgents.some((agent) => agent.id === agentByModule.verify)
    ) {
      const fallback = verifyAgents[0];
      if (fallback) {
        setAgentByModule((previous) => ({ ...previous, verify: fallback.id }));
      }
    }
    if (
      screen === "office" &&
      !officeAgents.some((agent) => agent.id === agentByModule.office)
    ) {
      const fallback = officeAgents[0];
      if (fallback) {
        setAgentByModule((previous) => ({ ...previous, office: fallback.id }));
      }
    }
  }, [screen, verifyAgents, officeAgents, agentByModule]);

  const handleOpenAgentDetail = (agentId: string) => {
    setDetailAgentId(agentId);
    setScreen("agentDetail");
  };

  const createSession = (agentId: string, caseId: string) => {
    const nextId = `sess-${Date.now()}`;
    const caseRecord = caseRecords.find((item) => item.id === caseId);
    const nextSession: SessionRecord = {
      id: nextId,
      title: `新会话 · ${caseRecord?.code ?? "案件"}`,
      agentId,
      caseId,
      updatedAt: "刚刚",
      messageCount: 0,
    };
    setSessions((previous) => [nextSession, ...previous]);
    setActiveSessionId(nextId);
    return nextId;
  };

  const openVerifyAgent = (agentId: string) => {
    const agent = agents.find((item) => item.id === agentId);
    if (!agent) return;
    if (role === "admin") {
      message.warning(
        "管理员账号不可进入案件类业务，请在普通用户视角下使用信息核查。",
      );
      return;
    }
    if (agent.module !== "verify" || !agent.enabled) return;
    setAgentByModule((previous) => ({ ...previous, verify: agentId }));
    const existing = sessions.find(
      (session) =>
        session.caseId === currentCase.id && session.agentId === agentId,
    );
    if (existing) {
      setActiveSessionId(existing.id);
    } else {
      createSession(agentId, currentCase.id);
    }
    setScreen("verify");
  };

  const handleAgentChange = (agentId: string) => {
    const agent = agentsForRole.find((item) => item.id === agentId);
    if (!agent || !agent.enabled) return;
    const module = agent.module;
    if (module === "verify" && role === "admin") {
      message.warning("管理员不可使用信息核查智能体。");
      return;
    }
    setAgentByModule((previous) => ({ ...previous, [module]: agentId }));

    if (module === "verify") {
      const existing = sessions.find(
        (session) =>
          session.caseId === currentCase.id && session.agentId === agentId,
      );
      if (existing) {
        setActiveSessionId(existing.id);
        message.info(`已切换到「${agent.name}」在当前案件下的会话。`);
      } else {
        createSession(agentId, currentCase.id);
        message.info(`「${agent.name}」在当前案件下没有历史会话，已新建会话。`);
      }
    } else {
      message.info(`已切换办公智能体为「${agent.name}」。`);
    }
  };

  const handleDeleteCases = (caseIds: string[]) => {
    const remaining = caseRecords.filter((item) => !caseIds.includes(item.id));
    if (remaining.length === 0) {
      message.warning("至少保留一个案件");
      return;
    }
    setCaseRecords(remaining);
    setSessions((previous) =>
      previous.filter((session) => !caseIds.includes(session.caseId)),
    );
    if (caseIds.includes(currentCase.id)) {
      const nextCase = remaining[0];
      setCurrentCase(nextCase);
      const existing =
        sessions.find(
          (session) =>
            session.caseId === nextCase.id &&
            session.agentId === agentByModule.verify,
        ) ?? sessions.find((session) => session.caseId === nextCase.id);
      if (existing) {
        setActiveSessionId(existing.id);
        setAgentByModule((previous) => ({
          ...previous,
          verify: existing.agentId,
        }));
      } else {
        createSession(agentByModule.verify, nextCase.id);
      }
    }
    message.success(`已从工作台移除 ${caseIds.length} 个案件`);
  };

  const handleSelectCase = (caseRecord: CaseRecord) => {
    setCurrentCase(caseRecord);
    const currentAgentId = agentByModule.verify;
    const existing =
      sessions.find(
        (session) =>
          session.caseId === caseRecord.id &&
          session.agentId === currentAgentId,
      ) ?? sessions.find((session) => session.caseId === caseRecord.id);
    if (existing) {
      setActiveSessionId(existing.id);
      setAgentByModule((previous) => ({
        ...previous,
        verify: existing.agentId,
      }));
    } else {
      createSession(currentAgentId, caseRecord.id);
    }
  };

  const handleSelectSession = (sessionId: string) => {
    const session = sessions.find((item) => item.id === sessionId);
    if (!session) return;
    setActiveSessionId(sessionId);
    const caseRecord = caseRecords.find((item) => item.id === session.caseId);
    if (caseRecord) setCurrentCase(caseRecord);
    setAgentByModule((previous) => ({
      ...previous,
      verify: session.agentId,
    }));
    if (screen !== "verify") setScreen("verify");
  };

  const handleOpenHomeSession = (session: SessionRecord) => {
    const caseRecord = caseRecords.find((item) => item.id === session.caseId);
    if (caseRecord) setCurrentCase(caseRecord);
    setActiveSessionId(session.id);
    setAgentByModule((previous) => ({
      ...previous,
      verify: session.agentId,
    }));
    setScreen("verify");
  };

  const handleUpdateAgent = (agentId: string, patch: Partial<AgentRecord>) => {
    setAgents((previous) =>
      previous.map((agent) =>
        agent.id === agentId ? { ...agent, ...patch } : agent,
      ),
    );
    message.success("智能体配置已更新");
  };

  const handleDuplicateAgent = (agentId: string) => {
    const base = agents.find((agent) => agent.id === agentId);
    if (!base) throw new Error("智能体不存在");
    // 复制一律生成「自定义」智能体，归属当前登录用户；系统预设本身保持不变。
    const nextAgent: AgentRecord = {
      ...base,
      id: `agent-custom-${Date.now()}`,
      name: `${base.name}·我的副本`,
      scope: "custom",
      baseName: base.name,
      owner: "张三",
      enabled: true,
      updatedAt: "刚刚",
      tags: ["自定义", "复制自预设"],
    };
    setAgents((previous) => [nextAgent, ...previous]);
    return nextAgent;
  };

  const handleDeleteAgents = (agentIds: string[]) => {
    setAgents((previous) =>
      previous.filter((agent) => !agentIds.includes(agent.id)),
    );
    if (detailAgentId && agentIds.includes(detailAgentId)) {
      setDetailAgentId(null);
      setScreen("agents");
    }
  };

  const handleToggleAgent = (agentId: string, enabled: boolean) => {
    setAgents((previous) =>
      previous.map((agent) =>
        agent.id === agentId ? { ...agent, enabled } : agent,
      ),
    );
    message.success(
      enabled
        ? "智能体已启用，可在智能体选择列表中使用。"
        : "智能体已停用，默认列表不再显示；历史会话仍可追溯。",
    );
  };

  const handleUseAgent = (agentId: string) => {
    const agent = agentsForRole.find((item) => item.id === agentId);
    if (!agent) return;
    if (agent.module === "verify") {
      openVerifyAgent(agentId);
    } else {
      setAgentByModule((previous) => ({ ...previous, office: agentId }));
      setScreen("office");
    }
  };

  const navigate = (nextScreen: Screen) => {
    if (nextScreen === "verify") {
      const selected = agents.find(
        (agent) => agent.id === agentByModule.verify && agent.enabled,
      );
      if (!selected) {
        const fallback = verifyAgents[0];
        if (fallback) {
          setAgentByModule((previous) => ({
            ...previous,
            verify: fallback.id,
          }));
        }
      }
    }
    setScreen(nextScreen);
  };

  const handleLogin = (nextRole: Role) => {
    setRole(nextRole);
    setScreen("home");
    if (nextRole === "admin") {
      const office = officeAgents[0];
      if (office) {
        setAgentByModule((previous) => ({ ...previous, office: office.id }));
      }
    }
  };

  const handleRoleChange = (nextRole: Role) => {
    setRole(nextRole);
    if (nextRole === "admin" && screen === "verify") {
      setScreen("home");
    }
    message.info(
      nextRole === "admin"
        ? "已切换为管理角色视角（演示）。"
        : "已切换为普通业务账号视角（演示）。",
    );
  };

  const handleModuleOpen = (module: ModuleKey) => {
    if (module === "archive" || module === "monitor") {
      message.info("该模块仍在规划中。");
      return;
    }
    setScreen(module);
  };

  if (screen === "login") {
    return <LoginPage onLogin={handleLogin} />;
  }

  if (screen === "home") {
    return (
      <HomePage
        role={role}
        notifications={visibleNotifications}
        sessions={sessions}
        cases={caseRecords}
        onOpenModule={handleModuleOpen}
        onOpenAgentManagement={() => setScreen("agents")}
        onOpenNotifications={() => setScreen("notifications")}
        onOpenSession={handleOpenHomeSession}
      />
    );
  }

  const detailAgent = agentsForRole.find((agent) => agent.id === detailAgentId);

  const topBar = (
    <TopBar
      screen={
        screen as
          | "office"
          | "verify"
          | "agents"
          | "notifications"
          | "agentDetail"
      }
      role={role}
      agents={agentsForRole}
      agentId={
        screen === "verify" ? agentByModule.verify : agentByModule.office
      }
      currentCase={currentCase}
      unreadCount={unreadCount}
      backLabel={screen === "agentDetail" ? "返回智能体管理" : "返回首页"}
      onBack={() => setScreen(screen === "agentDetail" ? "agents" : "home")}
      onAgentChange={handleAgentChange}
      onNavigate={navigate}
      onRoleChange={handleRoleChange}
      onLogout={() => {
        setScreen("login");
        setRole("user");
      }}
    />
  );

  return (
    <div className="dl-shell">
      {topBar}
      <div className="dl-shell-body">
        {screen === "verify" ? (
          <VerifyWorkspace
            currentCase={currentCase}
            cases={caseRecords}
            agents={agents}
            selectedAgentId={agentByModule.verify}
            sessions={sessions}
            activeSessionId={activeSessionId}
            onSelectCase={handleSelectCase}
            onDeleteCases={handleDeleteCases}
            onSelectSession={handleSelectSession}
            onCreateSession={createSession}
          />
        ) : screen === "office" ? (
          <OfficeWorkspace
            selectedAgent={
              agents.find((agent) => agent.id === agentByModule.office) ??
              agents[0]
            }
            role={role}
          />
        ) : screen === "agents" ? (
          <AgentManagementPage
            role={role}
            agents={agentsForRole}
            onDuplicateAgent={handleDuplicateAgent}
            onDeleteAgents={handleDeleteAgents}
            onUseAgent={handleUseAgent}
            onOpenDetail={handleOpenAgentDetail}
          />
        ) : screen === "agentDetail" && detailAgent ? (
          <AgentDetailPage
            key={detailAgent.id}
            agent={detailAgent}
            role={role}
            onToggleAgent={handleToggleAgent}
            onUpdateAgent={handleUpdateAgent}
            onDuplicateAgent={handleDuplicateAgent}
            onUseAgent={handleUseAgent}
            onOpenDetail={handleOpenAgentDetail}
          />
        ) : (
          <NotificationPage
            role={role}
            notifications={visibleNotifications}
            onOpenVerify={() => navigate("verify")}
          />
        )}
      </div>
    </div>
  );
}
