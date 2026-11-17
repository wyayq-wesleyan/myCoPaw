import { useEffect, useRef, useState } from "react";
import {
  AuditOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Modal, Tag, message } from "antd";

import type {
  AgentRecord,
  ApprovalRecord,
  CaseRecord,
  ChatMessage,
  SessionRecord,
  TargetRecord,
} from "../mock";
import {
  approvals as initialApprovals,
  initialMessages,
  targets as initialTargets,
} from "../mock";
import { CaseSidebar } from "./CaseSidebar";
import { ChatThread } from "./ChatThread";
import { Composer } from "./Composer";

interface VerifyWorkspaceProps {
  currentCase: CaseRecord;
  cases: CaseRecord[];
  agents: AgentRecord[];
  selectedAgentId: string;
  sessions: SessionRecord[];
  activeSessionId: string;
  onSelectCase: (caseRecord: CaseRecord) => void;
  onDeleteCases: (caseIds: string[]) => void;
  onSelectSession: (sessionId: string) => void;
  onCreateSession: (agentId: string, caseId: string) => string;
}

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

const historicalThreads: Record<string, ChatMessage[]> = {
  "sess-002": [
    {
      id: "s2-1",
      role: "user",
      author: "张三",
      time: "昨天 16:58",
      text: "把 A202600003 里的电诈线索先做一轮号码初筛。",
    },
    {
      id: "s2-2",
      role: "assistant",
      author: "信息核查·电诈线索专用",
      time: "昨天 17:05",
      text: "已完成 4 个目标的初筛，其中 2 个在控、2 个非在控。查询中心对本次查询批次返回需要审批，等待你选择终止本次查询或发起审批。",
    },
  ],
  "sess-003": [
    {
      id: "s3-1",
      role: "user",
      author: "张三",
      time: "11-14 15:12",
      text: "梳理一下跨境赌博案件里几个账户之间的关系。",
    },
    {
      id: "s3-2",
      role: "assistant",
      author: "信息核查·深度关联",
      time: "11-14 15:32",
      text: "案件已结案，关联查询仍按只读处理。已生成 3 层关系链，涉及 2 个非在控目标，未继续展开。",
    },
  ],
};

