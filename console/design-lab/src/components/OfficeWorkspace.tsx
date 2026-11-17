import { useRef, useState } from "react";
import { RobotOutlined } from "@ant-design/icons";
import { Tag, message } from "antd";

import type { AgentRecord, ChatMessage, Role, SessionRecord } from "../mock";
import { CaseFileTree } from "./CaseFileTree";
import { ChatConversation, MessageItem } from "./ChatThread";
import { Composer } from "./Composer";

interface OfficeWorkspaceProps {
  selectedAgent: AgentRecord;
  role: Role;
}

const initialMessages: ChatMessage[] = [
  {
    id: "o-1",
    role: "user",
    author: "张三",
    text: "把这份会议纪要整理成督办清单，按责任人和完成时限分组。",
    time: "09:24",
  },
  {
    id: "o-2",
    role: "assistant",
    author: "办公助手·通用",
    text: "已按“责任人 + 完成时限”整理为 4 项督办事项。原材料中的第 3 项没有明确责任人，我先标记为“待明确”，建议发出前补全。",
    table: {
      columns: ["序号", "督办事项", "责任人", "完成时限", "状态"],
      rows: [
        ["1", "补齐平台对接接口清单", "李四", "11-18", "进行中"],
        ["2", "完成信息核查流程评审", "王五", "11-20", "待开始"],
        ["3", "确认档案经营数据口径", "待明确", "11-22", "待明确"],
        ["4", "输出本周运行简报", "张三", "11-17", "进行中"],
      ],
    },
    time: "09:26",
  },
];

function officeSession(
  id: string,
  title: string,
  agentId: string,
  updatedAt: string,
  messageCount: number,
): SessionRecord {
  return {
    id,
    title,
    agentId,
    caseId: "OFFICE",
    updatedAt,
    messageCount,
  };
}

export function OfficeWorkspace({ selectedAgent, role }: OfficeWorkspaceProps) {
  const userName = role === "admin" ? "系统管理员" : "张三";
  const [sessions, setSessions] = useState<SessionRecord[]>([
    officeSession(
      "office-1",
      "会议纪要督办清单",
      selectedAgent.id,
      "今天 09:26",
      4,
    ),
    officeSession(
      "office-2",
      "平台接入情况汇报",
      selectedAgent.id,
      "昨天 17:40",
      8,
    ),
    officeSession("office-3", "本周运行简报", selectedAgent.id, "11-14", 6),
  ]);
  const [activeSessionId, setActiveSessionId] = useState("office-1");
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [thinking, setThinking] = useState(false);
  const replyTimer = useRef<number | null>(null);

  const switchSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setMessages(sessionId === "office-1" ? initialMessages : []);
  };

  const newSession = () => {
    const id = `office-${Date.now()}`;
    const next = officeSession(id, "新办公会话", selectedAgent.id, "刚刚", 0);
    setSessions((previous) => [next, ...previous]);
    setActiveSessionId(id);
    setMessages([]);
    message.success("已新建办公会话");
  };

  const send = (text: string) => {
    setMessages((previous) => [
      ...previous,
      {
        id: `o-${Date.now()}`,
        role: "user",
        author: userName,
        text,
        time: new Date().toLocaleTimeString("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
        }),
      },
    ]);
    setThinking(true);
    if (replyTimer.current) window.clearTimeout(replyTimer.current);
    replyTimer.current = window.setTimeout(() => {
      setMessages((previous) => [
        ...previous,
        {
          id: `o-${Date.now()}-reply`,
          role: "assistant",
          author: selectedAgent.name,
          text: "已按办公场景处理。这里不访问案件数据；如果材料涉及案件，请进入信息核查并选择案件后再处理。",
          time: new Date().toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
          }),
        },
      ]);
      setThinking(false);
    }, 700);
  };

  return (
    <div className="dl-office">
      <aside className="dl-office-sidebar">
        <div className="dl-office-space">
          <div className="dl-office-space-head">
            <div>
              <div className="dl-office-space-title">办公空间</div>
              <div className="dl-office-space-sub">
                当前用户专属 · 与案件隔离
              </div>
            </div>
            <span className="dl-office-space-mark">办</span>
          </div>
          <div className="dl-office-space-stats">
            <div>
              <strong>3</strong>
              <span>资料</span>
            </div>
            <div>
              <strong>2</strong>
              <span>临时</span>
            </div>
            <div>
              <strong>2</strong>
              <span>结果</span>
            </div>
          </div>
        </div>

        <div className="dl-office-files-body">
          <CaseFileTree
            caseCode="办公空间"
            rootName="办公空间"
            materialName="办公资料"
            spaceLabel="当前办公空间"
            mode="office"
          />
        </div>

        <div className="dl-office-side-note">
          智能体生成的临时文件在 tmp，结果文件在 res；上传文件仅当前用户可见。
        </div>
      </aside>

      <section className="dl-office-main">
        <div className="dl-office-head">
          <div>
            <h1>办公助手</h1>
            <p>不依赖案件的材料处理与日常问答，管理员与普通用户均可使用。</p>
          </div>
          <div className="dl-office-head-right">
            {role === "admin" ? (
              <Tag className="dl-office-admin-tag">管理员 · 非案件业务</Tag>
            ) : null}
            <span className="dl-verify-agent">
              <RobotOutlined /> {selectedAgent.name}
            </span>
            <Tag>非案件业务</Tag>
          </div>
        </div>

        <ChatConversation
          messages={messages}
          agentName={selectedAgent.name}
          thinking={thinking}
          thinkingText="正在整理材料…"
          empty={
            <div className="dl-office-empty">
              <RobotOutlined />
              <strong>开始一个新的办公任务</strong>
              <span>可以整理材料、起草公文、总结会议纪要或处理表格。</span>
            </div>
          }
        >
          {messages.map((item) => (
            <MessageItem
              key={item.id}
              message={item}
              agentName={selectedAgent.name}
              userName={userName}
              targets={[]}
              approvals={[]}
              onApprove={() => undefined}
              onTerminate={() => undefined}
              onExport={() => message.info("原型：导出办公结果")}
            />
          ))}
        </ChatConversation>

        <Composer
          sessions={sessions}
          activeSessionId={activeSessionId}
          model={selectedAgent.model}
          suggestions={["起草一份通知", "总结这份材料", "整理成表格"]}
          placeholder="描述你要处理的办公任务，Enter 发送"
          hintRight="办公助手不访问案件数据"
          onSend={send}
          onSessionChange={switchSession}
          onNewSession={newSession}
          onAttach={() => message.info("原型：上传办公材料")}
        />
      </section>
    </div>
  );
}

export default OfficeWorkspace;
