import { useState } from "react";
import {
  DownOutlined,
  HistoryOutlined,
  PaperClipOutlined,
  PlusOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Dropdown, Input, Tooltip } from "antd";

import type { SessionRecord } from "../mock";

interface ComposerProps {
  sessions: SessionRecord[];
  activeSessionId: string;
  model: string;
  onSend: (text: string) => void;
  onSessionChange: (sessionId: string) => void;
  onNewSession: () => void;
  onAttach: () => void;
  suggestions?: string[];
  placeholder?: string;
  hintRight?: string;
}

export function Composer({
  sessions,
  activeSessionId,
  model,
  onSend,
  onSessionChange,
  onNewSession,
  onAttach,
  suggestions = [
    "核查这个号码的实名信息",
    "整理当前在控目标的核查结论",
    "查看这个目标的关联号码",
  ],
  placeholder = "描述核查目标或追问，Enter 发送，Shift + Enter 换行",
  hintRight = "会话可在输入框右侧切换",
}: ComposerProps) {
  const [value, setValue] = useState("");
  const active = sessions.find((session) => session.id === activeSessionId);

  const sessionItems = sessions.map((session) => ({
    key: session.id,
    label: (
      <div className="dl-session-menu-item">
        <div className="dl-session-menu-top">
          <span className="dl-session-menu-title">{session.title}</span>
          {session.id === activeSessionId ? (
            <span className="dl-session-menu-current">当前</span>
          ) : null}
        </div>
        <div className="dl-session-menu-meta">{session.updatedAt}</div>
      </div>
    ),
  }));

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
            <PlusOutlined />
            {suggestion}
          </button>
        ))}
      </div>

      <div className="dl-composer">
        <Input.TextArea
          className="dl-composer-textarea"
          autoSize={{ minRows: 2, maxRows: 6 }}
          placeholder={placeholder}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onPressEnter={(event) => {
            if (!event.shiftKey) {
              event.preventDefault();
              send();
            }
          }}
        />
        <div className="dl-composer-bar">
          <div className="dl-composer-tools">
            <Tooltip title="新建会话">
              <button
                className="dl-icon-button"
                type="button"
                onClick={onNewSession}
              >
                <PlusOutlined />
              </button>
            </Tooltip>
            <Tooltip title="添加附件">
              <button
                className="dl-icon-button"
                type="button"
                onClick={onAttach}
              >
                <PaperClipOutlined />
              </button>
            </Tooltip>
          </div>

          <div className="dl-composer-right">
            <Dropdown
              menu={{
                items: sessionItems,
                onClick: ({ key }) => onSessionChange(key),
              }}
              trigger={["click"]}
              placement="topRight"
            >
              <button className="dl-session-chip" type="button">
                <HistoryOutlined />
                <span className="dl-session-chip-copy">
                  <span className="dl-session-chip-label">会话</span>
                  <span className="dl-session-chip-name">
                    {active?.title ?? "新会话"}
                  </span>
                </span>
                <DownOutlined />
              </button>
            </Dropdown>
            <span className="dl-context-chip">
              <span className="dl-context-ring" />
              上下文 18%
            </span>
            <span className="dl-model-chip">
              <ThunderboltOutlined />
              {model}
            </span>
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
        <span>Enter 发送 · Shift + Enter 换行</span>
        <span>{hintRight}</span>
      </div>
    </div>
  );
}

export default Composer;
