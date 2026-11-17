export type Role = "admin" | "user";

export type ModuleKey = "office" | "verify" | "archive" | "monitor";

export type Screen =
  | "login"
  | "home"
  | ModuleKey
  | "agents"
  | "agentDetail"
  | "notifications";

export type AgentScope = "system" | "custom";

export type AgentModule = "verify" | "office";

export interface AgentRecord {
  id: string;
  name: string;
  scope: AgentScope;
  module: AgentModule;
  description: string;
  enabled: boolean;
  model: string;
  baseName?: string;
  owner?: string;
  updatedAt: string;
  knowledgeCount: number;
  skillCount: number;
  toolCount: number;
  tags: string[];
  promptSummary: string;
  sourceSession?: string;
}

export interface CaseRecord {
  id: string;
  code: string;
  name: string;
  type: string;
  status: "在办" | "待审批" | "已结案";
  owner: string;
  org: string;
  updatedAt: string;
  beginAt: string;
  targets: number;
  pendingApprovals: number;
  tags: string[];
}

export type TargetStatus = "在控" | "非在控" | "待审批" | "已终止" | "核查中";

export interface TargetRecord {
  id: string;
  type: "手机号" | "身份证" | "车牌号";
  value: string;
  name: string;
  status: TargetStatus;
  source: string;
  lastChecked: string;
}

export type ApprovalStatus = "审批中" | "已通过" | "已驳回" | "待发起";

export interface ApprovalRecord {
  id: string;
  flowId: string;
  batchId: string;
  targetId: string;
  targetLabel: string;
  targetCount: number;
  type: string;
  status: ApprovalStatus;
  submittedAt: string;
  approver: string;
}

export interface ReferenceItem {
  id: string;
  kind: "knowledge" | "data_table" | "mcp";
  name: string;
  meta?: string;
}

export interface ToolCall {
  name: string;
  summary: string;
  status: "done" | "running" | "waiting";
  steps: string[];
}

export interface ResultCardData {
  eyebrow: string;
  title: string;
  status: string;
  target: string;
  summary: string;
  fields: Array<{ label: string; value: string }>;
  columns: string[];
  rows: string[][];
}

export interface NoPermissionData {
  targetId: string;
  targetLabel: string;
  reason: string;
  detail: string;
  source: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  author: string;
  time: string;
  text?: string;
  table?: { columns: string[]; rows: string[][] };
  tool?: ToolCall;
  resultCard?: ResultCardData;
  noPermission?: NoPermissionData;
  references?: ReferenceItem[];
}

export type PlanStatus = "done" | "running" | "waiting" | "pending" | "stopped";

export interface PlanStep {
  id: string;
  title: string;
  owner: string;
  status: PlanStatus;
  tool: string;
  approval?: string;
}

export interface SessionRecord {
  id: string;
  title: string;
  agentId: string;
  caseId: string;
  updatedAt: string;
  messageCount: number;
}

export interface NotificationRecord {
  id: string;
  kind: "approval" | "task" | "notice";
  title: string;
  description: string;
  time: string;
  status?: string;
  caseCode?: string;
}

export const currentUser = {
  name: "张三",
  loginName: "zhangsan",
  org: "XX 市公安局 · 情报研判中心",
  roleLabel: "案件研判岗 · 管理角色",
  avatar: "张",
};

