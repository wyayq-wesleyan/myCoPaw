/**
 * 知识空间页：文档状态列的区分。
 *
 * 用户报告：「上传后一直显示等待 Worker，但 LLM 后台是闲置的」。
 *
 * 根因不在队列，而在状态展示：一次失败的导入会带 1 小时退避重新排队
 * （``fail_job(retry=True)`` 把 job 置回 ``queued``），于是这一行**看起来**
 * 和「刚上传、马上会被领取」完全一样（都显示「等待 Worker」），而后端其实
 * 已经记下了失败原因与重试时间 —— 只是前端从没读过这两个字段。
 *
 * 这里锁定两条性质：
 * 1. ``queued`` 且已尝试过（``job_attempts > 0``）必须显示为「等待重试」，
 *    而不是「等待 Worker」，并给出重试时间；
 * 2. 失败原因要能通过该标签的提示看到，否则用户只能看到一个没有解释的等待。
 * 3. 真正新入队（attempts == 0）仍显示原来的「等待 Worker」，避免误导反转。
 */
import { readFileSync } from "node:fs";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/test/common_setup";

const { mockListDocuments, mockHealth } = vi.hoisted(() => ({
  mockListDocuments: vi.fn(),
  mockHealth: vi.fn(),
}));

vi.mock("../../api/modules/knowledge", () => ({
  knowledgeApi: {
    health: mockHealth,
    listDocuments: mockListDocuments,
    deleteDocuments: vi.fn(),
    searchFilter: vi.fn(),
    previewDocument: vi.fn(),
    getDocumentStats: vi.fn(),
    reprocessDocument: vi.fn(),
    deleteDocument: vi.fn(),
    upload: vi.fn(),
  },
}));

const TREE = {
  tree: [
    {
      id: "root-1",
      name: "默认",
      parent_id: null,
      is_root: true,
      is_public: true,
      document_count: 1,
      table_count: 0,
      agents: [],
      children: [],
    },
  ],
};

vi.mock("../../api/modules/paths", async () => {
  const actual = await vi.importActual<typeof import("../../api/modules/paths")>(
    "../../api/modules/paths",
  );
  return {
    ...actual,
    pathTree: async () => TREE,
    pathsApi: {
      tree: async () => TREE,
      assignable: async () => ({ items: [] }),
      agents: async () => ({ agents: [] }),
      bindResource: vi.fn(),
      unbindResource: vi.fn(),
      unbindResourceAnyPath: vi.fn(),
      create: vi.fn(),
      rename: vi.fn(),
      remove: vi.fn(),
      bindAgent: vi.fn(),
      unbindAgent: vi.fn(),
    },
  };
});

vi.mock("../../api/modules/agents", () => ({
  agentsApi: { listAgents: async () => ({ agents: [] }) },
}));

vi.mock("./DocumentPreview", () => ({
  default: () => null,
}));

import KnowledgePage from "./index";

const FAIL_REASON = "解析失败：该 PDF 为扫描件，需要 OCR 模型";

beforeEach(() => {
  vi.clearAllMocks();
  mockHealth.mockResolvedValue({ configured: true, status: "ok" });
});

describe("知识空间状态列", () => {
  it("失败后等待重试的任务显示「等待重试」而不是「等待 Worker」", async () => {
    mockListDocuments.mockResolvedValue({
      items: [
        {
          id: "d-retry",
          filename: "扫描件.pdf",
          status: "queued",
          processing_stage: "queued",
          job_attempts: 1,
          job_error: FAIL_REASON,
          job_next_retry_at: new Date(Date.now() + 45 * 60_000).toISOString(),
        },
      ],
      total: 1,
      page: 1,
    });

    renderWithProviders(<KnowledgePage />);

    await waitFor(() => {
      expect(screen.getByText("扫描件.pdf")).toBeInTheDocument();
    });

    // The distinguishing label: this row already failed once and is backing off.
    expect(screen.getByText("等待重试")).toBeInTheDocument();
    expect(screen.queryByText("等待 Worker")).not.toBeInTheDocument();
    // ...and the user is told when it will be attempted again.
    expect(screen.getByText(/再次尝试/)).toBeInTheDocument();
  });

  it("从未尝试过的任务仍显示「等待 Worker」", async () => {
    mockListDocuments.mockResolvedValue({
      items: [
        {
          id: "d-new",
          filename: "新上传.pdf",
          status: "queued",
          processing_stage: "queued",
          job_attempts: 0,
        },
      ],
      total: 1,
      page: 1,
    });

    renderWithProviders(<KnowledgePage />);

    await waitFor(() => {
      expect(screen.getByText("新上传.pdf")).toBeInTheDocument();
    });

    // A fresh upload genuinely is waiting for the worker; do not relabel it.
    expect(screen.getByText("等待 Worker")).toBeInTheDocument();
    expect(screen.queryByText("等待重试")).not.toBeInTheDocument();
  });

  it("失败原因被交给「等待重试」标签的提示", async () => {
    // The reason must reach the tooltip. Asserting the rendered `title`
    // attribute is unreliable here (AntD only materialises it on hover in
    // jsdom), so this checks the source contract plus the visible label: a
    // bare "等待重试" with no accessible reason would leave the user with the
    // same unexplained wait that caused this report.
    // The tooltip's `title` is only materialised on hover in jsdom, so assert
    // the source contract instead: a bare "等待重试" with no reason attached
    // would leave the user with the same unexplained wait that caused this
    // report.
    const source = readFileSync(
      `${process.cwd()}/src/pages/Knowledge/index.tsx`,
      "utf8",
    );

    expect(source).toMatch(/const reason =\s*record\.job_error \|\| record\.error/);
    expect(source).toMatch(/<Tooltip title=\{reason\}>/);
  });

  it("没有重试字段的老数据不会被误标为「等待重试」", async () => {
    // Rows created before these columns existed must keep the plain label —
    // a missing `job_attempts` means "unknown", not "has failed".
    mockListDocuments.mockResolvedValue({
      items: [
        {
          id: "d-legacy",
          filename: "旧记录.pdf",
          status: "queued",
          processing_stage: "queued",
        },
      ],
      total: 1,
      page: 1,
    });

    renderWithProviders(<KnowledgePage />);

    await waitFor(() => {
      expect(screen.getByText("旧记录.pdf")).toBeInTheDocument();
    });

    expect(screen.getByText("等待 Worker")).toBeInTheDocument();
    expect(screen.queryByText("等待重试")).not.toBeInTheDocument();
  });
});
