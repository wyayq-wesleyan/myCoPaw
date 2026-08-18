import { Button, Tooltip } from "@agentscope-ai/design";
import {
  CloseOutlined,
  DeleteOutlined,
  ReloadOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { AddSkillDropdown } from "./AddSkillDropdown";
import styles from "../index.module.less";

interface HeaderActionsProps {
  batchModeEnabled: boolean;
  selectedSkills: Set<string>;
  loading: boolean;
  uploading: boolean;
  fileInputRef: React.RefObject<HTMLInputElement>;
  onSelectAll: () => void;
  onClearSelection: () => void;
  onBatchEnable: () => void;
  onBatchDisable: () => void;
  onBatchDelete: () => void;
  onToggleBatchMode: () => void;
  onHardRefresh: () => void;
  onUploadClick: () => void;
  onCreate: () => void;
  onFileChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}

export function HeaderActions({
  batchModeEnabled,
  selectedSkills,
  loading,
  uploading,
  fileInputRef,
  onSelectAll,
  onClearSelection,
  onBatchEnable,
  onBatchDisable,
  onBatchDelete,
  onToggleBatchMode,
  onHardRefresh,
  onUploadClick,
  onCreate,
  onFileChange,
}: HeaderActionsProps) {
  const { t } = useTranslation();

  return (
    <div className={styles.headerRight}>
      <input
        type="file"
        accept=".zip"
        ref={fileInputRef}
        onChange={onFileChange}
        style={{ display: "none" }}
      />
      {batchModeEnabled ? (
        <div className={styles.batchActions}>
          <>
            <span className={styles.batchCount}>
              {t("skills.selectedCount", { count: selectedSkills.size })}
            </span>
            <Button type="default" onClick={onSelectAll}>
              {t("skills.selectAll")}
            </Button>
            <Button
              type="default"
              onClick={onClearSelection}
              icon={<CloseOutlined />}
            >
              {t("skills.clearSelection")}
            </Button>
            <Button
              type="default"
              icon={<EyeOutlined />}
              onClick={onBatchEnable}
            >
              {t("skills.batchEnable")}
            </Button>
            <Button
              danger
              icon={<EyeInvisibleOutlined />}
              onClick={onBatchDisable}
            >
              {t("skills.batchDisable")}
            </Button>
            <Button danger icon={<DeleteOutlined />} onClick={onBatchDelete}>
              {t("common.delete")} ({selectedSkills.size})
            </Button>
          </>
          <Button type="primary" onClick={onToggleBatchMode}>
            {t("skills.exitBatch")}
          </Button>
        </div>
      ) : (
        <>
          <div className={styles.headerActionsLeft}>
            <Tooltip title={t("skills.refreshHint")}>
              <Button
                type="default"
                icon={<ReloadOutlined spin={loading} />}
                onClick={onHardRefresh}
                disabled={loading}
              />
            </Tooltip>
          </div>
          <div className={styles.headerActionsRight}>
            <Button type="primary" onClick={onToggleBatchMode}>
              {t("skills.batchOperation")}
            </Button>
            <AddSkillDropdown
              onCreate={onCreate}
              onUploadZip={onUploadClick}
              uploading={uploading}
            />
          </div>
        </>
      )}
    </div>
  );
}