export const cases: CaseRecord[] = [
  {
    id: "CASE-2026-0001",
    code: "A202600001",
    name: "测试刑事案件一号",
    type: "刑事案件",
    status: "在办",
    owner: "张三",
    org: "情报研判中心",
    updatedAt: "2026-11-16 10:48",
    beginAt: "2026-01-05",
    targets: 3,
    pendingApprovals: 1,
    tags: ["电诈", "重点"],
  },
  {
    id: "CASE-2026-0002",
    code: "A202600002",
    name: "测试情报案件二号",
    type: "情报案件",
    status: "在办",
    owner: "李四",
    org: "情报研判中心",
    updatedAt: "2026-11-15 16:42",
    beginAt: "2026-02-11",
    targets: 1,
    pendingApprovals: 0,
    tags: ["线索核查"],
  },
  {
    id: "CASE-2026-0003",
    code: "A202600003",
    name: "电信诈骗线索核查",
    type: "行政案件",
    status: "待审批",
    owner: "王五",
    org: "反诈中心",
    updatedAt: "2026-11-15 10:08",
    beginAt: "2026-03-02",
    targets: 4,
    pendingApprovals: 2,
    tags: ["电诈", "紧急"],
  },
  {
    id: "CASE-2026-0004",
    code: "A202600004",
    name: "跨境赌博资金流向",
    type: "刑事案件",
    status: "已结案",
    owner: "赵六",
    org: "经侦支队",
    updatedAt: "2026-10-28 14:30",
    beginAt: "2026-01-18",
    targets: 0,
    pendingApprovals: 0,
    tags: ["资金流向"],
  },
  {
    id: "CASE-2026-0005",
    code: "A202600005",
    name: "重点人员动态核查",
    type: "情报案件",
    status: "在办",
    owner: "张三",
    org: "情报研判中心",
    updatedAt: "2026-11-14 09:12",
    beginAt: "2026-05-20",
    targets: 2,
    pendingApprovals: 0,
    tags: ["重点人员"],
  },
  {
    id: "CASE-2026-0006",
    code: "A202600006",
    name: "涉网黑灰产线索经营",
    type: "行政案件",
    status: "在办",
    owner: "周七",
    org: "网安支队",
    updatedAt: "2026-11-13 15:05",
    beginAt: "2026-06-08",
    targets: 5,
    pendingApprovals: 1,
    tags: ["涉网", "经营"],
  },
];

