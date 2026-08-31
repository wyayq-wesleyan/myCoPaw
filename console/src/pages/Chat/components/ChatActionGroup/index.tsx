import React from "react";

import { IconButton } from "@agentscope-ai/design";
import { SparkHistoryLine, SparkNewChatFill } from "@agentscope-ai/icons";
import { MoreOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { Dropdown, Flex, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { useCreateNewSession } from "../../hooks/useCreateNewSession";
import { useIsMobile } from "../../../../hooks/useIsMobile";

interface ChatActionGroupProps {
  onToggleHistory?: () => void;
  historyOpen?: boolean;
}

const ChatActionGroup: React.FC<ChatActionGroupProps> = ({
  onToggleHistory,
  historyOpen = false,
}) => {
  const { t } = useTranslation();
  const createNewSession = useCreateNewSession();
  const isCompact = useIsMobile();

  const moreItems: MenuProps["items"] = [];
  if (onToggleHistory) {
    moreItems.push({
      key: "history",
      icon: <SparkHistoryLine />,
      label: <div style={{ textAlign: "center" }}>{t("chat.chatHistoryTooltip")}</div>,
      onClick: () => onToggleHistory(),
    });
  }

  return (
    <Flex gap={8} align="center">
      <Tooltip title={t("chat.newChatTooltip")} mouseEnterDelay={0.5}>
        <IconButton bordered={false} icon={<SparkNewChatFill />} onClick={createNewSession} />
      </Tooltip>

      {!isCompact && onToggleHistory && (
        <Tooltip title={t("chat.chatHistoryTooltip")} mouseEnterDelay={0.5}>
          <IconButton
            bordered={false}
            icon={<SparkHistoryLine />}
            style={historyOpen ? { color: "var(--color-primary, #d2a55d)" } : undefined}
            onClick={onToggleHistory}
          />
        </Tooltip>
      )}

      {isCompact && moreItems.length > 0 && (
        <Dropdown menu={{ items: moreItems }} trigger={["click"]} placement="bottomRight">
          <IconButton bordered={false} icon={<MoreOutlined />} />
        </Dropdown>
      )}
    </Flex>
  );
};

export default ChatActionGroup;
