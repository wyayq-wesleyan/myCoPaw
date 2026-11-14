import type { ReactNode } from "react";
import {
  AuditOutlined,
  BookOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";

import type { PageKey } from "./Sidebar";

interface ModuleCopy {
  title: string;
  subtitle: string;
  icon: ReactNode;
  features: Array<{ title: string; desc: string }>;
}

const moduleCopy: Partial<Record<PageKey, ModuleCopy>> = {
  files: {
    title: "文件工作区",
    subtitle: "按案件与用户隔离的文件空间，支持上传、预览和引用到对话。",
    icon: <FolderOpenOutlined />,
    features: [
      { title: "案件文件树", desc: "以案件/路径组织文件，只展示当前账号有权访问的目录。" },
      { title: "预览与引用", desc: "PDF、Word、Excel、图片在线预览，一键引用到当前会话。" },
      { title: "上传与审计", desc: "上传、下载、删除动作写入 ELC，敏感文件走审批。" },
    ],
  },
  knowledge: {
    title: "知识空间",
    subtitle: "沉淀业务材料与制度文档，供智能体检索、引用和溯源。",
    icon: <BookOutlined />,
    features: [
      { title: "文档与路径", desc: "按知识空间和案件路径组织文档，支持批量导入。" },
      { title: "解析与索引", desc: "MinerU 解析、向量索引、知识图谱，统一展示处理状态。" },
      { title: "引用与依据", desc: "回答底部列出参考文档，支持查看原文片段。" },
    ],
  },
  data: {
    title: "数据空间",
    subtitle: "把表格变成可查询、可关联、可审计的数据资产。",
    icon: <DatabaseOutlined />,
    features: [
      { title: "路径与数据表", desc: "按案件路径管理数据表，展示行数、列数和更新时间。" },
      { title: "导入与建表", desc: "Excel/CSV 自动建表、字段推断、增量导入。" },
      { title: "查询与审计", desc: "智能体用 SQL 只读查询；敏感字段导出触发审批。" },
    ],
  },
  approvals: {
    title: "审批中心",
    subtitle: "集中查看 TCMS 审批进度、待办与审批规则。",
    icon: <AuditOutlined />,
    features: [
      { title: "待审批", desc: "智能体发起的数据查询、导出、外发等事项统一排队。" },
      { title: "审批状态", desc: "待审批、已通过、已驳回、已超时，支持跳转平台审批页。" },
      { title: "规则说明", desc: "哪些动作需要审批、审批人如何确定，一次说清。" },
    ],
  },
  inbox: {
    title: "收件箱",
    subtitle: "接收技能分享审批、系统通知和智能体任务结果。",
    icon: <InboxOutlined />,
    features: [
      { title: "分享审批", desc: "他人分享的技能/工具，经确认后才进入自己的资源列表。" },
      { title: "任务通知", desc: "长任务完成、失败、审批结果统一通知。" },
      { title: "已读与归档", desc: "按类型筛选，支持批量已读和归档。" },
    ],
  },
  cron: {
    title: "定时任务",
    subtitle: "按案件或账号创建周期性研判、数据同步和报告任务。",
    icon: <ClockCircleOutlined />,
    features: [
      { title: "任务编排", desc: "选择智能体、技能、数据源和执行频率。" },
      { title: "执行记录", desc: "每次执行的结果、耗时、错误和产物可追溯。" },
      { title: "审批与通知", desc: "涉及敏感数据的定时任务执行前走平台审批。" },
    ],
  },
  skills: {
    title: "技能",
    subtitle: "管理智能体可用的业务技能，支持账号级授权与分享审批。",
    icon: <ThunderboltOutlined />,
    features: [
      { title: "技能目录", desc: "内置、账号自建、他人分享三类来源统一展示。" },
      { title: "授权范围", desc: "按账号/智能体授权，普通用户只能看到自己的技能。" },
      { title: "安全扫描", desc: "上传前扫描脚本与依赖，风险技能默认禁用。" },
    ],
  },
  tools: {
    title: "工具",
    subtitle: "查看智能体可调用的内置工具与外部接入工具。",
    icon: <ToolOutlined />,
    features: [
      { title: "工具清单", desc: "按风险等级和来源分组，标记管理员专属工具。" },
      { title: "权限快照", desc: "账号工具权限与平台角色同步，变更可追踪。" },
      { title: "调用审计", desc: "每次工具调用记录参数摘要、结果和耗时。" },
    ],
  },
  users: {
    title: "用户与权限",
    subtitle: "平台账号、角色、菜单与资源授权的统一管理入口。",
    icon: <TeamOutlined />,
    features: [
      { title: "账号同步", desc: "从 MSP 同步用户与组织，禁用/离职自动失效。" },
      { title: "角色与菜单", desc: "平台角色映射到汇智能力包，菜单由权限驱动。" },
      { title: "资源授权", desc: "模型、技能、工具按账号授权，支持批量调整。" },
    ],
  },
  audit: {
    title: "审计日志",
    subtitle: "登录、查询、导入、导出、审批和权限变更的完整留痕。",
    icon: <SafetyCertificateOutlined />,
    features: [
      { title: "操作检索", desc: "按账号、案件、时间、动作类型筛选。" },
      { title: "ELC 对账", desc: "本地审计与 ELC 写入状态对照，失败自动重试。" },
      { title: "导出与留存", desc: "按合规要求导出，敏感字段脱敏。" },
    ],
  },
};

export function PlaceholderPage({ page }: { page: PageKey }) {
  const copy = moduleCopy[page] ?? {
    title: "模块工作区",
    subtitle: "该模块的视觉与交互将在下一轮原型中细化。",
    icon: <FolderOpenOutlined />,
    features: [],
  };

  return (
    <div className="dl-placeholder">
      <div className="dl-placeholder-hero">
        <div className="dl-placeholder-icon">{copy.icon}</div>
        <div>
          <h1 className="dl-placeholder-title">{copy.title}</h1>
          <p className="dl-placeholder-desc">{copy.subtitle}</p>
        </div>
      </div>

      <div className="dl-placeholder-grid">
        {copy.features.map((feature) => (
          <div className="dl-placeholder-card" key={feature.title}>
            <div className="dl-placeholder-card-title">{feature.title}</div>
            <div className="dl-placeholder-card-desc">{feature.desc}</div>
          </div>
        ))}
      </div>

      <div
        style={{
          marginTop: 14,
          padding: "12px 16px",
          border: "1px dashed #d3c9b9",
          borderRadius: 8,
          color: "#9a9185",
          fontSize: 11,
          background: "rgba(255,253,249,0.55)",
        }}
      >
        这是第一版信息架构占位页，用于确认导航和模块边界；具体表格、筛选和操作会在下一轮逐页细化。
      </div>
    </div>
  );
}

export default PlaceholderPage;