export const initialAgents: AgentRecord[] = [
  {
    id: "agent-verify-default",
    name: "信息核查·通用核查员",
    scope: "system",
    module: "verify",
    description:
      "面向案件在控目标的通用核查智能体，按“解析目标 → 权限校验 → 查询 → 结果整理”执行。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-12",
    knowledgeCount: 3,
    skillCount: 4,
    toolCount: 6,
    tags: ["系统预设", "通用", "查询服务"],
    promptSummary:
      "先确认目标是否属于当前案件在控目标；权限由查询中心判定；无权限时输出决策卡，不自行绕过。",
  },
  {
    id: "agent-verify-deep",
    name: "信息核查·深度关联",
    scope: "system",
    module: "verify",
    description:
      "对人员、号码、车辆进行多跳关联核查，适合需要关系链梳理的复杂线索。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-10",
    knowledgeCount: 5,
    skillCount: 6,
    toolCount: 8,
    tags: ["系统预设", "关联分析", "深度"],
    promptSummary:
      "优先展开一度、二度关联；每个结论必须给出证据来源；涉及非在控目标时停止并请求审批。",
  },
  {
    id: "agent-verify-fraud",
    name: "信息核查·电诈线索",
    scope: "system",
    module: "verify",
    description: "针对电诈线索，优先核查号码实名、近期活跃和关联设备。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-15 17:26",
    knowledgeCount: 6,
    skillCount: 4,
    toolCount: 7,
    tags: ["系统预设", "电诈", "号码核查"],
    promptSummary:
      "电诈线索优先输出号码实名、活跃时间、关联设备；涉及资金信息时必须单独申请审批。",
  },
  {
    id: "agent-verify-finance",
    name: "信息核查·资金流向",
    scope: "system",
    module: "verify",
    description: "围绕账户、交易流水和资金链路开展核查，适合经侦和涉赌案件。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-09",
    knowledgeCount: 4,
    skillCount: 5,
    toolCount: 7,
    tags: ["系统预设", "资金", "经侦"],
    promptSummary:
      "先梳理账户主体和交易时间线；资金类查询必须由查询中心返回权限结果；不展示无权限数据。",
  },
  {
    id: "agent-verify-vehicle",
    name: "信息核查·车辆核查",
    scope: "system",
    module: "verify",
    description:
      "面向车牌、车辆轨迹和关联人员的核查智能体，适合布控和轨迹分析。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-07",
    knowledgeCount: 3,
    skillCount: 4,
    toolCount: 5,
    tags: ["系统预设", "车辆", "轨迹"],
    promptSummary:
      "按车牌、时间范围、卡口三个维度组织查询；轨迹结果需要标注数据来源和时间。",
  },
  {
    id: "agent-office-default",
    name: "办公助手·通用",
    scope: "system",
    module: "office",
    description:
      "不依赖案件的办公智能体，用于公文起草、材料总结、表格整理和日常问答。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-08",
    knowledgeCount: 2,
    skillCount: 3,
    toolCount: 4,
    tags: ["系统预设", "办公", "非案件"],
    promptSummary:
      "以办公写作和材料整理为主；不访问案件数据；不确定的事实先向用户确认。",
  },
  {
    id: "agent-office-document",
    name: "办公助手·公文写作",
    scope: "system",
    module: "office",
    description: "面向通知、请示、简报和函件的公文写作智能体。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-11 14:18",
    knowledgeCount: 3,
    skillCount: 4,
    toolCount: 4,
    tags: ["系统预设", "公文", "写作"],
    promptSummary:
      "默认使用机关公文语体；先列提纲再成文；保留可替换的抬头、日期和落款。",
  },
  {
    id: "agent-my-verify-01",
    name: "我的核查流程",
    scope: "custom",
    module: "verify",
    description:
      "由「信息核查·通用核查员」复制而来：结果固定按人员信息、关联号码、结论三段输出。",
    enabled: true,
    model: "千问 3.6-27B",
    baseName: "信息核查·通用核查员",
    owner: "张三",
    updatedAt: "2026-11-16 09:40",
    knowledgeCount: 4,
    skillCount: 5,
    toolCount: 6,
    tags: ["自定义", "已调优"],
    promptSummary:
      "在通用核查流程上增加固定输出结构；结论不超过 3 条；每条结论标注查询证据。",
    sourceSession: "核查陈某某及关联号码",
  },
  {
    id: "agent-my-office-01",
    name: "我的公文助手",
    scope: "custom",
    module: "office",
    description: "由办公助手复制而来，增加了本单位公文模板和写作习惯。",
    enabled: true,
    model: "千问 3.6-27B",
    baseName: "办公助手·通用",
    owner: "张三",
    updatedAt: "2026-11-15 14:18",
    knowledgeCount: 3,
    skillCount: 4,
    toolCount: 4,
    tags: ["自定义", "公文"],
    promptSummary:
      "默认使用机关公文语体；先列提纲再成文；保留可替换的抬头、日期和落款。",
  },
  {
    id: "agent-office-meeting",
    name: "办公助手·会议纪要",
    scope: "system",
    module: "office",
    description: "把会议录音转写、会议记录整理成结论、待办、责任人和时限。",
    enabled: true,
    model: "千问 3.6-27B",
    updatedAt: "2026-11-06",
    knowledgeCount: 2,
    skillCount: 3,
    toolCount: 3,
    tags: ["系统预设", "会议", "纪要"],
    promptSummary:
      "先提炼会议结论，再整理待办；责任人、时限不明确时单独标注，不自行补充。",
  },
  {
    id: "agent-my-verify-03",
    name: "信息核查·旧版流程",
    scope: "system",
    module: "verify",
    description: "早期核查流程，已停用，仅保留历史会话追溯。",
    enabled: false,
    model: "千问 3.6-27B",
    updatedAt: "2026-10-29 11:03",
    knowledgeCount: 2,
    skillCount: 2,
    toolCount: 5,
    tags: ["系统预设", "旧版"],
    promptSummary: "旧版固定话术流程，仅保留用于历史会话复盘。",
  },
];

export const sessions: SessionRecord[] = [
  {
    id: "sess-001",
    title: "核查陈某某及关联号码",
    agentId: "agent-verify-default",
    caseId: "CASE-2026-0001",
    updatedAt: "今天 10:48",
    messageCount: 6,
  },
  {
    id: "sess-002",
    title: "电诈线索初筛与目标确认",
    agentId: "agent-verify-fraud",
    caseId: "CASE-2026-0003",
    updatedAt: "昨天 17:20",
    messageCount: 12,
  },
  {
    id: "sess-003",
    title: "跨境赌博资金关系梳理",
    agentId: "agent-verify-deep",
    caseId: "CASE-2026-0004",
    updatedAt: "11-14 15:32",
    messageCount: 8,
  },
];

