import { SparkExitFullscreenLine, SparkFullscreenLine } from "@agentscope-ai/icons";
import { useSidebarModeStore } from "../stores/sidebarModeStore";
import styles from "./sidebarSettingsPanel.module.less";

interface SidebarSettingsPanelProps {
  onClose?: () => void;
}

/** Offline console quick settings intentionally expose navigation only. */
export default function SidebarSettingsPanel({
  onClose,
}: SidebarSettingsPanelProps) {
  const { mode, toggleMode } = useSidebarModeStore();
  const isSimple = mode === "simple";

  return (
    <div className={styles.panel}>
      <div className={styles.row}>
        <span className={styles.label}>导航模式</span>
        <button
          className={`${styles.optBtn} ${styles.optBtnBlock}`}
          onClick={() => {
            toggleMode();
            onClose?.();
          }}
        >
          {isSimple ? (
            <SparkFullscreenLine size={14} />
          ) : (
            <SparkExitFullscreenLine size={14} />
          )}
          <span className={styles.optLabel}>
            {isSimple ? "显示完整菜单" : "仅显示常用菜单"}
          </span>
        </button>
      </div>
    </div>
  );
}
