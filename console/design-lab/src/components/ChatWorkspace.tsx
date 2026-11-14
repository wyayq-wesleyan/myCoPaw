import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownOutlined,
  BookOutlined,
  CheckCircleFilled,
  DatabaseOutlined,
  FileExcelOutlined,
  FileTextOutlined,
  HistoryOutlined,
  PaperClipOutlined,
  PlusOutlined,
  RobotOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Button, Input, Tag, Tooltip } from "antd";

import type { CaseRecord, ChatMessage, ReferenceItem } from "../mock";
import { initialMessages } from "../mock";
import { ContextPanel } from "./ContextPanel";

interface ChatWorkspaceProps {
  currentCase: CaseRecord;
}

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function referenceIcon(kind: ReferenceItem["kind"]) {
  return kind === "data_table" ? <DatabaseOutlined /> : <FileTextOutlined />;
}

function ToolCallCard({
  tool,
}: {
  tool: NonNullable<ChatMessage["tool"]>;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="dl-tool-card">
      <div className="dl-tool-head">
        <div className="dl-tool-icon">
          <ThunderboltOutlined />
        </div>
        <div className="dl-tool-title">{tool.name}</div>
        <span className="dl-tool-status">
          <CheckCircleFilled /> 完成
        </span>
        <button
          className="dl-tool-toggle"
          type="button"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起步骤" : "查看步骤"}
        </button>
      </div>
      <div className="dl-tool-summary">{tool.summary}</div>
      {expanded ? (
        <div className="dl-tool-steps">
          {tool.steps.map((step) => (
            <div className="dl-tool-step" key={step}>
              {step}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ReferencesFooter({
  references,
}: {
  references: ReferenceItem[];
}) {
  const [expanded, setExpanded] = useState(false);
  if (references.length === 0) return null;

  const visible = expanded ? references : references.slice(0, 3);
  const hidden = references.length - 3;

  return (
    <div className="dl-references">
      <div className="dl-ref-divider" />
      <div className="dl-ref-title">参考资料</div>
      <ol className="dl-ref-list">
        {visible.map((reference, index) => (
          <li className="dl-ref-item" key={reference.id}>
            <span className="dl-ref-index">{index + 1}.</span>
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
          {expanded ? "收起" : `更多（${hidden}）`}
        </button>
      ) : null}
    </div>
  );
}

function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div
      className={`dl-message ${
        isUser ? "dl-message-user" : "dl-message-assistant"
      }`}
    >
      <div className="dl-message-avatar">
        {isUser ? "张" : <RobotOutlined />}
      </div>
      <div className="dl-message-body">
        <div className="dl-message-meta">
          <span className="dl-message-author">
            {isUser ? message.author : "研判助手"}
          </span>
          <span className="dl-message-time">{message.time}</span>
        </div>
        <div className={`dl-bubble ${isUser ? "dl-bubble-user" : ""}`}>
          {message.tool ? <ToolCallCard tool={message.tool} /> : null}
          {message.text ? <p className="dl-answer-text">{message.text}</p> : null}
          {message.table ? (
            <table className="dl-data-table">
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
          ) : null}
          {message.references ? (
            <ReferencesFooter references={message.references} />
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Composer({ onSend }: { onSend: (text: string) => void }) {
  const [value, setValue] = useState("");
  const suggestions = [
    "统计各分数段人数",
    "生成研判报告",
    "查看关联案件材料",
  ];

  const send = () => {
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue("");
  };

  return (
    <div className="dl-composer-wrap">
      <div className="dl-quick-actions">
        {suggestions.map((suggestion) => (
          <button
            className="dl-quick-chip"
            key={suggestion}
            type="button"
            onClick={() => setValue(suggestion)}
          >
            <PlusOutlined style={{ fontSize: 9, marginRight: 4 }} />
            {suggestion}
          </button>
        ))}
      </div>
      <div className="dl-composer">
        <div className="dl-composer-input">
          <Input.TextArea
            autoSize={{ minRows: 2, maxRows: 6 }}
            placeholder="输入研判问题，Enter 发送，Shift + Enter 换行"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
          />
        </div>
        <div className="dl-composer-bar">
          <div className="dl-composer-tools">
            <Tooltip title="添加附件">
              <button className="dl-icon-button" type="button">
                <PaperClipOutlined />
              </button>
            </Tooltip>
            <Tooltip title="引用案件材料">
              <button className="dl-icon-button" type="button">
                <BookOutlined />
              </button>
            </Tooltip>
          </div>
          <div className="dl-composer-right">
            <span className="dl-model-chip">Qwen3.6-27B</span>
            <span className="dl-model-chip">案件内</span>
            <button
              className="dl-send-button"
              type="button"
              onClick={send}
              disabled={!value.trim()}
              aria-label="发送"
            >
              <SendOutlined />
            </button>
          </div>
        </div>
      </div>
      <div className="dl-composer-hint">
        Enter 发送 · Shift + Enter 换行 · 分析范围限定在当前案件权限内
      </div>
    </div>
  );
}

export function ChatWorkspace({ currentCase }: ChatWorkspaceProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [thinking, setThinking] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const references = useMemo(
    () => messages.flatMap((message) => message.references ?? []),
    [messages],
  );

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [messages, thinking]);

  const handleSend = (text: string) => {
    const userMessage: ChatMessage = {
      id: `m-${Date.now()}`,
      role: "user",
      author: "张三",
      time: nowTime(),
      text,
    };
    setMessages((previous) => [...previous, userMessage]);
    setThinking(true);
    window.setTimeout(() => {
      setMessages((previous) => [
        ...previous,
        {
          id: `m-${Date.now()}-reply`,
          role: "assistant",
          author: "研判助手",
          time: nowTime(),
          text:
            `已收到。我会在当前案件 ${currentCase.code} 的权限范围内继续检索和分析；` +
            "如需导出原始数据或查询敏感字段，会先发起平台审批并同步审批状态。",
        },
      ]);
      setThinking(false);
    }, 900);
  };

  return (
    <div className="dl-chat">
      <section className="dl-chat-main">
        <div className="dl-chat-header">
          <div className="dl-chat-title-row">
            <h2 className="dl-chat-title">案件研判工作台</h2>
            <Tag
              style={{
                marginLeft: 2,
                background: "#f2ede3",
                borderColor: "#e2dbcf",
                color: "#6b604f",
                fontSize: 10,
              }}
            >
              案件内会话
            </Tag>
            <div className="dl-chat-actions">
              <Button size="small" icon={<HistoryOutlined />}>
                历史会话
              </Button>
              <Button size="small" type="primary" icon={<PlusOutlined />}>
                新会话
              </Button>
            </div>
          </div>
          <div className="dl-chat-sub">
            围绕 {currentCase.code} · {currentCase.name} 进行知识检索、数据查询与研判问答
          </div>
        </div>

        <div className="dl-chat-scroll" ref={scrollRef}>
          <div className="dl-thread">
            {messages.map((message) => (
              <MessageItem key={message.id} message={message} />
            ))}
            {thinking ? (
              <div className="dl-message dl-message-assistant">
                <div className="dl-message-avatar">
                  <RobotOutlined />
                </div>
                <div className="dl-message-body">
                  <div className="dl-message-meta">
                    <span className="dl-message-author">研判助手</span>
                    <span className="dl-message-time">正在分析…</span>
                  </div>
                  <div className="dl-bubble">
                    <span style={{ color: "#8b8277", fontSize: 11 }}>
                      正在检索案件材料与数据表…
                    </span>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        <Composer onSend={handleSend} />
      </section>

      <ContextPanel currentCase={currentCase} references={references} />
    </div>
  );
}

export default ChatWorkspace;