export const targets: TargetRecord[] = [
  {
    id: "target-1",
    type: "手机号",
    value: "139****0001",
    name: "陈某某",
    status: "在控",
    source: "案件在控目标",
    lastChecked: "今天 10:42",
  },
  {
    id: "target-2",
    type: "身份证",
    value: "320***********1234",
    name: "待确认",
    status: "非在控",
    source: "用户本轮输入",
    lastChecked: "—",
  },
  {
    id: "target-3",
    type: "手机号",
    value: "138****8899",
    name: "李某某",
    status: "在控",
    source: "案件在控目标",
    lastChecked: "昨天 16:20",
  },
  {
    id: "target-4",
    type: "手机号",
    value: "137****6677",
    name: "王某",
    status: "在控",
    source: "案件在控目标",
    lastChecked: "11-14 09:30",
  },
];

export const approvals: ApprovalRecord[] = [
  {
    id: "approval-1",
    flowId: "审批-20261116-0031",
    batchId: "批次-20261116-0042",
    targetId: "target-2",
    targetLabel: "本批次 2 个目标（身份证 320***********1234 需审批）",
    targetCount: 2,
    type: "查询批次审批",
    status: "审批中",
    submittedAt: "今天 10:46",
    approver: "支队负责人",
  },
  {
    id: "approval-2",
    flowId: "审批-20261115-0018",
    batchId: "批次-20261115-0017",
    targetId: "target-3",
    targetLabel: "本批次 1 个目标（手机号 138****8899）",
    targetCount: 1,
    type: "查询批次审批",
    status: "已通过",
    submittedAt: "昨天 16:02",
    approver: "支队负责人",
  },
];

export const planSteps: PlanStep[] = [
  {
    id: "step-1",
    title: "解析核查目标",
    owner: "主智能体",
    status: "done",
    tool: "intent.resolve_targets",
  },
  {
    id: "step-2",
    title: "查询权限校验",
    owner: "查询中心",
    status: "done",
    tool: "查询权限校验",
  },
  {
    id: "step-3",
    title: "手机号核查",
    owner: "关联查询子智能体",
    status: "done",
    tool: "手机号实名查询",
  },
  {
    id: "step-4",
    title: "查询批次审批",
    owner: "查询中心 / 审批系统",
    status: "waiting",
    tool: "查询批次审批",
    approval: "审批-20261116-0031",
  },
  {
    id: "step-5",
    title: "汇总核查结论",
    owner: "主智能体",
    status: "pending",
    tool: "result.compose",
  },
];

export const references: ReferenceItem[] = [
  {
    id: "ref-1",
    kind: "mcp",
    name: "查询中心 · 人员基础信息",
    meta: "实名核验 · 命中",
  },
  {
    id: "ref-2",
    kind: "data_table",
    name: "案件在控目标清单",
    meta: "案件系统 · A202600001",
  },
  {
    id: "ref-3",
    kind: "knowledge",
    name: "信息核查工作规范（试行）.pdf",
    meta: "知识空间 · 核查规范",
  },
  {
    id: "ref-4",
    kind: "mcp",
    name: "查询中心 · 关联号码查询",
    meta: "关联号码查询 · 命中 2 条",
  },
];

