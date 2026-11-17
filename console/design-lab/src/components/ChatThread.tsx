import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRightOutlined,
  AuditOutlined,
  BookOutlined,
  CheckCircleFilled,
  ClockCircleFilled,
  CloseOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  LinkOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  StopOutlined,
  ThunderboltOutlined,
  UserOutlined,
  VerticalAlignBottomOutlined,
  VerticalAlignTopOutlined,
} from "@ant-design/icons";
import { Button, Tag } from "antd";

import type {
  ApprovalRecord,
  ChatMessage,
  ReferenceItem,
  ResultCardData,
  TargetRecord,
} from "../mock";

interface ChatThreadProps {
  messages: ChatMessage[];
  targets: TargetRecord[];
  approvals: ApprovalRecord[];
  agentName: string;
  userName: string;
  thinking: boolean;
  onApprove: (targetId: string) => void;
  onTerminate: (targetId: string) => void;
  onExport: () => void;
  approval?: ApprovalRecord;
  onOpenApproval: (approval: ApprovalRecord) => void;
}

function referenceIcon(kind: ReferenceItem["kind"]) {
  if (kind === "data_table") return <DatabaseOutlined />;
  if (kind === "mcp") return <LinkOutlined />;
  return <FileTextOutlined />;
}

function ToolCallCard({ tool }: { tool: NonNullable<ChatMessage["tool"]> }) {
  const [expanded, setExpanded] = useState(false);
  const statusText =
    tool.status === "done"
      ? "完成"
      : tool.status === "waiting"
      ? "等待审批"
      : "执行中";
  return (
    <div className="dl-tool-card">
      <div className="dl-tool-head">
        <span className="dl-tool-icon">
          <ThunderboltOutlined />
        </span>
        <span className="dl-tool-title">{tool.name}</span>
        <span className={`dl-tool-status dl-tool-status-${tool.status}`}>
          {tool.status === "done" ? (
            <CheckCircleFilled />
          ) : (
            <ClockCircleFilled />
          )}
          {statusText}
        </span>
        <button
          className="dl-tool-toggle"
          type="button"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : "查看步骤"}
        </button>
      </div>
      <div className="dl-tool-summary">{tool.summary}</div>
      {expanded ? (
        <div className="dl-tool-steps">
          {tool.steps.map((step) => (
            <code className="dl-tool-step" key={step}>
              {step}
            </code>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ReferencesFooter({ references }: { references: ReferenceItem[] }) {
  const [expanded, setExpanded] = useState(false);
  if (references.length === 0) return null;
  const visible = expanded ? references : references.slice(0, 3);
  const hidden = references.length - 3;

  return (
    <div className="dl-references">
      <div className="dl-ref-divider" />
      <div className="dl-ref-title">
        <BookOutlined /> 参考依据
      </div>
      <ol className="dl-ref-list">
        {visible.map((reference, index) => (
          <li className="dl-ref-item" key={reference.id}>
            <span className="dl-ref-index">{index + 1}</span>
            <span className="dl-ref-icon">{referenceIcon(reference.kind)}</span>
            <span className="dl-ref-copy">
              <span className="dl-ref-name">{reference.name}</span>
              {reference.meta ? (
                <span className="dl-ref-meta">{reference.meta}</span>
              ) : null}
            </span>
          </li>
        ))}
      </ol>
      {hidden > 0 ? (
        <button
          className="dl-ref-toggle"
          type="button"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : `更多 ${hidden} 条`}
        </button>
      ) : null}
    </div>
  );
}

function ResultCard({
  data,
  onExport,
}: {
  data: ResultCardData;
  onExport: () => void;
}) {
  return (
    <div className="dl-result-card">
      <div className="dl-result-head">
        <div>
          <div className="dl-result-eyebrow">{data.eyebrow}</div>
          <div className="dl-result-title-row">
            <h3>{data.title}</h3>
            <Tag className="dl-result-tag">{data.status}</Tag>
          </div>
        </div>
        <div className="dl-result-target">
          <SafetyCertificateOutlined />
          {data.target}
        </div>
      </div>

      <div className="dl-result-fields">
        {data.fields.map((field) => (
          <div className="dl-result-field" key={field.label}>
            <span>{field.label}</span>
            <strong>{field.value}</strong>
          </div>
        ))}
      </div>

      <div className="dl-result-summary">{data.summary}</div>

      <div className="dl-result-table-wrap">
        <table className="dl-result-table">
          <thead>
            <tr>
              {data.columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <tr key={row.join("-")}>
                {row.map((cell, index) => (
                  <td key={`${cell}-${index}`}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="dl-result-foot">
        <span className="dl-result-template">
          固定结果模板 · 后续可替换为业务模板
        </span>
        <div className="dl-result-actions">
          <button type="button" className="dl-text-button">
            查看原始返回 <ArrowRightOutlined />
          </button>
          <Button size="small" icon={<DownloadOutlined />} onClick={onExport}>
            导出报告
          </Button>
        </div>
      </div>
    </div>
  );
}

function NoPermissionCard({
  data,
  target,
  approvals,
  onApprove,
  onTerminate,
}: {
  data: NonNullable<ChatMessage["noPermission"]>;
  target?: TargetRecord;
  approvals: ApprovalRecord[];
  onApprove: (targetId: string) => void;
  onTerminate: (targetId: string) => void;
}) {
  const status = target?.status ?? "非在控";
  const approval = approvals.find((item) => item.targetId === data.targetId);
  const approved = approval?.status === "已通过";
  const rejected = approval?.status === "已驳回";
  const decided =
    status === "待审批" || status === "已终止" || approved || rejected;

  return (
    <div className={`dl-denied-card dl-denied-${status}`}>
      <div className="dl-denied-head">
        <span className="dl-denied-icon">
          <ExclamationCircleOutlined />
        </span>
        <div>
          <div className="dl-denied-kicker">查询中心 · 无权限返回</div>
          <div className="dl-denied-title">{data.reason}</div>
        </div>
        <span className="dl-denied-target">{data.targetLabel}</span>
      </div>
      <div className="dl-denied-detail">{data.detail}</div>
      <div className="dl-denied-source">
        <LinkOutlined /> {data.source}
      </div>

      {approved ? (
        <div className="dl-denied-result dl-denied-result-approved">
          <CheckCircleFilled />
          <div>
            <strong>审批通过，本批次查询已执行</strong>
            <span>
              {approval.flowId} · 查询结果已由接入服务入库并返回小助手。
            </span>
          </div>
        </div>
      ) : rejected ? (
        <div className="dl-denied-result dl-denied-result-stopped">
          <StopOutlined />
          <div>
            <strong>审批已驳回，本次查询批次结束</strong>
            <span>{approval?.flowId} · 可调整目标后重新发起查询。</span>
          </div>
        </div>
      ) : status === "待审批" ? (
        <div className="dl-denied-result dl-denied-result-waiting">
          <ClockCircleFilled />
          <div>
            <strong>查询批次审批已提交，等待上级审批</strong>
            <span>
              {approval
                ? `${approval.flowId} · ${approval.batchId} · 当前节点：${approval.approver}`
                : "审批流程已发起，通过后本批次所有目标将统一查询。"}
            </span>
          </div>
        </div>
      ) : status === "已终止" ? (
        <div className="dl-denied-result dl-denied-result-stopped">
          <StopOutlined />
          <div>
            <strong>本次查询批次已终止</strong>
            <span>本批次不再执行查询；案件和其他对话不受影响。</span>
          </div>
        </div>
      ) : (
        <>
          <div className="dl-denied-actions">
            <Button
              danger
              icon={<CloseOutlined />}
              onClick={() => onTerminate(data.targetId)}
            >
              终止本次查询
            </Button>
            <Button
              type="primary"
              icon={<SafetyCertificateOutlined />}
              onClick={() => onApprove(data.targetId)}
            >
              发起审批
            </Button>
          </div>
          <div className="dl-denied-hint">
            查询中心按批次判定权限；审批通过后，本批次所有目标统一执行查询。
          </div>
        </>
      )}

      {decided ? (
        <div className="dl-denied-scope">
          {approved
            ? "本批次目标已统一查询，结果已展示在会话中。"
            : status === "已终止" || rejected
            ? "本次查询批次已结束，不影响案件和其他对话。"
            : "审批通过后恢复本批次所有目标的查询。"}
        </div>
      ) : null}
    </div>
  );
}

export function MessageItem({
  message,
  agentName,
  userName,
  targets,
  approvals,
  onApprove,
  onTerminate,
  onExport,
}: {
  message: ChatMessage;
  agentName: string;
  userName: string;
  targets: TargetRecord[];
  approvals: ApprovalRecord[];
  onApprove: (targetId: string) => void;
  onTerminate: (targetId: string) => void;
  onExport: () => void;
}) {
  const isUser = message.role === "user";
  const target = message.noPermission
    ? targets.find((item) => item.id === message.noPermission?.targetId)
    : undefined;

  return (
    <article
      className={`dl-message ${
        isUser ? "dl-message-user" : "dl-message-assistant"
      }`}
      data-message-id={message.id}
    >
      <div className="dl-message-avatar">
        {isUser ? <UserOutlined /> : <RobotOutlined />}
      </div>
      <div className="dl-message-body">
        <div className="dl-message-meta">
          <span className="dl-message-author">
            {message.author || (isUser ? userName : agentName)}
          </span>
          <span className="dl-message-time">{message.time}</span>
        </div>
        <div className={`dl-bubble ${isUser ? "dl-bubble-user" : ""}`}>
          {message.tool ? <ToolCallCard tool={message.tool} /> : null}
          {message.resultCard ? (
            <ResultCard data={message.resultCard} onExport={onExport} />
          ) : null}
          {message.noPermission ? (
            <NoPermissionCard
              data={message.noPermission}
              target={target}
              approvals={approvals}
              onApprove={onApprove}
              onTerminate={onTerminate}
            />
          ) : null}
          {message.text ? (
            <p className="dl-answer-text">{message.text}</p>
          ) : null}
          {message.table ? (
            <div className="dl-result-table-wrap dl-chat-table">
              <table className="dl-result-table">
                <thead>
                  <tr>
                    {message.table.columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {message.table.rows.map((row) => (
                    <tr key={row.join("-")}>
                      {row.map((cell, index) => (
                        <td key={`${cell}-${index}`}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {message.references ? (
            <ReferencesFooter references={message.references} />
          ) : null}
        </div>
      </div>
    </article>
  );
}

interface ChatConversationProps {
  messages: ChatMessage[];
  agentName: string;
  thinking?: boolean;
  thinkingText?: string;
  empty?: ReactNode;
  children?: ReactNode;
}

export function ChatConversation({
  messages,
  agentName,
  thinking,
  thinkingText = "正在分析…",
  empty,
  children,
}: ChatConversationProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atTop, setAtTop] = useState(true);
  const [atBottom, setAtBottom] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);

  const updateScrollState = () => {
    const node = scrollRef.current;
    if (!node) return;
    setAtTop(node.scrollTop <= 4);
    setAtBottom(node.scrollHeight - node.scrollTop - node.clientHeight <= 4);
  };

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    updateScrollState();
  }, [messages, thinking, children]);

  const scrollToTop = () => {
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  };

  const scrollToBottom = () => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  };

  const jumpToMessage = (messageId: string) => {
    const container = scrollRef.current;
    if (!container) return;
    const node = container.querySelector(
      `[data-message-id="${messageId}"]`,
    ) as HTMLElement | null;
    if (!node) return;
    const top =
      node.getBoundingClientRect().top -
      container.getBoundingClientRect().top +
      container.scrollTop -
      12;
    container.scrollTo({ top, behavior: "smooth" });
    setHistoryOpen(false);
  };

  const messageSummary = (message: ChatMessage) =>
    message.text ??
    message.resultCard?.title ??
    message.noPermission?.reason ??
    message.tool?.summary ??
    "消息";

  return (
    <div className="dl-chat-scroll-wrap">
      <div
        className="dl-chat-scroll"
        ref={scrollRef}
        onScroll={updateScrollState}
      >
        <div className="dl-thread">
          {empty && messages.length === 0 ? empty : null}
          {children}

          {thinking ? (
            <article className="dl-message dl-message-assistant">
              <div className="dl-message-avatar">
                <RobotOutlined />
              </div>
              <div className="dl-message-body">
                <div className="dl-message-meta">
                  <span className="dl-message-author">{agentName}</span>
                  <span className="dl-message-time">正在分析</span>
                </div>
                <div className="dl-bubble dl-bubble-thinking">
                  <span className="dl-thinking-dot" />
                  <span className="dl-thinking-dot" />
                  <span className="dl-thinking-dot" />
                  <span>{thinkingText}</span>
                </div>
              </div>
            </article>
          ) : null}
        </div>
      </div>
      <div className="dl-chat-jump">
        {historyOpen ? (
          <div className="dl-chat-history-panel">
            <div className="dl-chat-history-head">
              <span>聊天记录</span>
              <span>{messages.length} 条</span>
            </div>
            <div className="dl-chat-history-list">
              {messages.length === 0 ? (
                <div className="dl-chat-history-empty">暂无消息</div>
              ) : (
                messages.map((message, index) => (
                  <button
                    key={message.id}
                    type="button"
                    className="dl-chat-history-item"
                    onClick={() => jumpToMessage(message.id)}
                  >
                    <span className="dl-chat-history-index">{index + 1}</span>
                    <span className="dl-chat-history-copy">
                      <span className="dl-chat-history-meta">
                        <strong>
                          {message.role === "user" ? "我" : message.author}
                        </strong>
                        <span>{message.time}</span>
                      </span>
                      <span className="dl-chat-history-text">
                        {messageSummary(message)}
                      </span>
                    </span>
                  </button>
                ))
              )}
            </div>
          </div>
        ) : null}
        <button
          type="button"
          className="dl-chat-jump-button"
          title="回到顶部"
          aria-label="回到顶部"
          disabled={atTop}
          onClick={scrollToTop}
        >
          <VerticalAlignTopOutlined />
        </button>
        <button
          type="button"
          className={`dl-chat-jump-button dl-chat-jump-count ${
            historyOpen ? "dl-chat-jump-count-active" : ""
          }`}
          title="聊天记录"
          aria-label="聊天记录"
          onClick={() => setHistoryOpen((value) => !value)}
        >
          {messages.length}
        </button>
        <button
          type="button"
          className="dl-chat-jump-button"
          title="回到底部"
          aria-label="回到底部"
          disabled={atBottom}
          onClick={scrollToBottom}
        >
          <VerticalAlignBottomOutlined />
        </button>
      </div>
    </div>
  );
}

export function ChatThread({
  messages,
  targets,
  approvals,
  agentName,
  userName,
  thinking,
  onApprove,
  onTerminate,
  onExport,
  approval,
  onOpenApproval,
}: ChatThreadProps) {
  return (
    <ChatConversation
      messages={messages}
      agentName={agentName}
      thinking={thinking}
      thinkingText="正在通过查询中心校验目标权限…"
    >
      {messages.map((message) => (
        <MessageItem
          key={message.id}
          message={message}
          agentName={agentName}
          userName={userName}
          targets={targets}
          approvals={approvals}
          onApprove={onApprove}
          onTerminate={onTerminate}
          onExport={onExport}
        />
      ))}

      {approval ? (
        <div className="dl-thread-approval">
          <span
            className={`dl-approval-request-icon dl-approval-request-icon-${approval.status}`}
          >
            <AuditOutlined />
          </span>
          <div className="dl-approval-request-main">
            <div className="dl-approval-request-title">
              查询审批请求 {approval.flowId}
              <span
                className={`dl-approval-request-status dl-approval-request-status-${approval.status}`}
              >
                {approval.status}
              </span>
            </div>
            <div className="dl-approval-request-meta">
              {approval.batchId} · {approval.targetLabel} · {approval.type} ·
              发起于 {approval.submittedAt} · 当前节点 {approval.approver}
            </div>
          </div>
          <button type="button" onClick={() => onOpenApproval(approval)}>
            {approval.status === "审批中" ? "打开审批表单" : "查看审批记录"}{" "}
            <LinkOutlined />
          </button>
        </div>
      ) : null}
    </ChatConversation>
  );
}

export default ChatThread;