export function VerifyWorkspace({
  currentCase,
  cases,
  agents,
  selectedAgentId,
  sessions,
  activeSessionId,
  onSelectCase,
  onDeleteCases,
  onSelectSession,
  onCreateSession,
}: VerifyWorkspaceProps) {
  const [targets, setTargets] = useState<TargetRecord[]>(initialTargets);
  const [approvals, setApprovals] =
    useState<ApprovalRecord[]>(initialApprovals);
  const [threads, setThreads] = useState<Record<string, ChatMessage[]>>({
    "sess-001": initialMessages,
    ...historicalThreads,
  });
  const [thinking, setThinking] = useState(false);
  const [approvalTarget, setApprovalTarget] = useState<TargetRecord | null>(
    null,
  );
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [targetsLoading, setTargetsLoading] = useState(false);
  const [targetsRefreshedAt, setTargetsRefreshedAt] = useState("今天 10:48");

  useEffect(() => {
    if (currentCase.id === "CASE-2026-0001") {
      setTargets(initialTargets);
      setApprovals(initialApprovals);
      return;
    }
    const generated = Array.from({ length: currentCase.targets }).map(
      (_, index) => ({
        id: `${currentCase.id}-target-${index + 1}`,
        type: "手机号" as const,
        value: `待确认 ${index + 1}`,
        name: `在控目标 ${index + 1}`,
        status: "在控" as const,
        source: "案件在控目标",
        lastChecked: "—",
      }),
    );
    setTargets(generated);
    setApprovals([]);
    setTargetsRefreshedAt("—");
  }, [currentCase.id, currentCase.targets]);

  const refreshTargets = () => {
    setTargetsLoading(true);
    window.setTimeout(() => {
      setTargets((previous) =>
        previous.map((target) => ({ ...target, lastChecked: "刚刚" })),
      );
      setTargetsRefreshedAt("刚刚");
      setTargetsLoading(false);
    }, 420);
  };

  const selectedAgent =
    agents.find((agent) => agent.id === selectedAgentId) ?? agents[0];
  const messages = threads[activeSessionId] ?? [];
  const activeSession = sessions.find(
    (session) => session.id === activeSessionId,
  );

  const latestApproval = approvals[0];

  const processedApprovals = useRef<Set<string>>(new Set());

  useEffect(() => {
    const applyDecision = (data: { flowId?: string; decision?: string }) => {
      if (!data?.flowId || !data.decision) return;
      const key = `${data.flowId}:${data.decision}`;
      if (processedApprovals.current.has(key)) return;
      processedApprovals.current.add(key);

      const status: ApprovalRecord["status"] =
        data.decision === "approved" ? "已通过" : "已驳回";
      setApprovals((previous) =>
        previous.map((approval) =>
          approval.flowId === data.flowId ? { ...approval, status } : approval,
        ),
      );

      if (data.decision === "approved") {
        const resultCard = initialMessages.find((message) => message.resultCard)
          ?.resultCard;
        setThreads((previous) => ({
          ...previous,
          [activeSessionId]: [
            ...(previous[activeSessionId] ?? []),
            {
              id: `msg-${Date.now()}-approved`,
              role: "assistant",
              author: selectedAgent?.name ?? "信息核查智能体",
              time: nowTime(),
              text: "审批已通过。接入服务已回调并完成本批次查询，结果已入库；以下是查询结果。",
              resultCard,
            },
          ],
        }));
        message.success("审批通过，查询结果已入库");
      } else {
        message.info("审批已驳回，本次查询批次结束");
      }
    };

    const handleMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "huizhi-tcms-approval") return;
      applyDecision(event.data);
    };

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== "huizhi-tcms-approval" || !event.newValue) return;
      try {
        applyDecision(JSON.parse(event.newValue));
      } catch {
        // Ignore malformed demo payloads.
      }
    };

    window.addEventListener("message", handleMessage);
    window.addEventListener("storage", handleStorage);
    return () => {
      window.removeEventListener("message", handleMessage);
      window.removeEventListener("storage", handleStorage);
    };
  }, [activeSessionId, selectedAgent]);

  const updateThread = (sessionId: string, next: ChatMessage[]) => {
    setThreads((previous) => ({ ...previous, [sessionId]: next }));
  };

  const handleSend = (text: string) => {
    const current = threads[activeSessionId] ?? [];
    const userMessage: ChatMessage = {
      id: `msg-${Date.now()}`,
      role: "user",
      author: "张三",
      time: nowTime(),
      text,
    };
    updateThread(activeSessionId, [...current, userMessage]);
    setThinking(true);
    window.setTimeout(() => {
      setThreads((previous) => ({
        ...previous,
        [activeSessionId]: [
          ...(previous[activeSessionId] ?? []),
          {
            id: `msg-${Date.now()}-reply`,
            role: "assistant",
            author: selectedAgent?.name ?? "信息核查智能体",
            time: nowTime(),
            text:
              `收到。我会在案件 ${currentCase.code} 的权限范围内继续核查；` +
              "查询仍由 查询中心判定权限，涉及非在控目标时会先让你选择终止或发起审批。",
          },
        ],
      }));
      setThinking(false);
    }, 900);
  };

  const openTcmsApproval = (approval: ApprovalRecord) => {
    const url = `/approval.html?flow=${encodeURIComponent(
      approval.flowId,
    )}&batch=${encodeURIComponent(
      approval.batchId,
    )}&target=${encodeURIComponent(approval.targetLabel)}`;
    window.open(url, "_blank");
  };

  const handleOpenApproval = (approval: ApprovalRecord) => {
    if (approval.status === "审批中") {
      openTcmsApproval(approval);
    } else {
      message.info(`原型：查看审批记录 ${approval.flowId}`);
    }
  };

  const requestApproval = (targetId: string) => {
    const target = targets.find((item) => item.id === targetId);
    if (target) setApprovalTarget(target);
  };

  const confirmApproval = () => {
    if (!approvalTarget) return;
    setApprovalLoading(true);
    window.setTimeout(() => {
      const flowId = `审批-20261116-${String(
        Math.floor(40 + Math.random() * 50),
      ).padStart(4, "0")}`;
      const batchId = `批次-20261116-${String(
        Math.floor(50 + Math.random() * 50),
      ).padStart(4, "0")}`;
      setTargets((previous) =>
        previous.map((target) =>
          target.id === approvalTarget.id
            ? { ...target, status: "待审批" }
            : target,
        ),
      );
      setApprovals((previous) => [
        {
          id: `approval-${Date.now()}`,
          flowId,
          batchId,
          targetId: approvalTarget.id,
          targetLabel: `本批次 2 个目标（${approvalTarget.type} ${approvalTarget.value} 需审批）`,
          targetCount: 2,
          type: "查询批次审批",
          status: "审批中",
          submittedAt: nowTime(),
          approver: "支队负责人",
        },
        ...previous,
      ]);
      setApprovalLoading(false);
      setApprovalTarget(null);
      message.success(
        `已通过接入服务发起查询批次审批 ${flowId}，审批通过后本批次所有目标统一查询。`,
      );
    }, 700);
  };

  const terminateTarget = (targetId: string) => {
    const target = targets.find((item) => item.id === targetId);
    setTargets((previous) =>
      previous.map((item) =>
        item.id === targetId ? { ...item, status: "已终止" } : item,
      ),
    );
    message.info(
      `已终止本次查询批次${
        target ? `（含 ${target.type} ${target.value}）` : ""
      }，案件和其他对话不受影响。`,
    );
  };

  const handleNewSession = () => {
    if (!selectedAgent) return;
    const nextId = onCreateSession(selectedAgent.id, currentCase.id);
    setThreads((previous) => ({ ...previous, [nextId]: [] }));
    message.success("已新建会话");
  };

  return (
    <div className={`dl-verify ${leftCollapsed ? "dl-left-collapsed" : ""}`}>
      <CaseSidebar
        cases={cases}
        currentCase={currentCase}
        targets={targets}
        targetsLoading={targetsLoading}
        targetsRefreshedAt={targetsRefreshedAt}
        onRefreshTargets={refreshTargets}
        collapsed={leftCollapsed}
        onToggleCollapsed={() => setLeftCollapsed((value) => !value)}
        onSelectCase={onSelectCase}
        onDeleteCases={onDeleteCases}
      />

      <section className="dl-verify-main">
        <div className="dl-verify-head">
          <div className="dl-verify-head-copy">
            <div className="dl-verify-kicker">
              案件工作区 · {currentCase.code}
            </div>
            <div className="dl-verify-title-row">
              <h1>{currentCase.name}</h1>
              <Tag className="dl-verify-status">{currentCase.status}</Tag>
            </div>
            <p>
              {currentCase.org} · 主办人 {currentCase.owner} · 在控目标{" "}
              {currentCase.targets} 个
            </p>
            <div className="dl-agent-carry">
              <RobotOutlined /> 智能体知识以只读方式挂载（不复制）：知识{" "}
              {selectedAgent?.knowledgeCount ?? 0} · 技能{" "}
              {selectedAgent?.skillCount ?? 0} · 工具{" "}
              {selectedAgent?.toolCount ?? 0} · 查询客户端 2
            </div>
          </div>
        </div>

        <ChatThread
          messages={messages}
          targets={targets}
          approvals={approvals}
          agentName={selectedAgent?.name ?? "信息核查智能体"}
          userName="张三"
          thinking={thinking}
          onApprove={requestApproval}
          onTerminate={terminateTarget}
          onExport={() =>
            message.success("原型：将按固定结果模板生成文件并记录审计日志。")
          }
          approval={latestApproval}
          onOpenApproval={handleOpenApproval}
        />

        <Composer
          sessions={sessions}
          activeSessionId={activeSessionId}
          model={selectedAgent?.model ?? "千问 3.6-27B"}
          onSend={handleSend}
          onSessionChange={onSelectSession}
          onNewSession={handleNewSession}
          onAttach={() =>
            message.info("原型：附件将进入当前案件工作区，不进入知识空间。")
          }
        />

        <div className="dl-verify-footnote">
          <span>
            <SafetyCertificateOutlined /> 查询权限由 查询中心判定
          </span>
          <span>
            <AuditOutlined /> 审批由接入服务发起，审批系统负责流转
          </span>
          <span>当前会话：{activeSession?.title ?? "新会话"}</span>
        </div>
      </section>

      <Modal
        title="发起查询批次审批"
        open={Boolean(approvalTarget)}
        onCancel={() => setApprovalTarget(null)}
        onOk={confirmApproval}
        okText="确认发起并打开审批表单"
        cancelText="取消"
        confirmLoading={approvalLoading}
        width={540}
      >
        <div className="dl-approval-modal">
          <div className="dl-approval-modal-target">
            <span>本批次 2 个目标</span>
            <strong>
              需审批目标：{approvalTarget?.type} {approvalTarget?.value}
            </strong>
          </div>
          <div className="dl-approval-modal-copy">
            本批次中的身份证目标不在当前案件在控目标中，查询服务
            返回需要审批。小助手将调用 接入服务
            发起查询批次审批，审批通过后本批次所有目标统一查询。
          </div>
          <div className="dl-approval-modal-route">
            <span>小助手</span>
            <i />
            <span>接入服务</span>
            <i />
            <span>审批表单</span>
            <i />
            <span>上级审批</span>
            <i />
            <span>回调恢复查询</span>
          </div>
          <div className="dl-approval-modal-note">
            审批期间本批次暂不执行查询；终止操作只终止本次查询批次，不影响案件和其他对话。
          </div>
        </div>
      </Modal>
    </div>
  );
}

export default VerifyWorkspace;