export const initialMessages: ChatMessage[] = [
  {
    id: "msg-1",
    role: "user",
    author: "张三",
    time: "10:41",
    text: "核查手机号 139****0001 和身份证 320***********1234，看看是否有关联。",
  },
  {
    id: "msg-2",
    role: "assistant",
    author: "信息核查·通用核查员",
    time: "10:42",
    tool: {
      name: "核查任务编排",
      summary: "解析 2 个目标 → 查询中心按批次校验权限 → 返回审批需求",
      status: "done",
      steps: [
        "intent.resolve_targets  识别手机号、身份证各 1 个",
        "查询权限校验  手机号在控；身份证不在控",
        "手机号实名查询  执行成功",
      ],
    },
    text: "已完成目标解析。手机号 139****0001 属于本案在控目标，已通过 查询中心返回结果；身份证 320***********1234 不在当前案件在控目标中，查询中心返回无权限，需要你决定下一步。",
  },
  {
    id: "msg-3",
    role: "assistant",
    author: "信息核查·通用核查员",
    time: "10:43",
    noPermission: {
      targetId: "target-2",
      targetLabel: "身份证 320***********1234",
      reason: "本批次有目标需要审批",
      detail:
        "查询中心已逐目标校验：身份证 320***********1234 不在当前案件在控目标中，返回需要审批标识。查询以批次执行，审批通过后本批次所有目标将统一查询；终止则只终止本次查询批次。",
      source: "查询中心 · 批次权限校验",
    },
  },
  {
    id: "msg-4",
    role: "assistant",
    author: "信息核查·通用核查员",
    time: "10:44",
    resultCard: {
      eyebrow: "查询中心 · 人员核查",
      title: "陈某某 · 人员基础信息",
      status: "已命中",
      target: "手机号 139****0001",
      summary:
        "该号码实名登记为陈某某，近 30 天活跃，关联 2 个常用号码。建议围绕关联号码继续核查，但需逐目标确认在控关系。",
      fields: [
        { label: "姓名", value: "陈某某" },
        { label: "证件状态", value: "有效" },
        { label: "归属地", value: "江苏南京" },
        { label: "运营商", value: "中国移动" },
        { label: "最近活跃", value: "2026-11-16 08:12" },
        { label: "风险标签", value: "涉诈线索关联" },
      ],
      columns: ["关联号码", "关系", "最近活跃", "状态"],
      rows: [
        ["138****8899", "常用联系", "昨天 21:40", "在控"],
        ["151****2208", "近 7 天新增", "今天 07:56", "非在控"],
      ],
    },
    references,
  },
  {
    id: "msg-5",
    role: "user",
    author: "张三",
    time: "10:47",
    text: "先不要动身份证那个目标，把手机号的关联号码整理成结论。",
  },
  {
    id: "msg-6",
    role: "assistant",
    author: "信息核查·通用核查员",
    time: "10:48",
    text: "已保留本查询批次的待决策状态。手机号关联核查结论：陈某某当前常用号码为 138****8899（在控），近 7 天新增 151****2208（非在控）。身份证所在批次需要审批后才能统一执行全部目标查询。",
  },
];

export const notifications: NotificationRecord[] = [
  {
    id: "notice-1",
    kind: "approval",
    title: "非在控目标核查审批待处理",
    description:
      "审批-20261116-0031 · 身份证 320***********1234 · 当前节点：支队负责人",
    time: "今天 10:46",
    status: "审批中",
    caseCode: "A202600001",
  },
  {
    id: "notice-2",
    kind: "task",
    title: "核查任务已完成",
    description:
      "手机号 139****0001 的关联号码查询已完成，结果已写入当前会话。",
    time: "今天 10:44",
    status: "已完成",
    caseCode: "A202600001",
  },
  {
    id: "notice-4",
    kind: "approval",
    title: "关联号码查询审批已通过",
    description: "审批-20261115-0018 · 手机号 138****8899 · 审批结果：通过",
    time: "昨天 16:12",
    status: "已通过",
    caseCode: "A202600001",
  },
];

export function caseInfoFor(caseRecord: CaseRecord) {
  return [
    { label: "案件编号", value: caseRecord.code },
    { label: "案件名称", value: caseRecord.name },
    { label: "案件类型", value: caseRecord.type },
    { label: "主办单位", value: caseRecord.org },
    { label: "主办人", value: caseRecord.owner },
    { label: "案件状态", value: caseRecord.status },
    { label: "立案时间", value: caseRecord.beginAt },
    { label: "在控目标", value: `${caseRecord.targets} 个` },
  ];
}

export function agentStatusLabel(agent: AgentRecord) {
  if (!agent.enabled) return "已停用";
  return agent.scope === "system" ? "系统预设" : "自定义";
}

export interface AgentResourceItem {
  name: string;
  meta: string;
}

export interface AgentResources {
  knowledgeSpaces: AgentResourceItem[];
  dataSpaces: AgentResourceItem[];
  skills: AgentResourceItem[];
  tools: AgentResourceItem[];
}

