import type { ThemeConfig } from "antd";

export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: "#143c69",
    colorInfo: "#143c69",
    colorSuccess: "#2f6f62",
    colorWarning: "#a8661d",
    colorError: "#b42318",
    colorTextBase: "#1e2a36",
    colorBgBase: "#f4f1ea",
    borderRadius: 8,
    controlHeight: 36,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Button: {
      primaryShadow: "none",
      fontWeight: 500,
    },
    Input: {
      activeShadow: "0 0 0 2px rgba(20, 60, 105, 0.10)",
    },
    Tabs: {
      inkBarColor: "#b08d57",
      itemSelectedColor: "#143c69",
      itemHoverColor: "#143c69",
      titleFontSize: 13,
    },
    Dropdown: {
      borderRadiusLG: 8,
    },
    Tooltip: {
      colorBgSpotlight: "#10263d",
    },
  },
};
