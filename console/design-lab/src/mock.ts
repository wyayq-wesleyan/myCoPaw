export type Role = "admin" | "user";

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
}

export interface ReferenceItem {
  id: string;
  kind: "knowledge" | "data_table";
  name: string;
  meta?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  author: string;
  time: string;
  text?: string;
  tool?: {
    name: string;
    summary: string;
    status: "done" | "running";
    steps: string[];
  };
  table?: {
    columns: string[];
    rows: string[][];
  };
  references?: ReferenceItem[];
}

export const currentUser = {
  name: "张三",
  loginName: "zhangsan",
  org: "XX 市公安局 · 情报研判中心",
  roleLabel: "案件研判岗",
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
    updatedAt: "2026-11-14 09:20",
    beginAt: "2026-01-05",
  },
  {
    id: "CASE-2026-0002",
    code: "A202600002",
    name: "测试情报案件二号",
    type: "情报案件",
    status: "在办",
    owner: "李四",
    org: "情报研判中心",
    updatedAt: "2026-11-13 16:42",
    beginAt: "2026-02-11",
  },
  {
    id: "CASE-2026-0003",
    code: "A202600003",
    name: "电信诈骗线索核查",
    type: "行政案件",
    status: "待审批",
    owner: "王五",
    org: "反诈中心",
    updatedAt: "2026-11-12 10:08",
    beginAt: "2026-03-02",
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
  },
];

export const caseInfo = [
  { label: "案件编号", value: "A202600001" },
  { label: "案件名称", value: "测试刑事案件一号" },
  { label: "案件类型", value: "刑事案件" },
  { label: "主办单位", value: "情报研判中心" },
  { label: "主办人", value: "张三" },
  { label: "案件状态", value: "在办" },
  { label: "立案时间", value: "2026-01-05" },
  { label: "关联路径", value: "考试成绩" },
];

export const references: ReferenceItem[] = [
  {
    id: "doc-1",
    kind: "knowledge",
    name: "客观题分数.xlsx",
    meta: "知识空间 · 考试成绩",
  },
  {
    id: "table-1",
    kind: "data_table",
    name: "tb_hzxxs_xlsx_328005d6",
    meta: "数据空间 · 考试成绩",
  },
  {
    id: "doc-2",
    kind: "knowledge",
    name: "2026年执法资格考试评分说明.pdf",
    meta: "知识空间 · 考试出题",
  },
  {
    id: "table-2",
    kind: "data_table",
    name: "tb_hzxxs_xlsx_6230aefc",
    meta: "数据空间 · 考试成绩",
  },
  {
    id: "doc-3",
    kind: "knowledge",
    name: "参考人员名单.xlsx",
    meta: "知识空间 · 考试成绩",
  },
];

export const initialMessages: ChatMessage[] = [
  {
    id: "m-1",
    role: "user",
    author: "张三",
    time: "16:46",
    text: "把刚上传的 客观题分数.xlsx 建成数据表，按分数段统计人数，并给我一个简要判断。",
  },
  {
    id: "m-2",
    role: "assistant",
    author: "研判助手",
    time: "16:48",
    tool: {
      name: "数据入库与统计",
      summary: "创建数据表 → 导入 46 行 → 执行分组统计",
      status: "done",
      steps: [
        "create_data_table  tb_hzxxs_xlsx_328005d6",
        "import_data_table  46 行 / 7 列",
        "query_data_table  按分数段聚合",
      ],
    },
    text:
      "已把 46 条客观题成绩录入数据表，并完成分数段统计。整体分布在 50–80 分之间，70 分以上 18 人，占比约 39.1%；60 分以下 6 人，建议重点复核这 6 名人员的答题记录。",
    table: {
      columns: ["分数段", "人数", "占比"],
      rows: [
        ["80 分及以上", "5", "10.9%"],
        ["70–79 分", "13", "28.3%"],
        ["60–69 分", "22", "47.8%"],
        ["60 分以下", "6", "13.0%"],
      ],
    },
    references,
  },
];

export const approvalRecord = {
  status: "无需审批",
  hint: "本次为只读统计查询，未触发平台审批流。",
  rule: "导出原始数据或查询敏感字段时，将自动发起 TCMS 审批。",
};

export const notifications = [
  { id: "n-1", title: "案件 A202600001 有新档案", time: "10 分钟前" },
  { id: "n-2", title: "数据导出审批已通过", time: "1 小时前" },
  { id: "n-3", title: "知识空间同步完成", time: "昨天" },
];