const verifyBaseResources: AgentResources = {
  knowledgeSpaces: [
    { name: "信息核查工作规范", meta: "12 篇文档 · 只读" },
    { name: "案件材料模板", meta: "8 篇文档 · 只读" },
  ],
  dataSpaces: [
    { name: "案件在控目标", meta: "3 张表 · 只读" },
    { name: "核查结果汇总", meta: "1 张表 · 可写" },
  ],
  skills: [
    { name: "目标解析", meta: "内置 · 已启用" },
    { name: "权限校验", meta: "内置 · 已启用" },
    { name: "关联号码查询", meta: "内置 · 已启用" },
    { name: "核查结论整理", meta: "内置 · 已启用" },
  ],
  tools: [
    { name: "查询中心", meta: "只读 · 按目标授权" },
    { name: "案件系统目标校验", meta: "只读 · 平台接口" },
    { name: "知识库检索", meta: "只读" },
    { name: "数据表查询", meta: "只读" },
  ],
};

const officeBaseResources: AgentResources = {
  knowledgeSpaces: [
    { name: "公文写作规范", meta: "6 篇文档 · 只读" },
    { name: "常用材料模板", meta: "11 篇文档 · 只读" },
  ],
  dataSpaces: [{ name: "办公材料整理", meta: "2 张表 · 可写" }],
  skills: [
    { name: "公文起草", meta: "内置 · 已启用" },
    { name: "会议纪要", meta: "内置 · 已启用" },
    { name: "表格整理", meta: "内置 · 已启用" },
  ],
  tools: [
    { name: "文档解析", meta: "只读" },
    { name: "表格处理", meta: "可写" },
    { name: "知识库检索", meta: "只读" },
  ],
};

const resourceOverrides: Record<string, Partial<AgentResources>> = {
  "agent-verify-deep": {
    knowledgeSpaces: [
      { name: "信息核查工作规范", meta: "12 篇文档 · 只读" },
      { name: "关联关系研判指引", meta: "9 篇文档 · 只读" },
      { name: "案件材料模板", meta: "8 篇文档 · 只读" },
    ],
    dataSpaces: [
      { name: "案件在控目标", meta: "3 张表 · 只读" },
      { name: "关联关系图谱", meta: "1 张表 · 可写" },
      { name: "核查结果汇总", meta: "1 张表 · 可写" },
    ],
    skills: [
      { name: "目标解析", meta: "内置 · 已启用" },
      { name: "多跳关联", meta: "内置 · 已启用" },
      { name: "关系链梳理", meta: "内置 · 已启用" },
      { name: "权限校验", meta: "内置 · 已启用" },
      { name: "结论整理", meta: "内置 · 已启用" },
    ],
  },
  "agent-my-verify-01": {
    knowledgeSpaces: [
      { name: "信息核查工作规范", meta: "12 篇文档 · 只读" },
      { name: "案件材料模板", meta: "8 篇文档 · 只读" },
      { name: "我的核查模板", meta: "5 篇文档 · 只读" },
    ],
    skills: [
      { name: "目标解析", meta: "内置 · 已启用" },
      { name: "权限校验", meta: "内置 · 已启用" },
      { name: "关联号码查询", meta: "内置 · 已启用" },
      { name: "三段式结论", meta: "自定义 · 已启用" },
      { name: "证据来源标注", meta: "自定义 · 已启用" },
    ],
  },
  "agent-verify-fraud": {
    knowledgeSpaces: [
      { name: "信息核查工作规范", meta: "12 篇文档 · 只读" },
      { name: "电诈案件办理指引", meta: "7 篇文档 · 只读" },
      { name: "电诈话术与设备特征", meta: "4 篇文档 · 只读" },
    ],
    dataSpaces: [
      { name: "案件在控目标", meta: "3 张表 · 只读" },
      { name: "电诈线索汇总", meta: "2 张表 · 可写" },
    ],
  },
  "agent-office-document": {
    knowledgeSpaces: [
      { name: "公文写作规范", meta: "6 篇文档 · 只读" },
      { name: "常用材料模板", meta: "11 篇文档 · 只读" },
      { name: "我的公文模板库", meta: "9 篇文档 · 只读" },
    ],
  },
};

export function resourcesForAgent(agent: AgentRecord): AgentResources {
  const base =
    agent.module === "office" ? officeBaseResources : verifyBaseResources;
  const override = resourceOverrides[agent.id];
  if (!override) return base;
  return {
    knowledgeSpaces: override.knowledgeSpaces ?? base.knowledgeSpaces,
    dataSpaces: override.dataSpaces ?? base.dataSpaces,
    skills: override.skills ?? base.skills,
    tools: override.tools ?? base.tools,
  };
}
