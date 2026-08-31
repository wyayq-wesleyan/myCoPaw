import { Tag } from "antd";
import {
  Package,
  Wrench,
  BrainCircuit,
  Zap,
  Terminal,
  LayoutDashboard,
  AppWindow,
} from "lucide-react";
import { SparkWifiLine } from "@agentscope-ai/icons";
import type { PluginType } from "@/api/modules/plugin";

const PLUGIN_TYPE_CONFIG: Record<
  PluginType,
  { label: string; color: string; icon: React.ReactNode }
> = {
  tool: {
    label: "工具",
    color: "blue",
    icon: <Wrench size={11} />,
  },
  provider: {
    label: "模型服务",
    color: "purple",
    icon: <BrainCircuit size={11} />,
  },
  hook: {
    label: "钩子",
    color: "orange",
    icon: <Zap size={11} />,
  },
  command: {
    label: "命令",
    color: "cyan",
    icon: <Terminal size={11} />,
  },
  frontend: {
    label: "前端",
    color: "green",
    icon: <LayoutDashboard size={11} />,
  },
  app: {
    label: "应用",
    color: "geekblue",
    icon: <AppWindow size={11} />,
  },
  channel: {
    label: "渠道",
    color: "default",
    icon: <SparkWifiLine size={11} />,
  },
  general: {
    label: "通用",
    color: "default",
    icon: <Package size={11} />,
  },
};

export function PluginTypeTag({ type }: { type: PluginType }) {
  const cfg = PLUGIN_TYPE_CONFIG[type] ?? PLUGIN_TYPE_CONFIG.general;
  return (
    <Tag
      color={cfg.color}
      icon={cfg.icon}
      style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
    >
      {cfg.label}
    </Tag>
  );
}
