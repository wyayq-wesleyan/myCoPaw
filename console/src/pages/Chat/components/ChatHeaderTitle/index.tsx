import React, { useEffect, useRef, useState } from "react";
import { Dropdown } from "antd";
import { DownOutlined } from "@ant-design/icons";
import { useChatAnywhereSessionsState } from "@agentscope-ai/chat";
import { useTranslation } from "react-i18next";
import { useCodingMode } from "../../../../stores/codingModeStore";
import styles from "./index.module.less";

const MOBILE_BREAKPOINT_PX = 480;

const ChatHeaderTitle: React.FC = () => {
  const { t } = useTranslation();
  const { sessions, currentSessionId, setCurrentSessionId } =
    useChatAnywhereSessionsState();
  const { codingMode } = useCodingMode();
  const currentSession = sessions.find((s) => s.id === currentSessionId);
  const chatName = currentSession?.name || "新对话";

  const [open, setOpen] = useState(false);

  // Detect mobile + whether title overflows. On mobile + overflow, render as
  // a horizontal marquee; otherwise keep the original ellipsis behavior.
  const containerRef = useRef<HTMLSpanElement | null>(null);
  const measureRef = useRef<HTMLSpanElement | null>(null);
  const [shouldMarquee, setShouldMarquee] = useState(false);

  useEffect(() => {
    const check = () => {
      const w =
        typeof window !== "undefined" ? window.innerWidth : Number.MAX_VALUE;
      const isMobile = w <= MOBILE_BREAKPOINT_PX;
      if (!isMobile) {
        setShouldMarquee(false);
        return;
      }
      const containerWidth =
        containerRef.current?.getBoundingClientRect().width ?? 0;
      const textWidth = measureRef.current?.getBoundingClientRect().width ?? 0;
      // Add a few px tolerance to avoid borderline jitter.
      setShouldMarquee(textWidth > containerWidth + 2);
    };

    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, [chatName, codingMode]);

  const handleSessionClick = (sessionId: string) => {
    setCurrentSessionId(sessionId);
    setOpen(false);
  };

  const activeRowRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!open) return;
    // With many sessions the active row can be below the fold; bring it into
    // view without scrolling the whole page.
    activeRowRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [open]);

  const className = codingMode
    ? `${styles.chatName} ${styles.chatNameCoding}`
    : styles.chatName;

  const titleContent = (
    <span className={className} ref={containerRef}>
      {shouldMarquee ? (
        <span className={styles.marquee}>{chatName}</span>
      ) : (
        chatName
      )}
    </span>
  );

  // Hidden span used to measure intrinsic text width for the marquee decision.
  // Placed outside .chatName so it does not duplicate text for screen readers
  // or testing-library queries.
  const measureSpan = (
    <span
      ref={measureRef}
      aria-hidden="true"
      style={{
        position: "absolute",
        visibility: "hidden",
        whiteSpace: "nowrap",
        pointerEvents: "none",
      }}
    >
      {chatName}
    </span>
  );

  const sessionCount = (
    <span className={styles.sessionCount} data-testid="chat-session-count">
      {sessions.length}
    </span>
  );
  const trigger = (
    <span
      className={styles.trigger}
      title={`${t("nav.sessions")} · ${sessions.length}`}
    >
      {titleContent}
      {sessionCount}
      <DownOutlined className={styles.sessionChevron} />
      {measureSpan}
    </span>
  );

  if (sessions.length === 0) {
    return trigger;
  }

  return (
    <Dropdown
      open={open}
      onOpenChange={setOpen}
      trigger={["click"]}
      placement="bottomLeft"
      popupRender={() =>
        open ? (
          <div className={styles.sessionPanel} data-testid="chat-session-panel">
            <div className={styles.sessionPanelHeader}>
              <span>{t("nav.sessions")}</span>
              <span className={styles.sessionPanelCount}>
                {sessions.length}
              </span>
            </div>
            <div className={styles.sessionPanelList} role="listbox">
              {sessions.map((session) => {
                const active = session.id === currentSessionId;
                return (
                  <button
                    key={session.id}
                    ref={active ? activeRowRef : undefined}
                    type="button"
                    role="option"
                    aria-selected={active}
                    className={`${styles.sessionRow} ${
                      active ? styles.sessionRowActive : ""
                    }`}
                    onClick={() => handleSessionClick(session.id)}
                  >
                    <span className={styles.sessionRowName}>
                      {session.name || "新对话"}
                    </span>
                    {active && (
                      <span className={styles.sessionRowCheck}>✓</span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null
      }
    >
      {trigger}
    </Dropdown>
  );
};

export default ChatHeaderTitle;
